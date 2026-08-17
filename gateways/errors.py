"""错误翻译层：上游错误码映射、限流判断、认证过期检测。

从 server.py 下沉，保持行为零变化。server.py 通过 `from gateways.errors import ...` 重新导出，
保证 `test_error_code_mapping.py` 等测试与内部调用继续有效。
"""

import os
import re
from typing import Optional, Tuple

# ─── 上游错误码映射表（数据驱动，新增网关只需追加一行）───
# 透传网关遇到下列「字段特征」（子串匹配，大小写敏感）即把上游错误体
# 标准化为 (目标 HTTP 状态码, SSE error type)，让下游客户端（opencode 等）
# 按标准错误重试/降级，而不是把伪成功/5xx 透传导致 UnknownError。
# 分为两段：上方限流段（429）保持既有行为；下方 _ANTHROPIC_ERROR_MAPS
# 覆盖 Anthropic 标准 error.type 枚举（401/400/403/404/503/504/500）。
# 字段特征, 目标状态码, SSE error type, 说明
_VENDOR_ERROR_MAPS = [
    ("ResourceExhausted", 429, "rate_limit_error", "qclaw/nvidia/openrouter 资源耗尽（并发限制）"),
    ("Worker local total request limit reached", 429, "rate_limit_error", "nvidia/openrouter 本地并发已满"),
    ("rate_limit_exceeded", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("too_many_requests", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("RateLimitError", 429, "rate_limit_error", "litellm 限流异常类名"),
    ("rate-limited", 429, "rate_limit_error", "openrouter 免费池上游限流（temporarily rate-limited upstream）"),
]

# ─── 扩展：Anthropic 标准 error.type 枚举（非限流类）───
# 把上游常见错误体标准化为对应 HTTP 状态码 + 标准 SSE error type，
# 让下游客户端（opencode 等）按标准错误分类处理（认证失败/参数错误/权限/
# 未找到/过载/超时/内部错误），而不是把 5xx/4xx 原样透传成 UnknownError。
# 仅做子串匹配（大小写敏感），与上方限流表同一机制；新增网关只需追加一行。
# 字段特征, 目标状态码, SSE error type, 说明
_ANTHROPIC_ERROR_MAPS = [
    ("authentication_error", 401, "authentication_error", "Anthropic 认证错误（无效/缺失 API key）"),
    ("Invalid API key", 401, "authentication_error", "OpenAI 风格认证错误文案"),
    ("invalid_request_error", 400, "invalid_request_error", "Anthropic 请求参数错误"),
    ("permission_error", 403, "permission_error", "Anthropic 权限错误（无访问权限）"),
    ("forbidden", 403, "permission_error", "网关 403 权限错误文案"),
    ("not_found_error", 404, "not_found_error", "Anthropic 资源未找到（模型/端点不存在）"),
    ("Model not found", 404, "not_found_error", "上游模型不存在文案"),
    ("overloaded_error", 503, "overloaded_error", "Anthropic 过载（服务不可用）"),
    ("overloaded", 503, "overloaded_error", "上游过载文案（nvidia/openrouter 5xx 过载）"),
    ("timeout_error", 504, "timeout_error", "Anthropic 上游超时"),
    ("deadline exceeded", 504, "timeout_error", "gRPC/上游 deadline 超时文案"),
    ("api_error", 500, "api_error", "Anthropic 内部服务器错误"),
    ("Internal server error", 500, "api_error", "上游 500 文案"),
]

# 与限流表合并为统一的下游识别表（限流优先，保持既有行为不变）
_VENDOR_ERROR_MAPS = _VENDOR_ERROR_MAPS + _ANTHROPIC_ERROR_MAPS

# ─── 标准 OpenAI error 信封的限流特征正则（回退匹配用）───
_VENDOR_ERROR_PATTERNS = [
    re.compile(r'"ResourceExhausted"'),
    re.compile(r'Worker local total request limit reached', re.IGNORECASE),
    re.compile(r'"(error_)?code"\s*:\s*"?(rate_limit_exceeded|too_many_requests)"?', re.IGNORECASE),
    re.compile(r'"type"\s*:\s*"rate_limit_error"', re.IGNORECASE),
]

# ─── 统一透传引擎配置（环境变量可覆盖）───
_VENDOR_RETRY_AFTER = int(os.environ.get("VENDOR_RETRY_AFTER_SECONDS", "3"))


def _is_auth_expired_error(exc: Exception) -> bool:
    """判断是否为 QClaw 网关 upstream auth 过期 (9002)。"""
    msg = str(exc).lower()
    return "9002" in msg or "该功能暂不可用" in msg


def _map_upstream_error(body_text: str) -> Optional[Tuple[int, str]]:
    """根据错误映射表把上游错误体转成 (http_status, sse_error_type)。

    匹配顺序：
    1. 先按 _VENDOR_ERROR_MAPS 做子串匹配——覆盖无标准 error 信封的格式，如：
       - openrouter: {"code":502,"message":"Upstream error from Nvidia: ResourceExhausted: ..."}
       - nvidia:     裸字符串 "ResourceExhausted: Worker local total request limit reached (32/32)"
    2. 回退到标准 OpenAI error 信封 {"error":{...}} 含 _VENDOR_ERROR_PATTERNS 特征。
    返回 None 表示不是可识别的限流/错误信封。
    """
    if not body_text:
        return None
    for _keyword, _status, _err_type, _desc in _VENDOR_ERROR_MAPS:
        if _keyword in body_text:
            return (_status, _err_type)
    if re.search(r'"error"\s*:', body_text):
        if any(p.search(body_text) for p in _VENDOR_ERROR_PATTERNS):
            return (429, "rate_limit_error")
    return None


def _vendor_body_retryable(body_text: str) -> bool:
    """判断上游错误体是否应被转成可重试的标准错误（429）。"""
    return _map_upstream_error(body_text) is not None


# 导出供 server.py 重新导出的公共符号
__all__ = [
    "_is_auth_expired_error",
    "_VENDOR_ERROR_MAPS",
    "_VENDOR_ERROR_PATTERNS",
    "_VENDOR_RETRY_AFTER",
    "_map_upstream_error",
    "_vendor_body_retryable",
]