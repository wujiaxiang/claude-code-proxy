# Open-Go 网关模块（muse-spark 走 Responses API，借鉴 8082 copilot 网关）
# muse-spark-1.2-contributor 在 opencode.ai 上仅支持 /v1/responses，不支持 /v1/chat/completions（500）。
# 网关对该模型做 chat→responses 双向翻译，其余模型保持 passthrough。
import json

# 复用 copilot 的 Responses 双向转换（OpenAI 标准 Responses 协议，两家上游一致）
from gateways.copilot import (
    _copilot_chat_to_responses_body,
    _copilot_responses_to_chat_body,
    _write_copilot_responses_stream,
)


def _open_go_is_responses_model(model: str) -> bool:
    """判断是否需走 Responses 协议。当前仅 muse-spark 系列。"""
    return isinstance(model, str) and model.startswith("muse-spark")


def open_go_should_use_responses(target: dict, raw_path: str, model: str | None, body_json: dict | None) -> bool:
    """供 server.py 在 _handle_target_request 中判定是否启用翻译。

    覆盖同为 opencode.ai 上游的两类 target：
    - handler=="copilot"（8082）：由 server.py 的 copilot 分支自行处理，此处不接管；
    - 其他 handler：显式配置 responsesModels 名单的按名单桥接
      （8093 opencode-zen 的 muse-spark-contributor-free 只支持 /responses，
      而 8080 聚合层统一用 Chat Completions 调成员端口，故必须桥接）；
      open-go（8094）在未配名单时仍按 muse-spark 前缀兜底，保持旧行为。
    """
    if target.get("handler") == "copilot":
        return False
    if raw_path not in ("/v1/chat/completions", "/chat/completions"):
        return False
    if not model or not isinstance(model, str):
        return False
    if body_json is None or not isinstance(body_json, dict):
        return False
    # 优先尊重 target 的 responsesModels 显式名单，其次 open-go 按前缀兜底
    explicit = target.get("responsesModels")
    if explicit is not None:
        return model in explicit
    return target.get("handler") == "open-go" and _open_go_is_responses_model(model)


def open_go_chat_to_responses(body: dict) -> dict:
    out = _copilot_chat_to_responses_body(body)
    # Muse 在 Go 网关上要求 max_output_tokens >= 16，否则直接 400
    # 上游错误：`max_output_tokens` The number must be `>= 16`.
    # 代理侧兜底：过小的请求按最小值 16 提升，避免把客户端的 4/10 透传导致必失败
    if "max_output_tokens" in out and isinstance(out["max_output_tokens"], int) and out["max_output_tokens"] < 16:
        out["max_output_tokens"] = 16
    # Go 网关对 muse 的 reasoning.effort 仅接受 none/minimal/low/medium/high，不接受 max
    # 上游错误：`reasoning.effort`: unknown variant `max`, expected one of `none`, `minimal`, `low`, `medium`, `high`
    # 代理侧兜底：max → high
    if isinstance(out.get("reasoning"), dict) and out["reasoning"].get("effort") == "max":
        out["reasoning"]["effort"] = "high"
    return out


def open_go_responses_to_chat(resp: dict, model: str) -> dict:
    return _copilot_responses_to_chat_body(resp, model)


async def open_go_write_responses_stream(writer, resp, model: str, label: str):
    await _write_copilot_responses_stream(writer, resp, model, label)
