"""
Anthropic ↔ OpenAI 协议转换（最小可用集，给 8081 /v1/messages FastAPI 路由 models[] 映射分支用）

只支持 copilot provider 路径实际需要的映射，不做完整兼容。

刻意不合并说明
----------------
本模块的 convert_anthropic_request_to_openai / convert_openai_response_to_anthropic
与 gateways/translate.py 的 _convert_oai_to_anthropic 都做 Anthropic↔OpenAI 互转，
但两者调用形状根本不同，故**刻意不合并**（non-merge）：

- 调用形状不同：本模块是 raw dict 对（无 request 对象、无 LiteLLM 参与），
  直接服务 server.py `create_message` 的「分支 A」——即 models[] 命中本地端口时的
  直发路径。而 gateways/translate.py:_convert_oai_to_anthropic(oai_data, request, original_model)
  需要 Pydantic request 对象，服务 LiteLLM / provider-strategy 管道（「分支 C」）。
- 不合并理由：强行合并会让「简单隔离的 dict→dict 转换路径」与复杂的
  LiteLLM/provider 机制耦合，引入双向依赖与回归风险，DRY 收益远小于此代价。
- 交叉引用：若未来需要统一 Anthropic 反向转换，请先看
  gateways/translate.py:_convert_oai_to_anthropic（按名搜索定位），
  确认其输入契约（request 对象 / LiteLLM 上下文）后再评估，勿盲目合并本模块。
"""

import json
import time
import uuid


def convert_anthropic_request_to_openai(anthropic_body: dict) -> dict:
    """Anthropic /v1/messages 请求体 → OpenAI /v1/chat/completions 请求体"""
    messages = []

    # system prompt
    system = anthropic_body.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                messages.append({"role": "system", "content": "\n\n".join(text_parts)})

    # conversation messages
    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # content blocks → extract text
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") == "tool_use":
                        parts.append({
                            "type": "function",
                            "id": block.get("id", ""),
                            "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}))},
                        })
                    elif block.get("type") == "tool_result":
                        parts.append({"type": "text", "text": block.get("content", "")})
                elif isinstance(block, str):
                    parts.append({"type": "text", "text": block})
            messages.append({"role": role, "content": parts})
        else:
            messages.append({"role": role, "content": str(content)})

    openai_req = {
        "model": anthropic_body.get("model", "claude-sonnet-5"),
        "messages": messages,
        "max_completion_tokens": anthropic_body.get("max_tokens", 4096),
    }

    # optional fields
    if anthropic_body.get("temperature") is not None:
        openai_req["temperature"] = anthropic_body["temperature"]
    if anthropic_body.get("top_p") is not None:
        openai_req["top_p"] = anthropic_body["top_p"]
    if anthropic_body.get("top_k") is not None:
        openai_req["top_k"] = anthropic_body["top_k"]

    # stream
    if anthropic_body.get("stream"):
        openai_req["stream"] = True
        openai_req["stream_options"] = {"include_usage": True}

    # tools
    tools = anthropic_body.get("tools")
    if tools:
        openai_tools = []
        for t in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })
        openai_req["tools"] = openai_tools

    return openai_req


def convert_openai_response_to_anthropic(openai_data: dict, original_model: str) -> dict:
    """OpenAI /v1/chat/completions 非流式响应 → Anthropic /v1/messages 响应"""
    choice = (openai_data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")

    # build Anthropic content blocks
    content_blocks = []
    if isinstance(content, str):
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    content_blocks.append({"type": "text", "text": item.get("text", "")})
                elif item.get("type") == "function":
                    # tool use
                    try:
                        args = json.loads(item.get("function", {}).get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": item.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                        "name": item.get("function", {}).get("name", ""),
                        "input": args,
                    })

    usage = openai_data.get("usage", {})
    return {
        "id": openai_data.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
        "type": "message",
        "role": "assistant",
        "model": openai_data.get("model", original_model),
        "content": content_blocks,
        "stop_reason": choice.get("finish_reason", "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0) or usage.get("output_tokens", 0),
        },
    }
