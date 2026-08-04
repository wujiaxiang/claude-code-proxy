"""验证 qclaw 重构：handler 改为 passthrough 后，特例参数通过 target 配置
（cleanQclawBody / extraHeaders）注入，而非硬编码 handler=="qclaw" 特判。

覆盖：
1. _handler_prepare_body：cleanQclawBody target 触发 body 清理（删非白名单字段）+ system message 注入
2. _prepare_fwd_headers：cleanQclawBody + extraHeaders[User-Agent] 触发 UA 强制为 OpenAI/JS 6.39.1，
   且客户端透传的其他 UA 变体被清除
3. 对照：普通 passthrough target（无 cleanQclawBody）不触发上述清理
"""
import json

import server


QCLAW_TARGET = {
    "label": "qclaw",
    "listenPort": 8085,
    "handler": "passthrough",
    "category": "crack",
    "extraHeaders": {"User-Agent": "OpenAI/JS 6.39.1"},
    "cleanQclawBody": True,
    "models": [],
}

PLAIN_TARGET = {
    "label": "openrouter",
    "listenPort": 8090,
    "handler": "passthrough",
    "category": "free",
    "models": [],
}


def test_body_clean_for_qclaw_target():
    # body 含非标准字段（qclaw 会 9002）+ 无 system message
    body = json.dumps({
        "model": "pool-hy3-preview",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high",  # 非白名单字段
        "unknown_field": 123,
    }).encode()
    new_bytes, body_json, cross = server._handler_prepare_body(QCLAW_TARGET, body)
    assert body_json is not None
    # system message 被注入
    assert any(m.get("role") == "system" for m in body_json.get("messages", []))
    # 非白名单字段被清理
    assert "reasoning_effort" not in body_json, "reasoning_effort 应被清理"
    assert "unknown_field" not in body_json, "unknown_field 应被清理"
    print("[PASS] qclaw target: body 清理 + system message 注入生效")


def test_body_not_cleaned_for_plain_target():
    body = json.dumps({
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "unknown_field": 123,
    }).encode()
    new_bytes, body_json, cross = server._handler_prepare_body(PLAIN_TARGET, body)
    # 普通 passthrough 不清理，未知字段保留
    assert "unknown_field" in (body_json or {}), "普通 target 不应清理字段"
    print("[PASS] 普通 passthrough target: 不触发 qclaw body 清理")


def test_ua_forced_for_qclaw_target():
    dummy_body = {"messages":[]}
    headers = {
        "authorization": "Bearer dummy",
        "user-agent": "python-httpx/0.28.1",  # 客户端透传的小写 UA
    }
    out = server._handler_prepare_headers(QCLAW_TARGET, dict(headers), {"messages": []})
    # extraHeaders 注入的 User-Agent 保留
    assert out.get("User-Agent") == "OpenAI/JS 6.39.1", f"UA 应为 OpenAI/JS 6.39.1，实际 {out.get('User-Agent')}"
    # 客户端小写 user-agent 被清除（防合并成逗号值）
    assert "user-agent" not in out, "客户端小写 user-agent 应被清除"
    print("[PASS] qclaw target: UA 强制 OpenAI/JS 6.39.1 + 清除客户端 UA")


def test_ua_passthrough_for_plain_target():
    dummy_body = {"messages":[]}
    headers = {"authorization": "Bearer dummy", "user-agent": "python-httpx/0.28.1"}
    out = server._handler_prepare_headers(PLAIN_TARGET, dict(headers), {"messages": []})
    # 普通 target 不清 UA
    assert out.get("user-agent") == "python-httpx/0.28.1", "普通 target 应保留客户端 UA"
    print("[PASS] 普通 passthrough target: UA 不变")


if __name__ == "__main__":
    test_body_clean_for_qclaw_target()
    test_body_not_cleaned_for_plain_target()
    test_ua_forced_for_qclaw_target()
    test_ua_passthrough_for_plain_target()
    print("ALL QCLAW REFACTOR TESTS PASSED")
