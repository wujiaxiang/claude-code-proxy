"""Broaden _VENDOR_ERROR_MAPS to cover Anthropic's full error.type enum.

TDD 步骤 1/2：先写失败测试，再实现。
- 每个新 error.type 至少一条代表性上游错误体 → 断言正确 (status, error_type)
- 回归：现有 6 条 429 限流映射 + 空串 + 未识别体仍返回 None 不被破坏

对应 Task 6：把 gateways/errors.py:_VENDOR_ERROR_MAPS 从 429-only 扩展到
authentication_error / invalid_request_error / permission_error / not_found_error /
overloaded_error / timeout_error / api_error。
"""
from gateways.errors import _map_upstream_error, _vendor_body_retryable


# ── 各新 error.type 的代表性上游错误体（来自真实网关常见格式）──
AUTH_BODY = (
    '{"type":"error","error":{'
    '"type":"authentication_error",'
    '"message":"invalid x-api-key provided"}}'
)
INVALID_REQUEST_BODY = (
    '{"error":{"type":"invalid_request_error",'
    '"message":"messages is a required field"}}'
)
PERMISSION_BODY = (
    '{"error":{"type":"permission_error",'
    '"message":"you do not have permission to access this model"}}'
)
FORBIDDEN_BODY = '{"error":{"message":"forbidden: access denied"}}'
NOT_FOUND_BODY = (
    '{"error":{"type":"not_found_error",'
    '"message":"model not found"}}'
)
MODEL_NOT_FOUND_BODY = '{"error":{"message":"Model not found: gpt-5"}}'
OVERLOADED_BODY = (
    '{"error":{"type":"overloaded_error",'
    '"message":"overloaded"}}'
)
OVERLOADED_TEXT_BODY = "Upstream is overloaded, please retry later"
TIMEOUT_BODY = (
    '{"error":{"type":"timeout_error",'
    '"message":"upstream request timed out"}}'
)
DEADLINE_BODY = "6 DEADLINE_EXCEEDED: deadline exceeded"
API_ERROR_BODY = (
    '{"error":{"type":"api_error",'
    '"message":"internal server error"}}'
)
INTERNAL_ERROR_BODY = '{"error":{"message":"Internal server error"}}'


def test_authentication_error_maps_to_401():
    assert _map_upstream_error(AUTH_BODY) == (401, "authentication_error")
    assert _map_upstream_error('{"error":{"message":"Invalid API key"}}') == (401, "authentication_error")


def test_invalid_request_error_maps_to_400():
    assert _map_upstream_error(INVALID_REQUEST_BODY) == (400, "invalid_request_error")


def test_permission_error_maps_to_403():
    assert _map_upstream_error(PERMISSION_BODY) == (403, "permission_error")
    assert _map_upstream_error(FORBIDDEN_BODY) == (403, "permission_error")


def test_not_found_error_maps_to_404():
    assert _map_upstream_error(NOT_FOUND_BODY) == (404, "not_found_error")
    assert _map_upstream_error(MODEL_NOT_FOUND_BODY) == (404, "not_found_error")


def test_overloaded_error_maps_to_503():
    assert _map_upstream_error(OVERLOADED_BODY) == (503, "overloaded_error")
    assert _map_upstream_error(OVERLOADED_TEXT_BODY) == (503, "overloaded_error")


def test_timeout_error_maps_to_504():
    assert _map_upstream_error(TIMEOUT_BODY) == (504, "timeout_error")
    assert _map_upstream_error(DEADLINE_BODY) == (504, "timeout_error")


def test_api_error_maps_to_500():
    assert _map_upstream_error(API_ERROR_BODY) == (500, "api_error")
    assert _map_upstream_error(INTERNAL_ERROR_BODY) == (500, "api_error")


def test_unknown_nonretryable_still_none():
    # 不属于任何已知 error.type 的体仍返回 None
    assert _map_upstream_error('{"error":{"message":"some weird thing"}}') is None
    assert _map_upstream_error("totally unrelated text") is None
    assert _map_upstream_error("") is None


def test_retryable_flag_reflects_mapping():
    # 仅被识别为限流/错误信封的体才 retryable；未识别的不可重试
    assert _vendor_body_retryable(AUTH_BODY) is True
    assert _vendor_body_retryable(OVERLOADED_BODY) is True
    assert _vendor_body_retryable('{"error":{"message":"unknown"}}') is False


def test_existing_rate_limit_mappings_regression():
    # 回归：扩展后现有 6 条 429 限流映射必须仍正确解析（无回归）
    assert _map_upstream_error(
        '{"code":502,"message":"Upstream error from Nvidia: '
        'ResourceExhausted: Worker local total request limit reached (33/32)"}'
    ) == (429, "rate_limit_error")
    assert _map_upstream_error(
        'ResourceExhausted: Worker local total request limit reached (32/32)'
    ) == (429, "rate_limit_error")
    assert _map_upstream_error(
        '{"error":{"message":"rate_limit_exceeded","type":"rate_limit_error"}}'
    ) == (429, "rate_limit_error")
    assert _map_upstream_error(
        '{"error":{"message":"temporarily rate-limited upstream"}}'
    ) == (429, "rate_limit_error")


if __name__ == "__main__":
    test_authentication_error_maps_to_401()
    test_invalid_request_error_maps_to_400()
    test_permission_error_maps_to_403()
    test_not_found_error_maps_to_404()
    test_overloaded_error_maps_to_503()
    test_timeout_error_maps_to_504()
    test_api_error_maps_to_500()
    test_unknown_nonretryable_still_none()
    test_retryable_flag_reflects_mapping()
    test_existing_rate_limit_mappings_regression()
    print("ALL TESTS PASSED")
