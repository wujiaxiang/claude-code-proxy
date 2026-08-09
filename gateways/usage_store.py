# 用量持久化模块（SQLite，纯标准库 sqlite3，无第三方依赖）。
#
# 为什么落 .cache/：代理进程跑在独立 mount namespace，/tmp 与 shell 隔离，
# 跨进程共享状态必须放仓库内 .cache/（AGENTS.md §10.4）。.cache/ 已在 .gitignore。
#
# 存什么：只存按 (date, label, model) 聚合的统计计数，绝无 key/token/请求体等敏感数据。
# cost/定价刻意不实现（本期 Must-NOT-Have）。
#
# 降级原则：本模块是「旁路观测」，任何 DB 故障都不得影响主请求链路——
# 所有公开函数捕获全部异常，logger.warning 后返回 False / 空 dict，绝不 raise。
#
# 跨模块约定（AGENTS.md §7）：对 server 的共享依赖用函数内延迟导入；此处再包一层
# try/except fallback，保证无 server 运行时（纯单测）也能独立 `import gateways.usage_store`。
import json
import logging
import os
import sqlite3
from typing import Any, Dict, Optional

# gateways/ 在项目根下，向上两级即 server.py 所在目录（不依赖 cwd）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_DB_PATH = os.path.join(BASE_DIR, ".cache", "usage.db")

_FALLBACK_LOGGER = logging.getLogger("usage_store")

