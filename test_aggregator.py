"""AggregatorEngine 单元测试（脚本式，无 pytest）。
用法: python test_aggregator.py
"""
import asyncio
import contextlib
import random
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gateways.aggregator.engine import AggregatorEngine, AllPoolsExhausted  # noqa: E402

passed = 0
failed = 0


def fake_response(status_code, text='{"error":"x"}', content_type="application/json"):
    """最小 httpx.Response 替身。

    engine.route_request 只访问三个属性：status_code / headers（.get("content-type")）
    / text。headers 必须是 dict 且 content-type 不能含 text/event-stream，
    否则 _extract_body_text 提前返回 "" —— 会让文本兜底与 unclassified 日志的
    body_prefix 失真。
    """
    return types.SimpleNamespace(
        status_code=status_code, headers={"content-type": content_type}, text=text
    )


def spy_trip(eng):
    """把 eng.trip 换成"记录 + 调用原实现"的包装，返回记录列表。

    保留真实熔断行为（端口确实被摘除，后续路由行为真实），同时可断言调用次数。
    断言"未熔断"时用 len(calls) == 0，比只验证换端点有真实验证力。
    """
    calls = []
    orig = eng.trip

    def wrapper(port, reason):
        calls.append((port, reason))
        return orig(port, reason)

    eng.trip = wrapper
    return calls


def two_member_target():
    """双成员默认池、无降级池的 target —— 便于确定性验证换端点/同端点重试。"""
    target = make_target()
    target["virtualModels"]["agg:pair"] = {
        "defaultPool": [
            {"port": 8082, "model": "pair-m1"},
            {"port": 8084, "model": "pair-m2"},
        ],
        "fallbackPool": [],
        "defaultRetries": 3,
        "fallbackRetries": 1,
    }
    return target


