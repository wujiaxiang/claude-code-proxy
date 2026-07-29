"""
Anthropic ↔ OpenAI 协议转换（最小可用集，给 8081 asyncio TCP handler 用）

只支持 copilot provider 路径实际需要的映射，不做完整兼容。
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