# 累加字段——upsert_day 的 delta 白名单，也是 get_trend 的返回键集合
_COUNTER_FIELDS = (
    "requests",
    "ok",
    "err",
    "translated429",
    "prompt_tokens",
    "completion_tokens",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS usage_daily (
  date TEXT NOT NULL,
  label TEXT NOT NULL,
  model TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 0,
  err INTEGER NOT NULL DEFAULT 0,
  translated429 INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, label, model)
)
"""

_CREATE_TABLE_ANTHROPIC_SQL = """
CREATE TABLE IF NOT EXISTS anthropic_daily (
  date TEXT NOT NULL,
  total_requests INTEGER NOT NULL DEFAULT 0,
  passthrough_ok INTEGER NOT NULL DEFAULT 0,
  passthrough_error INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date)
)
"""

_CREATE_TABLE_AGGREGATOR_SQL = """
CREATE TABLE IF NOT EXISTS aggregator_daily (
  date TEXT NOT NULL,
  vm_id TEXT NOT NULL,
  member TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 0,
  degraded INTEGER NOT NULL DEFAULT 0,
  err INTEGER NOT NULL DEFAULT 0,
  error_types TEXT NOT NULL DEFAULT '{}',
  latency_sum_ms INTEGER NOT NULL DEFAULT 0,
  latency_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, vm_id, member)
)
"""

# 三条建表语句集合（init_db 一次性建齐）
_ALL_CREATE_TABLE_SQL = (
    _CREATE_TABLE_SQL,
    _CREATE_TABLE_ANTHROPIC_SQL,
    _CREATE_TABLE_AGGREGATOR_SQL,
)


def _log():
    """取 server 的 logger；无 server 运行时（单测）回落到本地 logger。

    只在 server 已被加载时复用它的 logger——绝不由本模块「首次」触发 server 导入。
    本模块是旁路观测，一条降级 warning 不该拉起整个 server（配置加载 / 凭据解密 /
    网关日志 handler 等副作用），那在单测里是污染，在运行时是无谓开销。
    """
    try:
        import sys
        srv = sys.modules.get("server")
        if srv is not None and getattr(srv, "logger", None) is not None:
            return srv.logger
    except Exception:
        pass
    return _FALLBACK_LOGGER


def _connect() -> Optional[sqlite3.Connection]:
    """打开连接并确保父目录存在。失败返回 None（调用方负责降级）。"""
    parent = os.path.dirname(USAGE_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> bool:
    """确保 .cache 目录与全部三张表（usage_daily / anthropic_daily / aggregator_daily）存在。幂等；失败降级返回 False。"""
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            for sql in _ALL_CREATE_TABLE_SQL:
                conn.execute(sql)
        return True
    except Exception as e:
        _log().warning(f"usage_store: init_db failed ({USAGE_DB_PATH}): {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def upsert_day(date: str, label: str, model: str, delta: Dict[str, Any]) -> bool:
    """按主键 (date, label, model) UPSERT 累加各计数。

    delta 缺失的字段按 0 处理（不改动已有值）。失败降级返回 False。
    """
    delta = delta or {}
    values = [_int(delta.get(f)) for f in _COUNTER_FIELDS]
    sql = (
        "INSERT INTO usage_daily (date, label, model, "
        + ", ".join(_COUNTER_FIELDS)
        + ") VALUES (?, ?, ?, "
        + ", ".join("?" for _ in _COUNTER_FIELDS)
        + ") ON CONFLICT(date, label, model) DO UPDATE SET "
        + ", ".join(f"{f} = usage_daily.{f} + excluded.{f}" for f in _COUNTER_FIELDS)
    )
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(sql, [date, label, model] + values)
        return True
    except Exception as e:
        _log().warning(f"usage_store: upsert_day failed (date={date} label={label} model={model}): {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_trend(days: int) -> Dict[str, Dict[str, int]]:
    """读最近 N 天（含今天，不含未来）按日聚合的用量。

    返回 {date: {requests, ok, err, translated429, prompt_tokens, completion_tokens}}，
    按 date 升序。无数据或 DB 异常返回空 dict。
    """
    try:
        n = max(1, int(days))
    except (TypeError, ValueError):
        n = 1
    sql = (
        "SELECT date, "
        + ", ".join(f"SUM({f})" for f in _COUNTER_FIELDS)
        + " FROM usage_daily"
        " WHERE date >= date('now', 'localtime', ?) AND date <= date('now', 'localtime')"
        " GROUP BY date ORDER BY date ASC"
    )
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_SQL)
            rows = conn.execute(sql, (f"-{n - 1} day",)).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            out[row[0]] = {f: _int(row[i + 1]) for i, f in enumerate(_COUNTER_FIELDS)}
        return out
    except Exception as e:
        _log().warning(f"usage_store: get_trend failed (days={days}): {e}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ───────────────────────────── anthropic_daily ─────────────────────────────
# 8081 翻译入口按天一行的统计：total_requests / passthrough_ok / passthrough_error。
# delta 键：totalRequests / passthroughOk / passthroughError（驼峰，与 server 侧命名一致）。

def upsert_anthropic_day(date: str, delta: Dict[str, Any]) -> bool:
    """按主键 (date) UPSERT 累加 8081 入口统计。

    delta 键：totalRequests / passthroughOk / passthroughError。缺失按 0。
    失败降级返回 False。
    """
    delta = delta or {}
    total = _int(delta.get("totalRequests"))
    ok = _int(delta.get("passthroughOk"))
    err = _int(delta.get("passthroughError"))
    sql = (
        "INSERT INTO anthropic_daily (date, total_requests, passthrough_ok, passthrough_error)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(date) DO UPDATE SET"
        " total_requests = anthropic_daily.total_requests + excluded.total_requests,"
        " passthrough_ok = anthropic_daily.passthrough_ok + excluded.passthrough_ok,"
        " passthrough_error = anthropic_daily.passthrough_error + excluded.passthrough_error"
    )
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_ANTHROPIC_SQL)
            conn.execute(sql, [date, total, ok, err])
        return True
    except Exception as e:
        _log().warning(f"usage_store: upsert_anthropic_day failed (date={date}): {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_anthropic_trend(days: int) -> Dict[str, Dict[str, int]]:
    """读 8081 入口最近 N 天（含今天不含未来）按日统计。

    返回 {date: {total_requests, passthrough_ok, passthrough_error}} 升序。
    无数据或异常返回 {}。
    """
    try:
        n = max(1, int(days))
    except (TypeError, ValueError):
        n = 1
    sql = (
        "SELECT date, total_requests, passthrough_ok, passthrough_error FROM anthropic_daily"
        " WHERE date >= date('now', 'localtime', ?) AND date <= date('now', 'localtime')"
        " ORDER BY date ASC"
    )
    _FIELDS = ("total_requests", "passthrough_ok", "passthrough_error")
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_ANTHROPIC_SQL)
            rows = conn.execute(sql, (f"-{n - 1} day",)).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            out[row[0]] = {f: _int(row[i + 1]) for i, f in enumerate(_FIELDS)}
        return out
    except Exception as e:
        _log().warning(f"usage_store: get_anthropic_trend failed (days={days}): {e}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ──────────────────────────── aggregator_daily ─────────────────────────────
# 8080 聚合网关按 (date, vm_id, member) 一行的统计。
# delta 键：requests / ok / degraded / err / error_types(dict) / latency_sum_ms / latency_count。

def _merge_error_types(existing_json: str, delta: Dict[str, Any]) -> str:
    """把现有 error_types JSON 与 delta 的 dict 逐 key 累加，返回合并后的 JSON 字符串。"""
    merged: Dict[str, int] = {}
    try:
        existing = json.loads(existing_json) if existing_json else {}
        if isinstance(existing, dict):
            for k, v in existing.items():
                merged[str(k)] = _int(v)
    except Exception:
        merged = {}
    for k, v in (delta or {}).items():
        merged[str(k)] = merged.get(str(k), 0) + _int(v)
    return json.dumps(merged, ensure_ascii=False)


def upsert_aggregator_day(date: str, vm_id: str, member: str, delta: Dict[str, Any]) -> bool:
    """按主键 (date, vm_id, member) UPSERT 累加 8080 聚合网关统计。

    delta 键：requests / ok / degraded / err / error_types(dict) / latency_sum_ms / latency_count。
    error_types 为 dict，DB 中以 JSON 存；累加时读取原值逐 key 合并后整体替换。
    失败降级返回 False。
    """
    delta = delta or {}
    requests = _int(delta.get("requests"))
    ok = _int(delta.get("ok"))
    degraded = _int(delta.get("degraded"))
    err = _int(delta.get("err"))
    latency_sum = _int(delta.get("latency_sum_ms"))
    latency_count = _int(delta.get("latency_count"))
    delta_err_types = delta.get("error_types") if isinstance(delta.get("error_types"), dict) else {}

    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_AGGREGATOR_SQL)
            # 读取原 error_types 以合并累加
            cur = conn.execute(
                "SELECT error_types FROM aggregator_daily WHERE date=? AND vm_id=? AND member=?",
                (date, vm_id, member),
            ).fetchone()
            existing_json = cur[0] if cur else "{}"
            merged_err_types = _merge_error_types(existing_json, delta_err_types or {})
            conn.execute(
                "INSERT INTO aggregator_daily"
                " (date, vm_id, member, requests, ok, degraded, err, error_types, latency_sum_ms, latency_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(date, vm_id, member) DO UPDATE SET"
                " requests = aggregator_daily.requests + excluded.requests,"
                " ok = aggregator_daily.ok + excluded.ok,"
                " degraded = aggregator_daily.degraded + excluded.degraded,"
                " err = aggregator_daily.err + excluded.err,"
                " error_types = excluded.error_types,"
                " latency_sum_ms = aggregator_daily.latency_sum_ms + excluded.latency_sum_ms,"
                " latency_count = aggregator_daily.latency_count + excluded.latency_count",
                [date, vm_id, member, requests, ok, degraded, err, merged_err_types,
                 latency_sum, latency_count],
            )
        return True
    except Exception as e:
        _log().warning(
            f"usage_store: upsert_aggregator_day failed (date={date} vm={vm_id} member={member}): {e}"
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_aggregator_trend(days: int) -> Dict[str, Dict[str, Any]]:
    """读 8080 聚合网关最近 N 天（含今天不含未来）按日聚合（跨 vm/member 行合并）。

    返回 {date: {requests, ok, degraded, err, error_types(合并 dict), latency_sum_ms, latency_count}}
    升序；error_types 各行的 JSON 解析后逐 key 合并。无数据或异常返回 {}。
    """
    try:
        n = max(1, int(days))
    except (TypeError, ValueError):
        n = 1
    sql = (
        "SELECT date, requests, ok, degraded, err, error_types, latency_sum_ms, latency_count"
        " FROM aggregator_daily"
        " WHERE date >= date('now', 'localtime', ?) AND date <= date('now', 'localtime')"
        " ORDER BY date ASC"
    )
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_AGGREGATOR_SQL)
            rows = conn.execute(sql, (f"-{n - 1} day",)).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            d = row[0]
            if d not in out:
                out[d] = {
                    "requests": 0, "ok": 0, "degraded": 0, "err": 0,
                    "error_types": {}, "latency_sum_ms": 0, "latency_count": 0,
                }
            agg = out[d]
            agg["requests"] += _int(row[1])
            agg["ok"] += _int(row[2])
            agg["degraded"] += _int(row[3])
            agg["err"] += _int(row[4])
            agg["latency_sum_ms"] += _int(row[6])
            agg["latency_count"] += _int(row[7])
            # error_types JSON 逐 key 合并
            try:
                et = json.loads(row[5]) if row[5] else {}
                if isinstance(et, dict):
                    for k, v in et.items():
                        k = str(k)
                        agg["error_types"][k] = agg["error_types"].get(k, 0) + _int(v)
            except Exception:
                pass
        return out
    except Exception as e:
        _log().warning(f"usage_store: get_aggregator_trend failed (days={days}): {e}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
