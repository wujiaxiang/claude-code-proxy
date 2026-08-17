"""
test_messages_passthrough.py —— 8081 /v1/messages 透传回归套件（Task 1-8 合并，Task 9）。

本文件是 Task 1-8 七个分散测试文件的**单一合并事实源**：

  T1  test_anthropic_convert_reasoning.py       reasoning_content 多轮回传
  T2  test_v1messages_passthrough_auth.py       passthrough messages 鉴权注入
  T3  test_egress_guard.py                      target 出站 SSRF 防护
  T4  test_messages_contract.py (上)            thinking 字段过滤 + 字段白名单
  T5  test_messages_contract.py (下)            messagesProfile 能力门控
  T6  test_error_code_mapping.py                上游错误码标准化（广化枚举）
  T7  test_v1messages_stream_errors.py          流式路径错误标准化
  T8  test_anthropic_sse_error_guard.py         Anthropic 帧 event:error 流内守护

除合并既有用例外，本文件补全 bundle §9 要求的全部 6 项，并新增「鉴权 + 能力门控
联合」交互测试（单任务测试各自孤立、未曾覆盖）：

  §9-1 thinking 字段在 profile 允许时保留（单元 + 集成双层）
  §9-2 错误标准化在**流式与非流式双向**一致（429 + 更广枚举 + 内嵌-200）
  §9-3 未路由模型 → 404
  §9-4 鉴权注入：外部 target 拿到 _resolve_auth 凭据、客户端 secret 被剥离
  §9-5 reasoning_content 回声在翻译路径多轮保真
  §9-6 能力门控剥离不支持字段（profile says no 时 top_k 被丢弃）

ASGI 直调基础设施统一复用 `messages_test_helpers`（从 test_v1messages_lock.py
抽取），无重复样板。

用法: pytest test_messages_passthrough.py
"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import server as _srv  # noqa: E402
from messages_test_helpers import (  # noqa: E402
    FakeResponse,
    make_fake_client_cls,
    run_case,
    run_case_events,
    _req,
    _get_ci,
    _call_headers,
    _call_content,
)

from anthropic_convert import (  # noqa: E402  T1
    convert_anthropic_request_to_openai,
    convert_openai_response_to_anthropic,
)
from gateways.messages_contract import (  # noqa: E402  T4/T5
    _MESSAGES_ALLOWED_FIELDS,
    filter_messages_request,
)


# ============================================================
# 公共端口 / 模型常量
# ============================================================
MSG_PORT = 18099
MSG_MODEL = "upstream-model-x"
STREAM_PORT = 18098
STREAM_MODEL = "upstream-model-msg-stream"
GUARD_PORT = 18097
GUARD_MODEL = "upstream-model-sse-guard"
COMBO_PORT = 18096
COMBO_MODEL = "upstream-model-combo"


# ============================================================
# T1 —— reasoning_content 多轮回传（翻译路径）
# ============================================================
REASONING_TEXT = "let me think this through step by step before answering"


def test_openai_response_reasoning_content_becomes_thinking_block():
    """Turn 1: OpenAI 响应 reasoning_content → Anthropic thinking 块，逐字一致。"""
    openai_response = {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "The weather is sunny.",
                "reasoning_content": REASONING_TEXT,
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    anthropic_response = convert_openai_response_to_anthropic(openai_response, "claude-sonnet-5")

    thinking_blocks = [b for b in anthropic_response["content"] if b.get("type") == "thinking"]
    assert len(thinking_blocks) == 1, f"应恰好有 1 个 thinking 块: {anthropic_response['content']}"
    assert thinking_blocks[0]["thinking"] == REASONING_TEXT, (
        f"thinking 内容必须与 reasoning_content 逐字一致: {thinking_blocks[0]['thinking']!r} != {REASONING_TEXT!r}"
    )


def test_two_turn_reasoning_content_round_trip():
    """两轮：turn1 响应产出 thinking 块 → turn2 请求回放该 thinking 块 → 必须变回 reasoning_content。"""
    openai_response_turn1 = {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": REASONING_TEXT,
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    anthropic_response_turn1 = convert_openai_response_to_anthropic(
        openai_response_turn1, "claude-sonnet-5"
    )

    turn2_anthropic_request = {
        "model": "claude-sonnet-5",
        "messages": [
            {"role": "user", "content": "what's the weather in NYC?"},
            {"role": "assistant", "content": anthropic_response_turn1["content"]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_abc", "content": "sunny, 72F"},
            ]},
        ],
    }

    openai_req_turn2 = convert_anthropic_request_to_openai(turn2_anthropic_request)

    assistant_messages = [m for m in openai_req_turn2["messages"] if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1, f"应恰好 1 条 assistant 消息: {openai_req_turn2['messages']}"
    replayed = assistant_messages[0]

    assert "reasoning_content" in replayed, (
        f"回放给上游的 assistant 消息必须带 reasoning_content: {replayed}"
    )
    assert replayed["reasoning_content"] == REASONING_TEXT, (
        f"reasoning_content 必须逐字一致: {replayed['reasoning_content']!r} != {REASONING_TEXT!r}"
    )


# ============================================================
# T2 —— passthrough messages 鉴权注入（§9-4）
# ============================================================
_MODELS_CFG_WITH_APIKEY = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "ext-msg-secured",
            "aliases": ["ext-msg-secured-alias"],
            "target": {
                "port": MSG_PORT,
                "model": MSG_MODEL,
                "protocol": "messages",
                "apikey": "sk-target-secret",
            },
        }
    ],
}

_MODELS_CFG_WITH_APIKEY_ENV = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "ext-msg-env",
            "target": {
                "port": MSG_PORT,
                "model": MSG_MODEL,
                "protocol": "messages",
                "apikeyEnv": "TEST_MSG_APIKEY_ENV",
            },
        }
    ],
}

_MODELS_CFG_NO_CREDS = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "ext-msg-plain",
            "target": {
                "port": MSG_PORT,
                "model": MSG_MODEL,
                "protocol": "messages",
            },
        }
    ],
}


def _client_headers():
    return {
        "Authorization": "Bearer client-secret",
        "x-api-key": "client-xkey",
        "cookie": "client-cookie",
    }


def test_secured_target_injects_target_credential_and_strips_client_security_fields():
    """target 配置了 apikey → 转发 authorization 为目标凭据，客户端 x-api-key/cookie 不泄露。"""
    resp = FakeResponse(200, body=b'{"type":"message","content":[]}')
    status, _, _, calls, _ = run_case(
        [resp], _req("ext-msg-secured"), _MODELS_CFG_WITH_APIKEY, headers=_client_headers()
    )
    assert status == 200, f"期望 200，实到 {status}"
    assert calls, "未捕获到下游转发调用"
    sent_headers = _call_headers(calls[0])
    assert _get_ci(sent_headers, "authorization") == "Bearer sk-target-secret", \
        f"authorization 应被目标凭据覆盖，实到 {_get_ci(sent_headers, 'authorization')!r}"
    assert _get_ci(sent_headers, "x-api-key") is None, \
        f"客户端 x-api-key 不应泄露，实到 {_get_ci(sent_headers, 'x-api-key')!r}"
    assert _get_ci(sent_headers, "cookie") is None, \
        f"客户端 cookie 不应泄露，实到 {_get_ci(sent_headers, 'cookie')!r}"
    assert calls[0][1] == f"http://127.0.0.1:{MSG_PORT}/v1/messages", calls[0][1]


def test_authorized_target_uses_env_key_and_strips_client_secret():
    """target 用 apikeyEnv（环境变量）配置凭据 → 注入 env 读取的 key，客户端 secret 不出现。"""
    os.environ["TEST_MSG_APIKEY_ENV"] = "sk-env-secret"
    try:
        resp = FakeResponse(200, body=b'{"type":"message","content":[]}')
        status, _, _, calls, _ = run_case(
            [resp], _req("ext-msg-env"), _MODELS_CFG_WITH_APIKEY_ENV, headers=_client_headers()
        )
        assert status == 200, f"期望 200，实到 {status}"
        sent_headers = _call_headers(calls[0])
        assert _get_ci(sent_headers, "authorization") == "Bearer sk-env-secret", \
            f"authorization 应来自 env 凭据，实到 {_get_ci(sent_headers, 'authorization')!r}"
        assert _get_ci(sent_headers, "x-api-key") is None, \
            f"客户端 x-api-key 不应泄露，实到 {_get_ci(sent_headers, 'x-api-key')!r}"
        assert _get_ci(sent_headers, "cookie") is None, \
            f"客户端 cookie 不应泄露，实到 {_get_ci(sent_headers, 'cookie')!r}"
    finally:
        os.environ.pop("TEST_MSG_APIKEY_ENV", None)


def test_no_creds_target_passthrough_client_headers():
    """target 未配置任何凭据 → 回退为透传客户端 authorization / x-api-key / cookie（旧行为）。"""
    resp = FakeResponse(200, body=b'{"type":"message","content":[]}')
    status, _, _, calls, _ = run_case(
        [resp], _req("ext-msg-plain"), _MODELS_CFG_NO_CREDS, headers=_client_headers()
    )
    assert status == 200, f"期望 200，实到 {status}"
    sent_headers = _call_headers(calls[0])
    assert _get_ci(sent_headers, "authorization") == "Bearer client-secret", \
        f"无凭据时应透传客户端 authorization，实到 {_get_ci(sent_headers, 'authorization')!r}"
    assert _get_ci(sent_headers, "x-api-key") == "client-xkey", \
        f"无凭据时应透传客户端 x-api-key，实到 {_get_ci(sent_headers, 'x-api-key')!r}"
    assert _get_ci(sent_headers, "cookie") == "client-cookie", \
        f"无凭据时应透传客户端 cookie，实到 {_get_ci(sent_headers, 'cookie')!r}"


# ============================================================
# T3 —— target 出站 SSRF 防护（egress guard）
# ============================================================

def _eg_find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _eg_make_target(target_host, **overrides):
    target = {
        "label": "egress-test",
        "listenPort": _eg_find_free_port(),
        "category": "free",
        "handler": "passthrough",
        "targetHost": target_host,
        "targetPort": 443,
        "targetProtocol": "https",
        "routePrefix": "",
        "models": [],
        "secretRef": None,
        "apikeyEnv": None,
        "apikey": None,
        "enabled": True,
    }
    target.update(overrides)
    return target


def _eg_make_request():
    body = json.dumps({"model": "test-model", "messages": []}).encode()
    return (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )


async def _eg_run_target(target, request_bytes, timeout=5.0):
    async def handle(reader, writer):
        try:
            await _srv._handle_target_request(reader, writer, target)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request_bytes)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(), timeout)
        writer.close()
        await writer.wait_closed()
    finally:
        srv.close()
        await srv.wait_closed()
    return resp


async def _eg_start_mock_upstream():
    """最小合法上游：返回 200 空 JSON。"""
    port = _eg_find_free_port()

    async def handle(reader, writer):
        try:
            await reader.read(4096)
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(handle, "127.0.0.1", port)
    return srv, port


def test_is_internal_host_blocks_metadata_and_private_ranges():
    blocked = [
        "169.254.169.254",
        "10.0.0.1",
        "10.255.255.255",
        "192.168.1.1",
        "192.168.0.0",
        "172.16.0.1",
        "172.31.255.255",
        "foo.internal",
        "SERVICE.INTERNAL",
    ]
    for host in blocked:
        assert _srv._is_internal_host(host), f"{host} 应被判定为内网/受限地址"


def test_is_internal_host_allows_public_hosts():
    allowed = [
        "api.openai.com",
        "openrouter.ai",
        "8.8.8.8",
        "1.1.1.1",
        "172.32.0.1",
        "172.15.255.255",
    ]
    for host in allowed:
        assert not _srv._is_internal_host(host), f"{host} 不应被误判为内网地址"


def test_target_with_metadata_host_rejected_before_forwarding():
    """targetHost=169.254.169.254（云元数据服务）→ 转发前拒绝，明确错误响应。"""
    target = _eg_make_target("169.254.169.254")
    resp = asyncio.run(_eg_run_target(target, _eg_make_request()))
    status_line = resp.split(b"\r\n", 1)[0].decode()
    assert "200" not in status_line, f"应返回非 200 拒绝响应，实到 {status_line!r}"
    assert b"internal" in resp.lower() or b"forbidden" in resp.lower() or b"blocked" in resp.lower(), \
        f"错误响应体应明确说明内网拒绝原因，实到 {resp!r}"


def test_target_with_private_range_host_rejected_before_forwarding():
    """targetHost 落在 10.0.0.0/8 → 转发前拒绝。"""
    target = _eg_make_target("10.1.2.3")
    resp = asyncio.run(_eg_run_target(target, _eg_make_request()))
    status_line = resp.split(b"\r\n", 1)[0].decode()
    assert "200" not in status_line, f"应返回非 200 拒绝响应，实到 {status_line!r}"


def test_target_with_internal_domain_rejected_before_forwarding():
    """targetHost 为 *.internal 域名 → 转发前拒绝。"""
    target = _eg_make_target("metadata.internal")
    resp = asyncio.run(_eg_run_target(target, _eg_make_request()))
    status_line = resp.split(b"\r\n", 1)[0].decode()
    assert "200" not in status_line, f"应返回非 200 拒绝响应，实到 {status_line!r}"


def test_legitimate_external_target_unaffected():
    """targetHost 指向合法外部（本地 mock 上游模拟）→ 正常转发，不受阻断影响。"""
    async def scenario():
        upstream_srv, upstream_port = await _eg_start_mock_upstream()
        try:
            target = _eg_make_target("127.0.0.1", targetPort=upstream_port, targetProtocol="http")
            return await _eg_run_target(target, _eg_make_request())
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()

    resp = asyncio.run(scenario())
    status_line = resp.split(b"\r\n", 1)[0].decode()
    assert "200" in status_line, f"合法外部目标应正常转发得到 200，实到 {status_line!r}"
    assert b'"ok":true' in resp, f"应收到 mock 上游响应体，实到 {resp!r}"


# ============================================================
# T4 —— thinking 字段过滤 + 字段白名单（§9-1 单元层）
# ============================================================

def test_thinking_field_survives_filtering():
    """上游 extended thinking 必须随透传请求体一起转发到下游 Anthropic 端点。"""
    body = {
        "model": "claude-opus-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "some_unknown_field": "drop-me",
    }
    out = filter_messages_request(body)
    assert "thinking" in out, "thinking 字段必须在透传过滤后存活"
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "some_unknown_field" not in out


def test_non_allowlisted_field_is_dropped():
    """回归护栏：任意非白名单字段必须被丢弃，不能因加 thinking 而整体放宽。"""
    body = {
        "model": "x",
        "totally_made_up_key": 123,
        "another_junk": [1, 2, 3],
    }
    out = filter_messages_request(body)
    assert "totally_made_up_key" not in out
    assert "another_junk" not in out
    assert out == {"model": "x"}


def test_allowed_fields_constant_shape():
    """常量必须恰好 = 原白名单 ∪ {thinking}，不得增删其它字段。"""
    original = {
        "model", "max_tokens", "messages", "system", "temperature", "top_p",
        "top_k", "stop_sequences", "stream", "tools", "tool_choice", "metadata",
    }
    assert _MESSAGES_ALLOWED_FIELDS == original | {"thinking"}


# ============================================================
# T5 —— messagesProfile 能力门控（§9-6 单元层）
# ============================================================

def test_profile_supports_thinking_false_strips_thinking():
    """目标 messagesProfile.supportsThinking=false → thinking 字段在转发前被剥离。"""
    body = {
        "model": "claude-opus-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "top_k": 40,
    }
    profile = {"supportsThinking": False}
    out = filter_messages_request(body, profile=profile)
    assert "thinking" not in out, "supportsThinking=false 时 thinking 必须被剥离"
    assert out.get("top_k") == 40
    assert "messages" in out


def test_profile_supports_thinking_true_keeps_thinking():
    """目标 messagesProfile.supportsThinking=true → thinking 字段照常透传。"""
    body = {
        "model": "x",
        "thinking": {"type": "enabled", "budget_tokens": 256},
        "some_unknown_field": "drop-me",
    }
    profile = {"supportsThinking": True}
    out = filter_messages_request(body, profile=profile)
    assert out.get("thinking") == {"type": "enabled", "budget_tokens": 256}
    assert "some_unknown_field" not in out


def test_profile_absent_keeps_thinking_backward_compat():
    """profile 缺失（存量 target 不配置 messagesProfile）→ 完全沿用旧行为，thinking 透传。"""
    body = {
        "model": "x",
        "thinking": {"type": "enabled"},
        "top_k": 10,
    }
    out = filter_messages_request(body)
    assert "thinking" in out
    assert out.get("top_k") == 10


def test_profile_omits_supports_topk_keeps_topk():
    """profile 漏声明 supportsTopK → 默认 fail-open，top_k 必须透传（禁止全局 top_k 误删）。"""
    body = {
        "model": "x",
        "top_k": 64,
        "thinking": {"type": "enabled"},
    }
    profile = {"supportsThinking": True}
    out = filter_messages_request(body, profile=profile)
    assert out.get("top_k") == 64, "supportsTopK 缺失不得静默丢弃 top_k"
    assert "thinking" in out


def test_profile_supports_tool_choice_false_strips_tool_choice():
    """目标 messagesProfile.supportsToolChoice=false → tool_choice 字段被剥离。"""
    body = {
        "model": "x",
        "tools": [{"name": "f"}],
        "tool_choice": {"type": "auto"},
        "top_k": 5,
    }
    profile = {"supportsToolChoice": False}
    out = filter_messages_request(body, profile=profile)
    assert "tool_choice" not in out, "supportsToolChoice=false 时 tool_choice 必须被剥离"
    assert out.get("top_k") == 5
    assert "tools" in out


def test_profile_string_false_also_strips():
    """profile 值为字符串 'false'（JSON 配置常见）→ 同样触发剥离，容错。"""
    body = {"model": "x", "thinking": {"type": "enabled"}}
    profile = {"supportsThinking": "false"}
    out = filter_messages_request(body, profile=profile)
    assert "thinking" not in out


def test_profile_non_bool_keeps_field():
    """profile 值非布尔/非 'false' 字符串（如数字/其它）→ 透传，绝不误删。"""
    body = {"model": "x", "thinking": {"type": "enabled"}}
    profile = {"supportsThinking": 1}
    out = filter_messages_request(body, profile=profile)
    assert "thinking" in out


def test_profile_supports_topk_false_strips_top_k():
    """§9-6：目标 messagesProfile.supportsTopK=false → top_k 字段在转发前被剥离。"""
    body = {
        "model": "x",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "top_k": 40,
        "thinking": {"type": "enabled", "budget_tokens": 256},
    }
    profile = {"supportsTopK": False}
    out = filter_messages_request(body, profile=profile)
    assert "top_k" not in out, "supportsTopK=false 时 top_k 必须被剥离"
    # 其它语义能力字段（thinking）不受影响——同 profile 未声明 supportsThinking → 透传
    assert out.get("thinking") == {"type": "enabled", "budget_tokens": 256}
    assert "messages" in out


# ============================================================
# T6 —— 上游错误码标准化（广化枚举，§9-2 单元层）
# ============================================================
OPENROUTER_BODY = (
    '{"code":502,"message":"Upstream error from Nvidia: '
    'ResourceExhausted: Worker local total request limit reached (33/32)",'
    '"metadata":{"error_type":"provider_unavailable"}}'
)
OPENROUTER_RATE_LIMITED_BODY = (
    '{"error":{"message":"Provider returned error","code":429,'
    '"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. '
    'Please retry shortly","provider_name":"Google AI Studio","is_byok":false,'
    '"provider_error_code":"429","limit_source":"upstream_provider_shared_pool"}}'
)
NVIDIA_BODY = "ResourceExhausted: Worker local total request limit reached (32/32)"
STANDARD_OAI_BODY = (
    '{"error":{"message":"ResourceExhausted: Worker local total request limit reached (32/32)",'
    '"type":"rate_limit_error"}}'
)
NON_RATE_BODY = '{"error":{"message":"invalid api key","type":"authentication_error"}}'
UNKNOWN_BODY = '{"error":{"message":"some unrecognized condition","type":"mystery_error"}}'


def test_error_map_table_driven():
    """配置表驱动：多网关限流格式 + 新增 keyword 都能识别，非限流不误判。"""
    assert _srv._map_upstream_error(OPENROUTER_BODY) == (429, "rate_limit_error")
    assert _srv._map_upstream_error(OPENROUTER_RATE_LIMITED_BODY) == (429, "rate_limit_error")
    assert _srv._map_upstream_error(NVIDIA_BODY) == (429, "rate_limit_error")
    assert _srv._map_upstream_error(STANDARD_OAI_BODY) == (429, "rate_limit_error")
    _srv._VENDOR_ERROR_MAPS.append(("quota_exceeded", 429, "rate_limit_error", "测试扩展"))
    try:
        assert _srv._map_upstream_error('{"error":{"message":"quota_exceeded"}}') == (429, "rate_limit_error")
    finally:
        _srv._VENDOR_ERROR_MAPS.pop()
    assert _srv._map_upstream_error(NON_RATE_BODY) == (401, "authentication_error")
    assert _srv._map_upstream_error(UNKNOWN_BODY) is None, "未知 error.type 被误判"
    assert _srv._map_upstream_error("") is None
    assert _srv._vendor_body_retryable(OPENROUTER_BODY) is True
    assert _srv._vendor_body_retryable(OPENROUTER_RATE_LIMITED_BODY) is True


# ============================================================
# T7 —— 流式路径错误标准化（§9-2 流式侧）
# ============================================================
_MODELS_CFG_STREAM = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "msg-stream-test",
            "target": {
                "port": STREAM_PORT,
                "model": STREAM_MODEL,
                "protocol": "messages",
            },
        }
    ],
}


def test_stream_upstream_429_rate_limit_mapped():
    """上游流式路径返回 429（body 命中 ResourceExhausted 特征）→ 标准化 429 + rate_limit_error + Retry-After。"""
    body = b'{"error":{"message":"ResourceExhausted: quota exceeded"}}'
    resp = FakeResponse(429, body=body)
    status, hdrs, raw, calls, _ = run_case([resp], _req("msg-stream-test", stream=True), models_cfg=_MODELS_CFG_STREAM)
    assert status == 429, f"期望 429，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "rate_limit_error", parsed
    assert hdrs.get("retry-after") is not None, f"缺 Retry-After header: {hdrs}"


def test_stream_upstream_503_overloaded_mapped():
    """上游流式 503 + overloaded 文案 → 映射为 Anthropic overloaded_error。"""
    body = b'{"error":{"message":"upstream overloaded, please retry"}}'
    resp = FakeResponse(503, body=body)
    status, hdrs, raw, calls, _ = run_case([resp], _req("msg-stream-test", stream=True), models_cfg=_MODELS_CFG_STREAM)
    assert status == 503, f"期望 503，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "overloaded_error", parsed


def test_stream_upstream_non_mapped_error_falls_back_to_passthrough_style():
    """上游流式非 2xx 但 body 不命中映射表 → 保留状态码，Anthropic 格式 error 信封，不被误伤。"""
    body = b'{"type":"weird_error","error":{"type":"weird_error","message":"unrecognized upstream failure"}}'
    resp = FakeResponse(521, body=body)
    status, hdrs, raw, calls, _ = run_case([resp], _req("msg-stream-test", stream=True), models_cfg=_MODELS_CFG_STREAM)
    assert status == 521, f"期望原状态码 521 透传，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("message") == "unrecognized upstream failure", parsed


def test_stream_embedded_error_event_detected_and_mapped():
    """伪装成功响应：上游 200 但 SSE 含 event: error 帧 → 检测并转译为标准 Anthropic 错误。"""
    sse_chunks = [
        b'event: message_start\ndata: {"type":"message_start"}\n\n',
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"upstream overloaded (embedded-200)"}}\n\n',
    ]
    resp = FakeResponse(200, chunks=sse_chunks)
    status, hdrs, raw, calls, _ = run_case([resp], _req("msg-stream-test", stream=True), models_cfg=_MODELS_CFG_STREAM)
    assert status == 503, f"内嵌 error 帧应转译为 503，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "overloaded_error", parsed
    assert "embedded-200" in parsed.get("error", {}).get("message", ""), parsed


def test_stream_malformed_non_error_frame_passes_through_unchanged():
    """畸形/不可解析帧混杂在正常帧之间 → 原样透传不中断（fail-open 保留）。"""
    sse_chunks = [
        b'event: message_start\ndata: {"type":"message_start"}\n\n',
        b"this is not json and not an error event, just garbage\xff\xfe\n\n",
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    resp = FakeResponse(200, chunks=sse_chunks)
    status, hdrs, raw, calls, _ = run_case([resp], _req("msg-stream-test", stream=True), models_cfg=_MODELS_CFG_STREAM)
    assert status == 200, f"畸形帧不应中断流，期望 200，实到 {status}"
    raw_str = raw.decode("utf-8", errors="replace")
    assert "this is not json and not an error event, just garbage" in raw_str, \
        f"畸形帧应原样透传，未在输出中找到: {raw_str!r}"
    assert "hello" in raw_str, f"正常帧内容丢失: {raw_str!r}"
    assert "event: message_stop" in raw_str, f"结尾帧丢失: {raw_str!r}"


# ============================================================
# T8 —— Anthropic 帧 event:error 流内哨兵守护
# ============================================================
_MODELS_CFG_GUARD = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "sse-guard-test",
            "target": {
                "port": GUARD_PORT,
                "model": GUARD_MODEL,
                "protocol": "messages",
            },
        }
    ],
}

NORMAL_ANTHROPIC_FRAMES = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant"}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]

ERROR_FRAME_CHUNKS = NORMAL_ANTHROPIC_FRAMES[:2] + [
    b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"upstream overloaded (guard test)"}}\n\n',
]


def test_guard_off_stream_passes_through_unchanged():
    """guard OFF（默认）→ 即便内嵌 event: error 帧，由既有 #7 内联检测翻译；guard 本身 inert。"""
    resp = FakeResponse(200, chunks=ERROR_FRAME_CHUNKS)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=False
    )
    assert status == 503, f"guard OFF 时应仍由既有 #7 逻辑翻译为 503，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "overloaded_error", parsed
    assert "guard test" in parsed.get("error", {}).get("message", ""), parsed


