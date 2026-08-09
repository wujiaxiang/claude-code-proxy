"""
gateways/usage_store 单元测试（SQLite 用量持久化：建表 / UPSERT 累加 / 趋势查询 / 降级）。
用法: python test_usage_store.py

隔离约定：所有用例把 usage_store.USAGE_DB_PATH monkeypatch 到 tempfile 临时目录，
绝不触碰仓库真实 .cache/usage.db。测试不启动 server.py（纯单测），因此本文件
也顺带锁定「gateways.usage_store 可在无 server 时独立 import」这一契约。
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gateways.usage_store as usage_store

passed = 0
failed = 0


class _TempDb:
    """上下文管理器：把 USAGE_DB_PATH 指向临时目录下的 usage.db，退出时清理。

    subdir 用于模拟 ".cache" 尚不存在的场景（用例 d）。
    """

    def __init__(self, subdir: str = ".cache", filename: str = "usage.db"):
        self._subdir = subdir
        self._filename = filename
        self.tmpdir = ""
        self.db_path = ""
        self._orig = ""

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="usage_store_test_")
        parts = [self.tmpdir]
        if self._subdir:
            parts.append(self._subdir)
        parts.append(self._filename)
        self.db_path = os.path.join(*parts)
        self._orig = usage_store.USAGE_DB_PATH
        usage_store.USAGE_DB_PATH = self.db_path
        return self

    def __exit__(self, exc_type, exc, tb):
        usage_store.USAGE_DB_PATH = self._orig
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        return False


def _table_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(usage_daily)").fetchall()
    finally:
        conn.close()
    return [r[1] for r in rows]


def _day_offset(n):
    """今天往前 n 天的 YYYY-MM-DD。"""
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


# ─── (a) init_db 建表成功 + 幂等 ───
def test_a_init_db_creates_table_and_is_idempotent():
    with _TempDb() as t:
        assert usage_store.init_db() is True, "首次 init_db 应返回 True"
        assert os.path.exists(t.db_path), f"db 文件应被创建: {t.db_path}"
        cols = _table_columns(t.db_path)
        for expected in ("date", "label", "model", "requests", "ok", "err",
                         "translated429", "prompt_tokens", "completion_tokens"):
            assert expected in cols, f"usage_daily 缺列 {expected}，实际: {cols}"
        # 幂等：重复调用不报错、不清空既有数据
        usage_store.upsert_day("2026-01-01", "copilot", "m1", {"requests": 1})
        assert usage_store.init_db() is True, "重复 init_db 应仍返回 True"
        assert usage_store.init_db() is True, "第三次 init_db 应仍返回 True"
        trend = usage_store.get_trend(3650)
        assert trend.get("2026-01-01", {}).get("requests") == 1, \
            f"重复 init_db 不应清空数据，实际 trend: {trend}"


# ─── (b) 同 key 两次 upsert 累加 ───
def test_b_upsert_day_accumulates_on_same_key():
    with _TempDb():
        usage_store.init_db()
        day = "2026-02-02"
        delta = {"requests": 1, "ok": 1, "err": 0, "translated429": 0,
                 "prompt_tokens": 10, "completion_tokens": 5}
        assert usage_store.upsert_day(day, "qclaw", "glm-4", delta) is True, \
            "首次 upsert_day 应返回 True"
        usage_store.upsert_day(day, "qclaw", "glm-4",
                               {"requests": 1, "ok": 0, "err": 1, "translated429": 2,
                                "prompt_tokens": 90, "completion_tokens": 15})
        row = usage_store.get_trend(3650)[day]
        assert row["requests"] == 2, f"requests 应累加为 2，实际 {row['requests']}"
        assert row["ok"] == 1, f"ok 应为 1，实际 {row['ok']}"
        assert row["err"] == 1, f"err 应为 1，实际 {row['err']}"
        assert row["translated429"] == 2, f"translated429 应为 2，实际 {row['translated429']}"
        assert row["prompt_tokens"] == 100, f"prompt_tokens 应为 100，实际 {row['prompt_tokens']}"
        assert row["completion_tokens"] == 20, f"completion_tokens 应为 20，实际 {row['completion_tokens']}"

        # 不同 model 走独立主键行，同日聚合时相加
        usage_store.upsert_day(day, "qclaw", "kimi", {"requests": 3, "ok": 3})
        row2 = usage_store.get_trend(3650)[day]
        assert row2["requests"] == 5, f"同日跨 model 应聚合为 5，实际 {row2['requests']}"
        assert row2["ok"] == 4, f"同日跨 model ok 应为 4，实际 {row2['ok']}"

        # 缺省字段按 0 处理，不得报错
        usage_store.upsert_day(day, "qclaw", "kimi", {"requests": 1})
        row3 = usage_store.get_trend(3650)[day]
        assert row3["requests"] == 6, f"缺省字段应按 0 累加，实际 {row3['requests']}"
        assert row3["ok"] == 4, f"未提供的 ok 不应变化，实际 {row3['ok']}"


# ─── (c) get_trend 近 N 天 / 升序 / 窗口裁剪 ───
def test_c_get_trend_window_and_ordering():
    with _TempDb():
        usage_store.init_db()
        d0, d1, d3, d10 = _day_offset(0), _day_offset(1), _day_offset(3), _day_offset(10)
        usage_store.upsert_day(d10, "copilot", "m", {"requests": 100})
        usage_store.upsert_day(d3, "copilot", "m", {"requests": 3})
        usage_store.upsert_day(d0, "copilot", "m", {"requests": 1})
        usage_store.upsert_day(d1, "copilot", "m", {"requests": 2})

        trend = usage_store.get_trend(7)
        assert d10 not in trend, f"7 天窗口外的 {d10} 不应出现，实际 keys: {list(trend)}"
        assert set(trend) == {d0, d1, d3}, f"窗口内应含 3 天，实际: {list(trend)}"
        assert list(trend.keys()) == sorted(trend.keys()), \
            f"结果应按 date 升序，实际: {list(trend.keys())}"
        assert trend[d3]["requests"] == 3

        # 未来日期不应混入（只含当天及之前）
        future = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
        usage_store.upsert_day(future, "copilot", "m", {"requests": 999})
        trend2 = usage_store.get_trend(7)
        assert future not in trend2, f"未来日期不应出现在 get_trend 里，实际 keys: {list(trend2)}"

        # 更大的窗口能拿到更早的数据
        assert d10 in usage_store.get_trend(30), "30 天窗口应包含 10 天前的数据"


def test_c2_get_trend_empty_and_degraded():
    with _TempDb():
        usage_store.init_db()
        assert usage_store.get_trend(7) == {}, "无数据时应返回空 dict"
    # 指向一个不可用路径（父路径是文件而非目录）→ 应降级返回 {} 而不是抛异常
    tmpdir = tempfile.mkdtemp(prefix="usage_store_test_bad_")
    orig = usage_store.USAGE_DB_PATH
    try:
        blocker = os.path.join(tmpdir, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        usage_store.USAGE_DB_PATH = os.path.join(blocker, "sub", "usage.db")
        assert usage_store.init_db() is False, "不可用路径的 init_db 应降级返回 False"
        assert usage_store.get_trend(7) == {}, "不可用路径的 get_trend 应降级返回 {}"
        assert usage_store.upsert_day("2026-01-01", "l", "m", {"requests": 1}) is False, \
            "不可用路径的 upsert_day 应降级返回 False"
    finally:
        usage_store.USAGE_DB_PATH = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── (d) 目录不存在时自动创建 ───
def test_d_init_db_creates_missing_cache_dir():
    with _TempDb(subdir=".cache") as t:
        cache_dir = os.path.dirname(t.db_path)
        assert not os.path.exists(cache_dir), "前置条件：.cache 目录此时不应存在"
        assert usage_store.init_db() is True, "init_db 应在目录缺失时自动创建"
        assert os.path.isdir(cache_dir), f".cache 目录应被自动创建: {cache_dir}"
        assert os.path.exists(t.db_path), "db 文件应被创建"


# ─── (e) 无 server 独立可用 + 常量契约 + 真实库零污染 ───
def test_e_standalone_import_and_path_contract():
    assert "server" not in sys.modules, \
        "测试进程不应加载 server.py（usage_store 必须能独立 import）"
    assert isinstance(usage_store.USAGE_DB_PATH, str) and usage_store.USAGE_DB_PATH
    expected = os.path.join(usage_store.BASE_DIR, ".cache", "usage.db")
    assert usage_store.USAGE_DB_PATH == expected, \
        f"USAGE_DB_PATH 应为 BASE_DIR/.cache/usage.db，实际 {usage_store.USAGE_DB_PATH}"
    assert os.path.isfile(os.path.join(usage_store.BASE_DIR, "server.py")), \
        f"BASE_DIR 应为项目根（含 server.py），实际 {usage_store.BASE_DIR}"
    # 真实库零污染：跑完全部用例后仓库 .cache/usage.db 不应被本测试创建
    real_db = expected
    assert not getattr(test_e_standalone_import_and_path_contract, "_created", False)
    if not os.path.exists(real_db):
        # 记录：若不存在，后续用例（已全部隔离）也不得创建它
        with _TempDb():
            usage_store.init_db()
        assert not os.path.exists(real_db), \
            f"测试不得创建真实库文件: {real_db}"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            globals()["passed"] += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            globals()["failed"] += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            globals()["failed"] += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
