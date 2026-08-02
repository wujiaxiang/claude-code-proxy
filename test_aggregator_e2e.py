"""AggregatorEngine 端到端集成场景测试（脚本式，无 pytest，全注入 fake）。

覆盖 6 个验收场景：
1. happy path + 会话粘性
2. 熔断 + 降级（含 429 不触发熔断的回归）
3. 探测恢复
4. 空降级池边界
5. 热重载保状态
6. 多虚拟模型隔离

用法: python test_aggregator_e2e.py
"""
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aggregator import AggregatorEngine, AllPoolsExhausted  # noqa: E402

passed = 0
failed = 0


def make_target(**overrides):
    """与 test_aggregator.py 的 make_target 保持同构，供 route_request 集成场景使用。"""
    target = {
        "label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
        "poolDefaults": {"defaultRetries": 3, "fallbackRetries": 1, "sessionAffinityTtlSeconds": 3600,
                          "probeIntervalSeconds": 300, "weight": 1},
        "quotaErrorPatterns": ["insufficient credit", "quota exceeded", "余额不足",
                               r"credits? exhausted", r"\b402\b"],
        "virtualModels": {
            "agg:sonnet": {
                "defaultPool": [
                    {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
                    {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
                    {"port": 8090, "model": "openrouter/auto", "weight": 1},
                ],
                "fallbackPool": [{"port": 8094, "model": "some-model"}],
                "defaultRetries": 3, "fallbackRetries": 1,
            },
            "agg:opus": {
                "defaultPool": [{"port": 8092, "model": "gemini-opus-like"}],
                "fallbackPool": [],
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


# 429 / 限流类文本样例（来自 server.py _VENDOR_ERROR_PATTERNS 典型值），必须绝不触发熔断
RATE_LIMIT_TEXTS = [
    "429 rate_limit_exceeded: slow down",
    "too_many_requests, please retry later",
    "google.api_core.exceptions.ResourceExhausted: 429",
    "rate_limit_error: overloaded_error",
]

QUOTA_TEXT = "Error: insufficient credit balance, please top up"


# ─── 场景 1: happy path + 会话粘性 ───

async def test_e2e_happy_path_session_sticky():
    """同一会话标识连续两次请求 → 同一成员被转发；第二次命中会话缓存未重新选择。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(11))
    calls = []

    async def fake_send(member, info):
        calls.append((member.port, member.model, info["attempt"], info["pool"]))
        return "ok body"

    member1, result1 = await eng.route_request("agg:sonnet", session_id="sess-happy", send_fn=fake_send)
    assert result1 == "ok body"

    stats_before = eng.session_stats()
    member2, result2 = await eng.route_request("agg:sonnet", session_id="sess-happy", send_fn=fake_send)
    stats_after = eng.session_stats()

    assert member2.key == member1.key, f"会话粘性丢失: {member2.key} != {member1.key}"
    assert len(calls) == 2, f"应恰好两次 send_fn 调用（各一次成功），实际 {len(calls)}"
    assert calls[0][:2] == (member1.port, member1.model)
    assert calls[1][:2] == (member1.port, member1.model)
    # 第二次命中缓存：hits 增加，lookups 增加，且未产生第二个 tried port（说明未重新走加权选择的重试循环）
    assert stats_after["hits"] == stats_before["hits"] + 1
    assert stats_after["lookups"] == stats_before["lookups"] + 1


# ─── 场景 2: 熔断 + 降级 + 429 回归 ───

async def test_e2e_trip_and_fallback_degrade():
    """默认池成员返回配额不足文本 → trip 生效、跨虚拟模型排除；请求降级池成功。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(5))
    calls = []

    async def fake_send(member, info):
        calls.append((member.port, member.model, info["pool"]))
        if info["pool"] == "default":
            return QUOTA_TEXT
        return "fallback ok body"

    member, result = await eng.route_request("agg:sonnet", session_id=None, send_fn=fake_send)

    # (b) 降级池成功
    assert member.port == 8094 and member.model == "some-model"
    assert result == "fallback ok body"
    degraded_calls = [c for c in calls if c[2] == "fallback"]
    assert degraded_calls == [(8094, "some-model", "fallback")]

    # (a) 默认池里被打过的成员全部被 trip（因为 QUOTA_TEXT 对每次尝试都返回）
    default_calls = [c for c in calls if c[2] == "default"]
    tripped_now = eng.tripped_ports()
    for port, _, _ in default_calls:
        assert port in tripped_now and tripped_now[port].state == "tripped"

    # 熔断跨虚拟模型生效：若 agg:haiku 恰好也用到某个被熔断端口，应被排除
    tripped_port_set = set(tripped_now.keys())
    if 8082 in tripped_port_set:
        try:
            eng.pick_member("agg:haiku", session_id=None)
            raise AssertionError("agg:haiku 唯一成员端口若被熔断应抛错")
        except ValueError:
            pass

    stats = eng.get_stats()
    fb_stats = stats["virtual_models"]["agg:sonnet"]["8094:some-model"]
    assert fb_stats["degraded"] == 1


async def test_e2e_rate_limit_never_trips_regression():
    """关键回归：429/限流文本不匹配 quotaErrorPatterns，引擎不将其识别为配额错误，
    因此既不触发熔断，也不会像 quota_error 那样强制换成员重试——响应体内容本身
    对引擎不透明，只有显式抛异常或命中 quota_error 才会让 route_request 继续换成员。
    这里验证：命中限流文本的响应被当作该成员的一次"正常"应答直接返回，且全程无端口被 trip。"""
    for rate_limit_text in RATE_LIMIT_TEXTS:
        eng = AggregatorEngine(make_target(), rng=random.Random(9))
        calls = []

        async def fake_send(member, info, _text=rate_limit_text):
            calls.append((member.port, info["pool"]))
            return _text

        member, result = await eng.route_request("agg:sonnet", session_id=None, send_fn=fake_send)

        # 不应有任何端口被熔断（回归的核心断言）
        assert eng.tripped_ports() == {}, f"'{rate_limit_text}' 不应触发熔断，但触发了: {eng.tripped_ports()}"
        # 未命中 quota_error → 引擎在第一次尝试就把该响应当作成功结果返回，仅尝试 1 次
        default_attempts = [c for c in calls if c[1] == "default"]
        assert len(default_attempts) == 1, f"非 quota 文本不应触发换成员重试，实际尝试 {len(default_attempts)} 次"
        assert result == rate_limit_text
        assert member.port in (8082, 8084, 8090)

        stats = eng.get_stats()
        member_stats = stats["virtual_models"]["agg:sonnet"][f"{member.port}:{member.model}"]
        assert member_stats["ok"] == 1, "限流文本应被记为该成员的一次 ok（非 err/degraded），证明未被误判为配额错误"


# ─── 场景 3: 探测恢复 ───

def test_e2e_probe_recovery_cycle():
    """trip 后注入时钟前进超过 probeIntervalSeconds → probe_due_ports 返回该端口；
    record_probe_result(True) 恢复可再选；record_probe_result(False) 保持 tripped 不可选。"""
    clock = FakeClock(2000.0)
    eng = AggregatorEngine(make_target(), clock=clock, rng=random.Random(2))

    eng.trip(8082, "quota_error")
    assert eng.probe_due_ports() == [], "未到探测间隔不应返回该端口"

    clock.advance(301)
    due = eng.probe_due_ports()
    assert due == [8082]

    # False 分支：保持 tripped，不可选
    eng.record_probe_result(8082, False)
    assert eng.tripped_ports()[8082].state == "tripped"
    try:
        eng.pick_member("agg:haiku", session_id=None)
        raise AssertionError("端口仍应熔断不可选")
    except ValueError:
        pass

    clock.advance(301)
    due2 = eng.probe_due_ports()
    assert due2 == [8082]

    # True 分支：恢复 normal 可再选
    eng.record_probe_result(8082, True)
    assert 8082 not in eng.tripped_ports()
    m = eng.pick_member("agg:haiku", session_id=None)
    assert m.port == 8082


# ─── 场景 4: 空降级池边界 ───

async def test_e2e_empty_fallback_pool_raises():
    """agg:opus（无 fallbackPool 配置内容）默认池全失败 → route_request 抛 AllPoolsExhausted。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    async def fake_send(member, info):
        raise RuntimeError(f"upstream connection refused on port {member.port}")

    try:
        await eng.route_request("agg:opus", session_id=None, send_fn=fake_send)
        raise AssertionError("空降级池 + 默认池全失败应抛 AllPoolsExhausted")
    except AllPoolsExhausted as e:
        assert isinstance(e.last_error, RuntimeError)
        assert "upstream connection refused on port 8092" in str(e.last_error)


# ─── 场景 5: 热重载保状态 ───

async def test_e2e_reload_preserves_and_clears_state():
    """reload(相同配置) 两次 → 会话粘性和熔断状态都保留；配置变化移除端口 → 该端口熔断状态清除。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(4))

    async def fake_send(member, info):
        return "ok"

    member, _ = await eng.route_request("agg:sonnet", session_id="reload-sess", send_fn=fake_send)
    eng.trip(8090, "quota_error")
    assert 8090 in eng.tripped_ports()

    eng.reload(make_target())
    eng.reload(make_target())
    assert 8090 in eng.tripped_ports(), "同配置 reload 两次后熔断状态应保留"
    assert ("agg:sonnet", "reload-sess") in eng._sessions, "会话粘性应保留"
    assert eng._sessions[("agg:sonnet", "reload-sess")].member.key == member.key

    target2 = make_target()
    target2["virtualModels"]["agg:sonnet"]["defaultPool"] = [
        {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
        {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
    ]
    eng.reload(target2)
    assert 8090 not in eng.tripped_ports(), "移除引用端口后熔断状态应清除"


# ─── 场景 6: 多虚拟模型隔离 ───

async def test_e2e_multi_vm_isolation_shared_port():
    """agg:sonnet 与 agg:opus 共享 8082 端口，全面隔离性验证。"""
    target = make_target()
    target["virtualModels"]["agg:opus"] = {
        "defaultPool": [{"port": 8082, "model": "claude-opus-4.8"}],
        "fallbackPool": [],
    }
    eng = AggregatorEngine(target, rng=random.Random(6))
    calls = []

    async def fake_send(member, info):
        calls.append((member.port, member.model))
        return "ok"

    # (a) agg:sonnet 请求绝不选中 agg:opus 独有成员
    for i in range(30):
        m, _ = await eng.route_request("agg:sonnet", session_id=f"iso-{i}", send_fn=fake_send)
        assert m.model != "claude-opus-4.8"
    assert all(model != "claude-opus-4.8" for _port, model in calls)

    # (b) 同一会话标识在两虚拟模型下各建独立粘性，互不干扰
    m_sonnet, _ = await eng.route_request("agg:sonnet", session_id="shared-sess", send_fn=fake_send)
    m_opus, _ = await eng.route_request("agg:opus", session_id="shared-sess", send_fn=fake_send)
    assert m_opus.port == 8082 and m_opus.model == "claude-opus-4.8"
    m_sonnet_again, _ = await eng.route_request("agg:sonnet", session_id="shared-sess", send_fn=fake_send)
    m_opus_again, _ = await eng.route_request("agg:opus", session_id="shared-sess", send_fn=fake_send)
    assert m_sonnet_again.key == m_sonnet.key, "agg:sonnet 会话粘性应独立保持"
    assert m_opus_again.key == m_opus.key, "agg:opus 会话粘性应独立保持"

    # (c) trip(8082) → 两个虚拟模型都失去 8082；不引用 8082 的第三方虚拟模型完全不受影响
    target["virtualModels"]["agg:untouched"] = {
        "defaultPool": [{"port": 8093, "model": "unrelated-model"}],
        "fallbackPool": [],
    }
    eng.reload(target)
    eng.trip(8082, "quota_error")

    for i in range(20):
        m = eng.pick_member("agg:sonnet", session_id=None)
        assert m.port != 8082
    try:
        eng.pick_member("agg:opus", session_id=None)
        raise AssertionError("agg:opus 唯一成员在 8082，应抛错")
    except ValueError:
        pass
    m_unrelated = eng.pick_member("agg:untouched", session_id=None)
    assert m_unrelated.port == 8093, "未引用 8082 的虚拟模型不应受影响"

    # (d) 未知虚拟模型 → ValueError（server.py 层转 400，此处只验证引擎层契约）
    try:
        eng.pick_member("agg:不存在", session_id=None)
        raise AssertionError("未知虚拟模型应抛 ValueError")
    except ValueError:
        pass
    try:
        await eng.route_request("agg:不存在", session_id=None, send_fn=fake_send)
        raise AssertionError("未知虚拟模型 route_request 应抛 ValueError")
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