def test_guard_off_normal_frames_pass_through_byte_identical():
    """guard OFF + 正常帧序列 → 完全透传，字节内容与帧顺序不变。"""
    resp = FakeResponse(200, chunks=NORMAL_ANTHROPIC_FRAMES)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=False
    )
    assert status == 200, f"期望 200 透传，实到 {status}"
    expected = b"".join(NORMAL_ANTHROPIC_FRAMES)
    assert raw == expected, f"guard OFF 时正常帧应字节透传不变:\n实到: {raw!r}\n期望: {expected!r}"


def test_guard_on_intercepts_error_frame_via_t6_map():
    """开关 ON：guard 在 #7 既有检测之前扫描到 event: error 帧，按 T6 映射表翻译。"""
    resp = FakeResponse(200, chunks=ERROR_FRAME_CHUNKS)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=True
    )
    assert status == 503, f"overloaded_error 应映射为 503，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("type") == "error", parsed
    assert parsed.get("error", {}).get("type") == "overloaded_error", parsed
    assert "guard test" in parsed.get("error", {}).get("message", ""), parsed


def test_guard_on_intercepts_authentication_error_frame():
    """开关 ON + authentication_error 帧（T6 枚举覆盖）→ 401。"""
    chunks = NORMAL_ANTHROPIC_FRAMES[:1] + [
        b'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"invalid api key"}}\n\n',
    ]
    resp = FakeResponse(200, chunks=chunks)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=True
    )
    assert status == 401, f"authentication_error 应映射为 401，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "authentication_error", parsed


