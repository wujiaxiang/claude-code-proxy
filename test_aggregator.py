"""AggregatorEngine 单元测试（脚本式，无 pytest）。
用法: python test_aggregator.py
"""
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gateways.aggregator.engine import AggregatorEngine, AllPoolsExhausted  # noqa: E402

passed = 0
failed = 0


def make_target(**overrides):
    target = {
        "label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
        "poolDefaults": {"defaultRetries": 2, "fallbackRetries": 1, "sessionAffinityTtlSeconds": 3600,
                          "probeIntervalSeconds": 300, "weight": 1},
        "quotaErrorPatterns": ["insufficient credit", "quota exceeded", "余额不足",
                               r"credits? exhausted", r"\b402\b"],
        "virtualModels": {
            "agg:sonnet": {
                "defaultPool": [
                    {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
                    {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
                    {"port": 8090, "model": "openrouter/auto"},
                ],
                "fallbackPool": [{"port": 8094, "model": "some-model"}],
                "defaultRetries": 3, "fallbackRetries": 1,
            },
            "agg:haiku": {
                "defaultPool": [{"port": 8082, "model": "claude-haiku-4.5"}],
                "fallbackPool": [],
            },
        },
    }
    target.update(overrides)
    return target


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def test_weighted_distribution():
    """1000 次无 session pick，成员频率应与权重比例偏差 < ±10%。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(42))
    counts = {8082: 0, 8084: 0, 8090: 0}
    for i in range(1000):
        m = eng.pick_member("agg:sonnet", session_id=None)
        counts[m.port] += 1
    total_weight = 3 + 2 + 1
    for port, weight in ((8082, 3), (8084, 2), (8090, 1)):
        expected = 1000 * weight / total_weight
        actual = counts[port]
        deviation = abs(actual - expected) / expected
        assert deviation < 0.10, f"port {port} 偏差过大: expected~{expected}, actual={actual}"


def test_equal_pool_uniform():
    """平等池（无 weight）各成员被选频率大致均匀。"""
    target = make_target()
    target["virtualModels"]["agg:equal"] = {
        "defaultPool": [
            {"port": 8090, "model": "m1"},
            {"port": 8091, "model": "m2"},
            {"port": 8093, "model": "m3"},
        ],
        "fallbackPool": [],
    }
    eng = AggregatorEngine(target, rng=random.Random(7))
    counts = {8090: 0, 8091: 0, 8093: 0}
    for _ in range(900):
        m = eng.pick_member("agg:equal", session_id=None)
        counts[m.port] += 1
    expected = 300
    for port, actual in counts.items():
        deviation = abs(actual - expected) / expected
        assert deviation < 0.15, f"port {port} 不均匀: {actual}"


def test_session_affinity_sticky():
    """同 (vm, session) 连续 20 次 pick 返回同一成员。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(1))
    first = eng.pick_member("agg:sonnet", session_id="sess-A")
    for _ in range(19):
        m = eng.pick_member("agg:sonnet", session_id="sess-A")
        assert m.key == first.key, f"会话粘性丢失: {m.key} != {first.key}"


def test_session_ttl_expiry():
    """TTL 过期后可重新选择（不保证不同，但不再走缓存路径）。"""
    clock = FakeClock(1000.0)
    eng = AggregatorEngine(make_target(), clock=clock, rng=random.Random(1))
    first = eng.pick_member("agg:sonnet", session_id="sess-B")
    clock.advance(3601)
    stats_before = eng.session_stats()
    eng.pick_member("agg:sonnet", session_id="sess-B")
    stats_after = eng.session_stats()
    # lookups 增加了但缓存已过期被清除重建
    assert stats_after["lookups"] == stats_before["lookups"] + 1


def test_trip_removes_port_from_all_vms():
    """quota 文本触发熔断；该端口从所有虚拟模型池中消失。429 文本不触发。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(1))
    assert not eng.quota_error("HTTP 429 rate_limit_exceeded: too_many_requests")
    assert not eng.quota_error("google.api_core.exceptions.ResourceExhausted")
    assert not eng.quota_error("rate_limit_error: please retry")
    assert eng.quota_error("Error: insufficient credit balance")
    assert eng.quota_error("quota exceeded for this month")
    assert eng.quota_error("余额不足，请充值")

    eng.trip(8082, "quota_error")
    tripped = eng.tripped_ports()
    assert 8082 in tripped and tripped[8082].state == "tripped"

    for _ in range(50):
        m = eng.pick_member("agg:sonnet", session_id=None)
        assert m.port != 8082, "熔断端口不应被选中 (agg:sonnet)"
    try:
        eng.pick_member("agg:haiku", session_id=None)
        raise AssertionError("agg:haiku 唯一成员在 8082，应无可用成员抛错")
    except ValueError:
        pass


def test_probe_recovery():
    """trip 后经过 probeIntervalSeconds → probe_due_ports 返回该端口；
    record_probe_result(ok=True) 恢复；ok=False 保持 tripped。"""
    clock = FakeClock(1000.0)
    eng = AggregatorEngine(make_target(), clock=clock, rng=random.Random(1))
    eng.trip(8082, "quota_error")
    assert eng.probe_due_ports() == []
    clock.advance(300)
    due = eng.probe_due_ports()
    assert due == [8082]
    assert eng.tripped_ports()[8082].state == "probing"

    eng.record_probe_result(8082, ok=False)
    assert eng.tripped_ports()[8082].state == "tripped"

    clock.advance(300)
    due2 = eng.probe_due_ports()
    assert due2 == [8082]
    eng.record_probe_result(8082, ok=True)
    assert 8082 not in eng.tripped_ports()
    m = eng.pick_member("agg:haiku", session_id=None)
    assert m.port == 8082


async def test_route_request_default_success():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    async def send_fn(member, info):
        return "ok response body"

    member, result = await eng.route_request("agg:sonnet", session_id="s1", send_fn=send_fn)
    assert result == "ok response body"
    assert member.port in (8082, 8084, 8090)


async def test_route_request_fallback_success():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    async def send_fn(member, info):
        if info["pool"] == "default":
            raise RuntimeError("connection failed")
        return "fallback ok"

    member, result = await eng.route_request("agg:sonnet", session_id=None, send_fn=send_fn)
    assert member.port == 8094
    assert result == "fallback ok"
    stats = eng.get_stats()
    fb_stats = stats["virtual_models"]["agg:sonnet"]["8094:some-model"]
    assert fb_stats["degraded"] == 1


async def test_route_request_all_pools_exhausted():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    async def send_fn(member, info):
        raise RuntimeError("always fails")

    try:
        await eng.route_request("agg:sonnet", session_id=None, send_fn=send_fn)
        raise AssertionError("应抛 AllPoolsExhausted")
    except AllPoolsExhausted as e:
        assert e.last_error is not None


async def test_route_request_no_fallback_pool_raises():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    async def send_fn(member, info):
        raise RuntimeError("fails")

    try:
        await eng.route_request("agg:haiku", session_id=None, send_fn=send_fn)
        raise AssertionError("agg:haiku 无降级池，应直接抛 AllPoolsExhausted")
    except AllPoolsExhausted:
        pass


def test_reload_preserves_state_clears_removed_ports():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))
    eng.pick_member("agg:sonnet", session_id="keep-me")
    eng.trip(8090, "quota_error")
    assert 8090 in eng.tripped_ports()

    # 同配置 reload 两次：状态保留
    eng.reload(make_target())
    eng.reload(make_target())
    assert 8090 in eng.tripped_ports(), "同配置 reload 后熔断状态应保留"
    assert ("agg:sonnet", "keep-me") in eng._sessions, "会话缓存应保留"

    # 移除 8090 的配置 → 其熔断状态应被清除
    target2 = make_target()
    target2["virtualModels"]["agg:sonnet"]["defaultPool"] = [
        {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
        {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
    ]
    eng.reload(target2)
    assert 8090 not in eng.tripped_ports(), "端口被移除后熔断状态应清除"


def test_multi_vm_isolation():
    """两虚拟模型共享端口 8082；隔离性验证。"""
    target = make_target()
    target["virtualModels"]["agg:opus"] = {
        "defaultPool": [{"port": 8082, "model": "claude-opus-4.8"}],
        "fallbackPool": [],
    }
    eng = AggregatorEngine(target, rng=random.Random(3))

    # (a) agg:sonnet 请求永不返回 agg:opus 专属 model 名
    for _ in range(30):
        m = eng.pick_member("agg:sonnet", session_id=None)
        assert m.model != "claude-opus-4.8"

    # (b) 同 session_id 在两虚拟模型下各建独立粘性条目
    m_sonnet = eng.pick_member("agg:sonnet", session_id="shared-sess")
    m_opus = eng.pick_member("agg:opus", session_id="shared-sess")
    assert eng.pick_member("agg:sonnet", session_id="shared-sess").key == m_sonnet.key
    assert eng.pick_member("agg:opus", session_id="shared-sess").key == m_opus.key
    assert m_opus.port == 8082 and m_opus.model == "claude-opus-4.8"

    # (c) trip(8082) → agg:sonnet 与 agg:opus 都失去 8082；agg:haiku 独立于此(它本身唯一端口就是8082)
    #     用一个不引用 8082 的虚拟模型验证不受影响
    target["virtualModels"]["agg:untouched"] = {
        "defaultPool": [{"port": 8093, "model": "unrelated-model"}],
        "fallbackPool": [],
    }
    eng.reload(target)
    eng.trip(8082, "quota_error")
    for _ in range(20):
        m = eng.pick_member("agg:sonnet", session_id=None)
        assert m.port != 8082
    try:
        eng.pick_member("agg:opus", session_id=None)
        raise AssertionError("agg:opus 唯一成员在 8082，应抛错")
    except ValueError:
        pass
    m_unrelated = eng.pick_member("agg:untouched", session_id=None)
    assert m_unrelated.port == 8093, "未引用 8082 的虚拟模型不应受影响"


async def test_unknown_virtual_model_raises():
    eng = AggregatorEngine(make_target(), rng=random.Random(1))
    try:
        eng.pick_member("agg:does-not-exist", session_id=None)
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass
    try:
        async def send_fn(member, info):
            return "x"

        await eng.route_request("agg:nope", session_id=None, send_fn=send_fn)
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass


def test_empty_default_pool_raises_on_construct():
    target = make_target()
    target["virtualModels"]["agg:broken"] = {"defaultPool": [], "fallbackPool": []}
    try:
        AggregatorEngine(target)
        raise AssertionError("空 defaultPool 应抛 ValueError")
    except ValueError:
        pass


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            if asyncio.iscoroutinefunction(t):
                asyncio.run(t())
            else:
                t()
            print(f"PASS {t.__name__}")
            globals()["passed"] += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            globals()["failed"] += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            globals()["failed"] += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
