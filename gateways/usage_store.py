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
    """确保 .cache 目录与 usage_daily 表存在。幂等；失败降级返回 False。"""
    conn = None
    try:
        conn = _connect()
        assert conn is not None
        with conn:
            conn.execute(_CREATE_TABLE_SQL)
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
