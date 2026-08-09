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
    """Anthropic /v1/messages 请求体 → OpenAI /v1/chat/completions 请求体

    消息循环借鉴 LiteLLM（litellm/llms/anthropic/experimental_pass_through/
    adapters/transformation.py）的 bucket 架构：一条 Anthropic 消息可能产生
    多条 OpenAI 消息（tool_result 独立成 role:"tool"），flush 顺序
    tool 消息在前、user 消息在后。assistant 的 tool_use → 顶层 tool_calls。
    """
    # ── token-saver：翻译前压缩超长 tool_result（默认关闭；旁路优化，异常不影响主链路）──
    # compress_tool_results 就地修改 messages，只缩短 tool_result 文本、不改结构，
    # 因此下面的翻译逻辑直接读压缩后的内容即可（刻意不 deepcopy body）。
    try:
        import server as _srv  # 模块属性访问：_TOKEN_SAVER_CFG 是热重载可变全局
        if _srv._TOKEN_SAVER_CFG.get("enabled"):
            from gateways.rtk import compress_tool_results, format_rtk_log
            _rtk = compress_tool_results(anthropic_body.get("messages"))
            if _rtk.get("compressed"):
                _log = format_rtk_log(_rtk.get("stats") or {})
                if _log:
                    _srv.logger.info(_log)
    except Exception:
        pass  # token-saver 是旁路优化：任何异常都静默跳过，绝不破坏请求

    messages = []
    system_out = ""

    # system prompt（多文本块合并成纯字符串；放顶层 system 字段而非 messages[0]——
    # 实测腾讯 codebuddy 上游对 messages[0] 的 role:"system" 消息敏感触发 content_filter，
    # 顶层 system 字段不触发。2026-08-08）
    system = anthropic_body.get("system")
    if system:
        if isinstance(system, str):
            system_out = system
        elif isinstance(system, list):
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                system_out = "\n\n".join(text_parts)

    # conversation messages（bucket 架构：tool 消息独立，user 内容合并）
    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "assistant":
            # assistant：text 块合并成纯字符串 + tool_use → 顶层 tool_calls
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(str(block.get("text", "")))
                        elif block.get("type") == "tool_use":
                            # Anthropic tool_use → OpenAI tool_calls（顶层字段）
                            tool_calls.append({
                                "id": str(block.get("id", "")),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name", "")),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
            assistant_msg: dict[str, object] = {"role": "assistant"}
            assistant_content: str = "".join(text_parts) if text_parts else ""
            assistant_msg["content"] = assistant_content
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
        elif role == "user" and isinstance(content, list):
            # user 消息：text/image 合并到 user content，tool_result 独立成 role:"tool"
            tool_messages = []
            user_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        user_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # tool_result → role:"tool"（必须独立，不能塞 user content）
                        # 单 item 拍平成纯字符串（许多 OpenAI 兼容上游拒绝 list 形式 tool content）
                        tr_content = block.get("content", "")
                        if isinstance(tr_content, list):
                            # 多 item 合并成单条 tool 消息（每个 tool_use 只能有一个 tool_result）
                            text_bits = []
                            for c in tr_content:
                                if isinstance(c, str):
                                    text_bits.append(c)
                                elif isinstance(c, dict) and c.get("type") == "text":
                                    text_bits.append(c.get("text", ""))
                            tr_text = "\n".join(text_bits)
                        else:
                            tr_text = str(tr_content)
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tr_text,
                        })
                    elif block.get("type") == "image":
                        # Anthropic image → OpenAI image_url（data URL）
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            media = source.get("media_type", "image/jpeg")
                            data = source.get("data", "")
                            user_parts.append(f"data:{media};base64,{data}")
                        elif source.get("type") == "url":
                            user_parts.append(source.get("url", ""))
                elif isinstance(block, str):
                    user_parts.append(block)
            # flush 顺序：tool 消息在前，user 消息在后（OpenAI 要求 tool 紧跟 assistant tool_calls）
            messages.extend(tool_messages)
            if user_parts:
                messages.append({"role": "user", "content": "\n".join(user_parts)})
        else:
            # user 字符串 或其他（system 已在上面处理）
            messages.append({"role": role, "content": str(content)})

    openai_req = {
        "model": anthropic_body.get("model", "claude-sonnet-5"),
        "messages": messages,
        "max_completion_tokens": anthropic_body.get("max_tokens", 4096),
    }
    if system_out:
        openai_req["system"] = system_out

    # optional fields
    if anthropic_body.get("temperature") is not None:
        openai_req["temperature"] = anthropic_body["temperature"]
    if anthropic_body.get("top_p") is not None:
        openai_req["top_p"] = anthropic_body["top_p"]
    if anthropic_body.get("top_k") is not None:
        openai_req["top_k"] = anthropic_body["top_k"]
    # stop_sequences → stop（OpenAI 参数名）
    if anthropic_body.get("stop_sequences"):
        openai_req["stop"] = anthropic_body["stop_sequences"]

    # stream
    if anthropic_body.get("stream"):
        openai_req["stream"] = True
        openai_req["stream_options"] = {"include_usage": True}

    # tools（Anthropic tools[{name,description,input_schema}] → OpenAI tools）
    tools = anthropic_body.get("tools")
    if tools:
        openai_tools = []
        for t in tools:
            function_chunk = {"name": t.get("name", "")}
            if t.get("description"):
                function_chunk["description"] = t["description"]
            if t.get("input_schema"):
                function_chunk["parameters"] = t["input_schema"]
            openai_tools.append({"type": "function", "function": function_chunk})
        openai_req["tools"] = openai_tools

    # tool_choice 映射
    tool_choice = anthropic_body.get("tool_choice")
    if tool_choice and isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "auto":
            openai_req["tool_choice"] = "auto"
        elif tc_type == "none":
            openai_req["tool_choice"] = "none"
        elif tc_type == "any":
            openai_req["tool_choice"] = "required"
        elif tc_type == "tool" and tool_choice.get("name"):
            openai_req["tool_choice"] = {
                "type": "function", "function": {"name": tool_choice["name"]}
            }

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