@contextlib.contextmanager
def capture_server_warnings():
    """捕获 engine.py `import server as _srv; _srv.logger.warning(...)` 的日志。

    engine 在 unclassified 分支里做函数内延迟导入，因此往 sys.modules 注入
    stub `server` 模块即可拦截；这样测试也不依赖 fastapi/litellm（系统 python3
    没装这些依赖，真去 import server 会 ModuleNotFoundError）。
    """
    warnings = []
    stub = types.ModuleType("server")
    setattr(  # noqa: B010 - ModuleType 无静态 logger 属性，动态注入是 stub 的常规做法
        stub,
        "logger",
        types.SimpleNamespace(
            warning=warnings.append,
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
    )
    saved = sys.modules.get("server")
    sys.modules["server"] = stub
    try:
        yield warnings
    finally:
        if saved is None:
            sys.modules.pop("server", None)
        else:
            sys.modules["server"] = saved


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


def test_classify_failure_status_codes():
    """classify_failure 三层判定：状态码优先于文本，2xx 绝不文本兜底。"""
    eng = AggregatorEngine(make_target(), rng=random.Random(1))

    # 已知状态码直接返回映射分类
    assert eng.classify_failure(401, "", lambda t: False) == "retry_other_or_fallback"
    assert eng.classify_failure(500, "", lambda t: False) == "retry_same"
    assert eng.classify_failure(400, "", lambda t: False) == "pass_through_to_client"

    # 关键回归：2xx 即使 body 命中配额文本也绝不判定为熔断/重试
    assert eng.classify_failure(200, "insufficient credit", lambda t: True) == "success"

    # None 状态码（无状态码信号）→ 文本兜底：命中配额词 → retry_other_or_fallback
    assert eng.classify_failure(None, "insufficient credit", lambda t: True) == "retry_other_or_fallback"
    # None 状态码且文本未命中 → 安全默认 success
    assert eng.classify_failure(None, "", lambda t: False) == "success"

    # 未知状态码且非 2xx → 文本兜底
    assert eng.classify_failure(418, "", lambda t: False) == "unclassified"
    assert eng.classify_failure(418, "insufficient credit", lambda t: True) == "retry_other_or_fallback"


async def _assert_auth_class_trips_and_fails_over(status_code, expected_reason):
    """401/402/403 共用编排：首个端点返回该状态码 → 熔断 + 换端点成功。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []

    async def send_fn(member, info):
        calls.append(member.port)
        if len(calls) == 1:
            return fake_response(status_code, '{"error":"denied"}')
        return fake_response(200, '{"ok":true}')

    member, result = await eng.route_request("agg:pair", session_id=None, send_fn=send_fn)

    first_port = calls[0]
    assert len(calls) == 2, f"应换端点重试一次，实际尝试 {calls}"
    assert member.port != first_port, f"应换到另一个端口，仍是 {member.port}"
    assert getattr(result, "status_code", None) == 200

    # 熔断确实发生，且 reason 正确
    assert tripped == [(first_port, expected_reason)], f"熔断记录不符: {tripped}"
    breakers = eng.tripped_ports()
    assert first_port in breakers, f"端口 {first_port} 应被熔断"
    assert breakers[first_port].state == "tripped"
    assert breakers[first_port].reason == expected_reason
    # 未失败的端口不应被熔断
    assert member.port not in breakers, "成功端口不应被熔断"

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    failed_key = next(k for k in stats if k.startswith(f"{first_port}:"))
    assert stats[failed_key]["error_types"].get(expected_reason, 0) >= 1


async def test_route_request_401_triggers_fallback_and_trip():
    """401 凭据失效 → 熔断该端口（reason=401_auth）并换到池内另一端点。"""
    await _assert_auth_class_trips_and_fails_over(401, "401_auth")


async def test_route_request_402_triggers_fallback_and_trip():
    """402 欠费 → 熔断该端口（reason=402_billing）并换到池内另一端点。"""
    await _assert_auth_class_trips_and_fails_over(402, "402_billing")


async def test_route_request_403_triggers_fallback_and_trip():
    """403 禁止 → 熔断该端口（reason=403_forbidden）并换到池内另一端点。"""
    await _assert_auth_class_trips_and_fails_over(403, "403_forbidden")


async def test_route_request_429_fails_over_without_tripping():
    """关键回归：429 限流立刻换端点，但全程绝不熔断。

    429 会自行恢复；trip 会摘除端口 300s，误伤共享该端口的其他虚拟模型/会话。
    """
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []

    async def send_fn(member, info):
        calls.append(member.port)
        if len(calls) == 1:
            return fake_response(429, '{"error":"rate_limit_exceeded"}')
        return fake_response(200, '{"ok":true}')

    member, result = await eng.route_request("agg:pair", session_id=None, send_fn=send_fn)

    # 核心断言：trip 全程未被调用
    assert len(tripped) == 0, f"429 绝不应触发熔断，却发生了 {tripped}"
    assert eng.tripped_ports() == {}, "429 后不应有任何端口处于熔断态"

    # 换端点确实发生
    assert len(calls) == 2, f"429 应立刻换端点，实际尝试 {calls}"
    assert calls[0] != calls[1], "第二次尝试应是另一个端口"
    assert member.port == calls[1]
    assert getattr(result, "status_code", None) == 200

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    limited_key = next(k for k in stats if k.startswith(f"{calls[0]}:"))
    assert stats[limited_key]["error_types"].get("429_rate_limit", 0) >= 1, (
        f"应记录 429_rate_limit，实际 {stats[limited_key]['error_types']}"
    )


async def test_route_request_5xx_retries_same_then_fails_over_without_tripping():
    """关键回归：5xx 先同端点重试 1 次，仍失败才换端点；全程绝不熔断。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []

    async def send_fn(member, info):
        calls.append(member.port)
        return fake_response(500, '{"error":"internal server error"}')

    try:
        await eng.route_request("agg:pair", session_id=None, send_fn=send_fn)
        raise AssertionError("全部 500 且无降级池，应抛 AllPoolsExhausted")
    except AllPoolsExhausted:
        pass

    # 核心断言：trip 全程未被调用
    assert len(tripped) == 0, f"5xx 绝不应触发熔断，却发生了 {tripped}"
    assert eng.tripped_ports() == {}, "5xx 后不应有任何端口处于熔断态"

    # 同端点重试 1 次 → 第 1、2 次尝试是同一端口，第 3 次换端点
    first_port = calls[0]
    assert calls[0] == calls[1], f"5xx 首次应同端点重试，实际 {calls}"
    assert calls.count(first_port) == 2, f"同一端口应被尝试 2 次，实际 {calls}"
    assert calls[2] != first_port, f"第二次失败后应换端点，实际 {calls}"

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    failed_key = next(k for k in stats if k.startswith(f"{first_port}:"))
    error_types = stats[failed_key]["error_types"]
    assert error_types.get("500_transient", 0) >= 1, f"应记录 500_transient，实际 {error_types}"
    assert error_types.get("5xx_persistent", 0) >= 1, f"应记录 5xx_persistent，实际 {error_types}"


async def test_route_request_400_passthrough_no_retry():
    """400 客户端错误 → 原样透传给客户端：不重试、不换端点、不熔断。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []
    bad_request = fake_response(400, '{"error":"invalid request body"}')

    async def send_fn(member, info):
        calls.append(member.port)
        return bad_request

    member, result = await eng.route_request("agg:pair", session_id=None, send_fn=send_fn)

    assert len(calls) == 1, f"400 不应重试，实际尝试 {calls}"
    assert result is bad_request, "400 应原样返回上游响应对象"
    assert len(tripped) == 0, f"400 不应熔断，却发生了 {tripped}"
    assert eng.tripped_ports() == {}

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    key = next(k for k in stats if k.startswith(f"{member.port}:"))
    assert stats[key]["error_types"].get("pass_through_to_client", 0) >= 1


async def test_route_request_unclassified_status_logs_warning():
    """未知状态码（418）且 body 无配额词 → 透传 + 打一条 WARNING 日志。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []
    teapot = fake_response(418, '{"detail":"i am a teapot"}')

    async def send_fn(member, info):
        calls.append(member.port)
        return teapot

    with capture_server_warnings() as warnings:
        member, result = await eng.route_request("agg:pair", session_id=None, send_fn=send_fn)

    assert len(calls) == 1, f"unclassified 不应重试，实际尝试 {calls}"
    assert result is teapot, "unclassified 应原样返回上游响应对象"
    assert len(tripped) == 0, f"unclassified 不应熔断，却发生了 {tripped}"
    assert eng.tripped_ports() == {}

    assert len(warnings) == 1, f"应打且仅打一条 WARNING，实际 {warnings}"
    msg = warnings[0]
    assert "unclassified" in msg, f"日志应含 'unclassified'：{msg}"
    assert "418" in msg, f"日志应含状态码 418：{msg}"
    assert str(member.port) in msg, f"日志应含端口号：{msg}"

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    key = next(k for k in stats if k.startswith(f"{member.port}:"))
    assert stats[key]["error_types"].get("unclassified", 0) >= 1


async def test_sticky_member_failure_retries_same_endpoint_first():
    """粘性成员遇 5xx：先同端点重试，仍失败才换端点，成功后粘性重新生根。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    session_id = "sticky-5xx"

    # 第一轮：无粘性 → 成功并在端口 A 生根
    round1 = []

    async def send_ok(member, info):
        round1.append(member.port)
        return fake_response(200, '{"ok":true}')

    member_a, _ = await eng.route_request("agg:pair", session_id, send_ok)
    port_a = member_a.port
    assert len(round1) == 1 and round1[0] == port_a
    port_b = 8084 if port_a == 8082 else 8082

    # 第二轮：粘性命中 A，A 持续 500 → 同端点重试 → 换到 B 成功
    round2 = []

    async def send_a_fails(member, info):
        round2.append(member.port)
        if member.port == port_a:
            return fake_response(500, '{"error":"internal"}')
        return fake_response(200, '{"ok":true}')

    member2, _ = await eng.route_request("agg:pair", session_id, send_a_fails)
    assert round2[0] == port_a, f"应优先打粘性成员 {port_a}，实际 {round2}"
    assert round2[:2] == [port_a, port_a], f"粘性成员 5xx 应同端点重试，实际 {round2}"
    assert round2.count(port_a) == 2, f"A 应被尝试 2 次，实际 {round2}"
    assert member2.port == port_b, f"应换到端口 B={port_b}，实际 {member2.port}"
    assert len(tripped) == 0, f"5xx 全程不应熔断，却发生了 {tripped}"

    # 第三轮：粘性已重新生根到 B → 第一次尝试就是 B
    round3 = []

    async def send_ok3(member, info):
        round3.append(member.port)
        return fake_response(200, '{"ok":true}')

    member3, _ = await eng.route_request("agg:pair", session_id, send_ok3)
    assert round3 == [port_b], f"粘性应重新生根到 B={port_b}，实际 {round3}"
    assert member3.port == port_b


async def test_sticky_member_failover_reanchors_new_sticky():
    """粘性成员被熔断（402）后换到 B 成功 → 粘性确定性重新生根到 B。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(3))
    session_id = "sticky-402"

    # 第一轮：粘性 miss → 在 A 生根
    round1 = []

    async def send_ok(member, info):
        round1.append(member.port)
        return fake_response(200, '{"ok":true}')

    member_a, _ = await eng.route_request("agg:pair", session_id, send_ok)
    port_a = member_a.port
    port_b = 8084 if port_a == 8082 else 8082

    # 第二轮：粘性命中 A，A 返回 402 → 熔断 A → 换 B 成功
    round2 = []
    tripped = spy_trip(eng)

    async def send_a_402(member, info):
        round2.append(member.port)
        if member.port == port_a:
            return fake_response(402, '{"error":"payment required"}')
        return fake_response(200, '{"ok":true}')

    member2, _ = await eng.route_request("agg:pair", session_id, send_a_402)
    assert round2[0] == port_a, f"应优先打粘性成员 {port_a}，实际 {round2}"
    assert round2 == [port_a, port_b], f"402 应立刻换端点，实际 {round2}"
    assert member2.port == port_b
    assert tripped == [(port_a, "402_billing")], f"A 应因 402_billing 熔断，实际 {tripped}"
    assert eng.tripped_ports()[port_a].state == "tripped"

    # 第三轮：同 session 直接命中 B（不重新随机选择）
    round3 = []

    async def send_ok3(member, info):
        round3.append(member.port)
        return fake_response(200, '{"ok":true}')

    member3, _ = await eng.route_request("agg:pair", session_id, send_ok3)
    assert round3[0] == port_b, f"第三轮第一个尝试就应是 B={port_b}，实际 {round3}"
    assert round3 == [port_b], f"粘性命中应只尝试一次，实际 {round3}"
    assert member3.port == port_b


