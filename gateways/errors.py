"""错误翻译层：上游错误码映射、限流判断、认证过期检测。

从 server.py 下沉，保持行为零变化。server.py 通过 `from gateways.errors import ...` 重新导出，
保证 `test_error_code_mapping.py` 等测试与内部调用继续有效。
"""

import os
import re
from typing import Optional, Tuple

# ─── 限流/资源耗尽错误特征（复用 _VENDOR_ERROR_MAPS 的 keyword，单点维护）───
# 这些关键字同时被 _is_rate_limit_error、_map_upstream_error 复用。

# ─── 上游错误码映射表（数据驱动，新增网关只需追加一行）───
# 透传网关遇到下列「字段特征」（子串匹配，大小写敏感）即把上游错误体
# 标准化为 (目标 HTTP 状态码, SSE error type)，让下游客户端（opencode 等）
# 按标准错误重试/降级，而不是把伪成功/5xx 透传导致 UnknownError。
# 字段特征, 目标状态码, SSE error type, 说明
_VENDOR_ERROR_MAPS = [
    ("ResourceExhausted", 429, "rate_limit_error", "qclaw/nvidia/openrouter 资源耗尽（并发限制）"),
    ("Worker local total request limit reached", 429, "rate_limit_error", "nvidia/openrouter 本地并发已满"),
    ("rate_limit_exceeded", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("too_many_requests", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("RateLimitError", 429, "rate_limit_error", "litellm 限流异常类名"),
    ("rate-limited", 429, "rate_limit_error", "openrouter 免费池上游限流（temporarily rate-limited upstream）"),
]

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


def _is_rate_limit_error(exc: Exception) -> bool:
    """识别 LiteLLM 抛出的限流类异常（含 qclaw 的 ResourceExhausted / Worker local ...）。

    命中后调用方应返回 HTTP 429 + Retry-After，让下游客户端（如 opencode）自动重试。
    关键字复用 _VENDOR_ERROR_MAPS，保持单点维护。
    """
    from litellm.exceptions import RateLimitError as _LiteLLMRateLimitError  # litellm.RateLimitError 的同一个类，走公开导出路径
    import litellm
    if isinstance(exc, (_LiteLLMRateLimitError, getattr(litellm, "RouterRateLimitError", ()))):
        return True
    text = str(exc)
    if not text:
        return False
    return any(k in text for k, _s, _t, _d in _VENDOR_ERROR_MAPS)


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
    "_is_rate_limit_error",
    "_VENDOR_ERROR_MAPS",
    "_VENDOR_ERROR_PATTERNS",
    "_VENDOR_RETRY_AFTER",
    "_map_upstream_error",
    "_vendor_body_retryable",
]