def test_guard_on_normal_frames_pass_through_byte_identical():
    """关键反回归：guard ON 时正常 Anthropic 帧序列必须原样字节透传——不触碰非 error 帧。"""
    resp = FakeResponse(200, chunks=NORMAL_ANTHROPIC_FRAMES)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=True
    )
    assert status == 200, f"正常帧不应被拦截，期望 200，实到 {status}"
    expected = b"".join(NORMAL_ANTHROPIC_FRAMES)
    assert raw == expected, f"guard ON 时正常帧必须字节透传不变:\n实到: {raw!r}\n期望: {expected!r}"


def test_guard_on_unrecognized_malformed_frame_still_passes_through():
    """开关 ON + 无法识别的畸形终止帧 → fail-open 保留，guard 判定为 None，原样透传不中断。"""
    chunks = NORMAL_ANTHROPIC_FRAMES[:2] + [
        b"totally unrecognized garbage tail, not json, no known vendor keyword\xff\xfe\n\n",
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    resp = FakeResponse(200, chunks=chunks)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=True
    )
    assert status == 200, f"无法识别的畸形帧应 fail-open 透传，期望 200，实到 {status}"
    raw_str = raw.decode("utf-8", errors="replace")
    assert "totally unrecognized garbage tail" in raw_str, \
        f"畸形帧应原样透传，未在输出中找到: {raw_str!r}"
    assert "event: message_stop" in raw_str, f"结尾帧丢失: {raw_str!r}"


