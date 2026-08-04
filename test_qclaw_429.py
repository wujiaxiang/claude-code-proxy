"""验证上游限流错误被正确转成 429 / SSE error 事件，覆盖多网关多格式 + 配置表驱动。

覆盖：
1. 错误码映射表 _VENDOR_ERROR_MAPS / _map_upstream_error：
   - openrouter: {"code":502,"message":"...ResourceExhausted..."}  (无 "error": 字段)
   - nvidia:     裸字符串 "ResourceExhausted: Worker local total request limit reached (32/32)"
   - 标准 OAI:   {"error":{"type":"rate_limit_error",...}}
   - 配置表驱动：新增一行 keyword（如 "quota_exceeded"）即可被识别
   - 非限流（如 authentication_error）不被误判
2. LiteLLM 翻译路径（qclaw/copilot/anthropic/gemini）：litellm 抛 RateLimitError
   → openai_chat_completions 返回 HTTP 429 + Retry-After
3. LiteLLM 非限流异常 → 500 不变

用 FastAPI TestClient 但不进入 with 上下文（避免触发 lifespan 绑定端口）。
"""
from unittest.mock import patch, AsyncMock

import litellm
from fastapi.testclient import TestClient

import server


# ── 各网关错误体样例（来自真实上游） ──
OPENROUTER_BODY = (
    '{"code":502,"message":"Upstream error from Nvidia: '
    'ResourceExhausted: Worker local total request limit reached (33/32)",'
    '"metadata":{"error_type":"provider_unavailable"}}'
)
NVIDIA_BODY = "ResourceExhausted: Worker local total request limit reached (32/32)"
STANDARD_OAI_BODY = (
    '{"error":{"message":"ResourceExhausted: Worker local total request limit reached (32/32)",'
    '"type":"rate_limit_error"}}'
)
NON_RATE_BODY = '{"error":{"message":"invalid api key","type":"authentication_error"}}'


def _make_rate_limit_error():
    return litellm.RateLimitError(
        "ResourceExhausted: Worker local total request limit reached (32/32)",
        llm_provider="qclaw",
        model="some-model",
    )


def test_error_map_table_driven():
    """配置表驱动：三种限流格式 + 新增 keyword 都能识别，非限流不误判。"""
    assert server._map_upstream_error(OPENROUTER_BODY) == (429, "rate_limit_error")
    assert server._map_upstream_error(NVIDIA_BODY) == (429, "rate_limit_error")
    assert server._map_upstream_error(STANDARD_OAI_BODY) == (429, "rate_limit_error")
    # 配置表驱动：临时追加一行 keyword 即被识别（验证"加表项即可扩展"）
    server._VENDOR_ERROR_MAPS.append(("quota_exceeded", 429, "rate_limit_error", "测试扩展"))
    try:
        assert server._map_upstream_error('{"error":{"message":"quota_exceeded"}}') == (429, "rate_limit_error")
    finally:
        server._VENDOR_ERROR_MAPS.pop()
    assert server._map_upstream_error(NON_RATE_BODY) is None, "非限流被误判"
    assert server._map_upstream_error("") is None
    assert server._vendor_body_retryable(OPENROUTER_BODY) is True
    print("[PASS] 配置表驱动：openrouter/nvidia/标准OAI + 扩展keyword 均识别，非限流不误判")


def test_litellm_rate_limit_returns_429():
    orig_provider = server.PREFERRED_PROVIDER
    server.PREFERRED_PROVIDER = "anthropic"
    try:
        with patch.object(
            server.litellm, "acompletion", new=AsyncMock(side_effect=_make_rate_limit_error())
        ):
            client = TestClient(server.app)
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 429, f"期望 429，实际 {resp.status_code}: {resp.text}"
        assert resp.headers.get("retry-after"), "缺少 Retry-After 头"
        print(f"[PASS] LiteLLM 限流 → 429, Retry-After={resp.headers.get('retry-after')}")
    finally:
        server.PREFERRED_PROVIDER = orig_provider


def test_litellm_non_rate_limit_stays_500():
    orig_provider = server.PREFERRED_PROVIDER
    server.PREFERRED_PROVIDER = "anthropic"
    try:
        with patch.object(
            server.litellm, "acompletion", new=AsyncMock(side_effect=ValueError("boom"))
        ):
            client = TestClient(server.app)
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 500, f"期望 500，实际 {resp.status_code}: {resp.text}"
        print("[PASS] LiteLLM 非限流异常 → 500 (保持不变)")
    finally:
        server.PREFERRED_PROVIDER = orig_provider


if __name__ == "__main__":
    test_error_map_table_driven()
    test_litellm_rate_limit_returns_429()
    test_litellm_non_rate_limit_stays_500()
    print("ALL TESTS PASSED")