async def test_200_response_with_quota_keyword_in_body_is_never_tripped():
    """端到端关键回归：200 响应体里出现配额词（正常对话内容）绝不误判熔断。"""
    eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    tripped = spy_trip(eng)
    calls = []
    ok_with_keyword = fake_response(200, '{"content":"insufficient credit balance"}')

    # 前置确认：该文本确实命中配额模式，所以本测试验证的是状态码优先级而非文本不匹配
    assert eng.quota_error("insufficient credit balance"), "测试前提：该文本应命中配额模式"

    async def send_fn(member, info):
        calls.append(member.port)
        return ok_with_keyword

    member, result = await eng.route_request("agg:pair", session_id="quota-text", send_fn=send_fn)

    assert len(calls) == 1, f"200 应一次成功，实际尝试 {calls}"
    assert result is ok_with_keyword, "应原样返回该 200 响应对象"
    # 核心断言：trip 全程未被调用
    assert len(tripped) == 0, f"200 响应绝不应熔断，却发生了 {tripped}"
    assert eng.tripped_ports() == {}, "不应有任何端口处于熔断态"

    stats = eng.get_stats()["virtual_models"]["agg:pair"]
    key = next(k for k in stats if k.startswith(f"{member.port}:"))
    assert stats[key]["ok"] >= 1, f"应记 ok，实际 {stats[key]}"
    assert stats[key]["err"] == 0, f"不应记 err，实际 {stats[key]}"
    assert stats[key]["error_types"] == {}, f"不应有错误分类，实际 {stats[key]['error_types']}"

    # 该保证由两道独立防线共同提供，逐一钉死（只测其中一道会让另一道回归时静默通过）：
    # 防线 1 —— 惰性 body 读取：2xx 时 route_request 根本不读 body。
    #           探针放在响应对象的 .text 属性上（engine 读 body 的唯一途径），
    #           这样断言的是"有没有碰 body"这个直接可观测事实，而不是下游副作用。
    probe_eng = AggregatorEngine(two_member_target(), rng=random.Random(1))
    probe_tripped = spy_trip(probe_eng)
    text_reads = []

    class _BodyReadProbe:
        status_code = 200
        headers = {"content-type": "application/json"}

        @property
        def text(self):
            text_reads.append(True)
            return '{"content":"insufficient credit balance"}'

    probed_response = _BodyReadProbe()

    async def send_ok(member_, info):
        return probed_response

    _, probe_result = await probe_eng.route_request("agg:pair", session_id=None, send_fn=send_ok)
    assert probe_result is probed_response
    assert text_reads == [], f"2xx 响应不应读取 body，实际读了 {len(text_reads)} 次"
    assert len(probe_tripped) == 0, f"2xx 不应熔断，却发生了 {probe_tripped}"

    # 防线 2 —— classify_failure 的状态码优先级：即便 body 被读出来并命中配额词，
    #           2xx 也必须判定 success（防止惰性读取策略未来放宽时静默回归）。
    assert probe_eng.classify_failure(200, "insufficient credit balance", lambda t: True) == "success"
    assert probe_eng.classify_failure(299, "quota exceeded", lambda t: True) == "success"


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