# ============================================================
# 新增 bundle §9 补全 + 交互测试（合并后新增，单任务未覆盖）
# ============================================================

def test_unrouted_model_returns_404():
    """§9-3：models[] 未命中（legacy 单端口模式已下线，无兜底路径）→ 显式 404。"""
    resp = FakeResponse(200, body=b'{"type":"message","content":[]}')
    # 默认空 models 配置：任何模型都不命中
    status, _, raw, calls, _ = run_case([resp], _req("ghost-model-not-configured"))
    assert status == 404, f"未路由模型应返回 404，实到 {status}"
    assert calls == [], "未命中路由时不应向下游发起任何连接"


def test_nonstream_upstream_429_mapped_to_rate_limit():
    """§9-2 非流式侧：上游 429 + ResourceExhausted → 标准化 429 + rate_limit_error + Retry-After。"""
    body = b'{"error":{"message":"ResourceExhausted: quota exceeded"}}'
    resp = FakeResponse(429, body=body)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("msg-stream-test"), models_cfg=_MODELS_CFG_STREAM
    )
    assert status == 429, f"期望 429，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "rate_limit_error", parsed
    assert parsed.get("error", {}).get("original_status") == 429, parsed
    assert hdrs.get("retry-after") is not None, f"非流式也应带 Retry-After: {hdrs}"


def test_nonstream_upstream_authentication_error_mapped_to_401():
    """§9-2 非流式侧（更广枚举）：上游 401 + authentication_error → 标准化 401。"""
    body = b'{"error":{"type":"authentication_error","message":"invalid api key"}}'
    resp = FakeResponse(401, body=body)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("msg-stream-test"), models_cfg=_MODELS_CFG_STREAM
    )
    assert status == 401, f"authentication_error 应标准化为 401，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "authentication_error", parsed
    assert hdrs.get("retry-after") is not None, f"应带 Retry-After: {hdrs}"


def test_stream_upstream_authentication_error_frame_mapped_to_401():
    """§9-2 流式侧（更广枚举，guard OFF 走 #7 内联检测）：内嵌 authentication_error 帧 → 401。"""
    chunks = NORMAL_ANTHROPIC_FRAMES[:1] + [
        b'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"invalid api key"}}\n\n',
    ]
    resp = FakeResponse(200, chunks=chunks)
    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("sse-guard-test", stream=True), models_cfg=_MODELS_CFG_GUARD, guard_enabled=False
    )
    assert status == 401, f"流式内嵌 authentication_error 应映射为 401，实到 {status}"
    parsed = json.loads(raw)
    assert parsed.get("error", {}).get("type") == "authentication_error", parsed


def test_passthrough_forwards_thinking_field():
    """§9-1 集成层：外部 secured target（无 profile → fail-open）转发时 thinking 字段随请求体透传。"""
    body = b'{"type":"message","content":[{"type":"text","text":"ok"}]}'
    resp = FakeResponse(200, body=body)
    req_body = _req(
        "ext-msg-secured",
        thinking={"type": "enabled", "budget_tokens": 1024},
    )
    status, _, _, calls, _ = run_case(
        [resp], req_body, _MODELS_CFG_WITH_APIKEY
    )
    assert status == 200, f"期望 200，实到 {status}"
    fwd = json.loads(_call_content(calls[0]))
    assert fwd.get("thinking") == {"type": "enabled", "budget_tokens": 1024}, \
        f"thinking 必须随透传请求体转发到下游: {fwd}"


def test_secured_target_auth_plus_capability_profile_combined():
    """交互测试（§9-4 + §9-6 + §9-1）：外部 target 同时配置 apikey 与 messagesProfile。

    - 鉴权注入：转发 authorization 用目标凭据，客户端 secret 不泄露（§9-4）
    - 能力门控：profile.supportsTopK=false → 转发请求体剥离 top_k（§9-6）
    - 能力门控不误伤：profile 未声明 supportsThinking → thinking 照常透传（§9-1）
    """
    # target 在 _MODELS_CFG 中带 apikey（触发鉴权注入）；profile 查 _TARGETS 按 listenPort
    combo_cfg = {
        "modelDefaults": {"defaultPort": 8082},
        "models": [
            {
                "name": "combo-secured",
                "target": {
                    "port": COMBO_PORT,
                    "model": COMBO_MODEL,
                    "protocol": "messages",
                    "apikey": "sk-target-secret",
                },
            }
        ],
    }
    combo_targets = [
        {
            "listenPort": COMBO_PORT,
            "messagesProfile": {"supportsTopK": False},
        }
    ]

    body = b'{"type":"message","content":[{"type":"text","text":"ok"}]}'
    resp = FakeResponse(200, body=body)
    req_body = _req(
        "combo-secured",
        top_k=40,
        thinking={"type": "enabled", "budget_tokens": 256},
    )
    status, _, _, calls, _ = run_case(
        [resp], req_body,
        models_cfg=combo_cfg,
        models_targets=combo_targets,
        headers=_client_headers(),
    )
    assert status == 200, f"期望 200，实到 {status}"

    # §9-4 鉴权注入 + 失败闭合
    sent_headers = _call_headers(calls[0])
    assert _get_ci(sent_headers, "authorization") == "Bearer sk-target-secret", \
        f"authorization 应被目标凭据覆盖，实到 {_get_ci(sent_headers, 'authorization')!r}"
    assert _get_ci(sent_headers, "x-api-key") is None, "客户端 x-api-key 不应泄露"
    assert _get_ci(sent_headers, "cookie") is None, "客户端 cookie 不应泄露"

    # §9-6 能力门控剥离 top_k；§9-1 thinking 在 profile 未禁用时透传
    fwd = json.loads(_call_content(calls[0]))
    assert "top_k" not in fwd, f"supportsTopK=false 时 top_k 必须被剥离: {fwd}"
    assert fwd.get("thinking") == {"type": "enabled", "budget_tokens": 256}, \
        f"thinking 在 profile 未禁用时应透传: {fwd}"
    assert "messages" in fwd
