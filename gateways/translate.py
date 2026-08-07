# 翻译层模块（从 server.py 拆分，零行为变化）
# 此处符号原样剪切自 server.py，逻辑/参数/返回值/常量/正则均未改动。
# 对 server 的共享依赖（logger / Pydantic 模型 / 环境常量等）一律用函数内延迟导入
# `from server import X`——server 双加载已被主模块别名防护；_PROVIDER_STRATEGIES
# 经 PEP 562 模块 __getattr__ 惰性构建（避免 server↔translate 模块级循环导入），
# 首次访问时才从 server 拉取 _qclaw_provider / _gemini_provider / _copilot_provider。
import json
import uuid
from typing import Any, Dict, List, Optional

# 本地 token 估算（tiktoken）— 上游 QClaw 网关不返回 usage，需自行估算
import tiktoken as _tiktoken

# 缓存 tokenizer 实例（每个 encoding 只加载一次）
_TIKTOKEN_CACHE: Dict[str, "_tiktoken.Encoding"] = {}


def _get_tokenizer(model_name: str) -> "_tiktoken.Encoding":
    """根据模型名选合适的 tokenizer。

    QClaw 透传模型（DeepSeek/GLM/Kimi/MiniMax）以及 Claude 都用 cl100k_base 做近似估算——
    这是经验上最接近的通用 tokenizer，估算误差通常在 ±10% 内，足够给 Claude Code 显示用量。
    """
    from server import logger
    cache_key = "cl100k_base"
    if cache_key not in _TIKTOKEN_CACHE:
        try:
            _TIKTOKEN_CACHE[cache_key] = _tiktoken.get_encoding(cache_key)
        except Exception as _e:
            logger.warning(f"Failed to load tiktoken encoding {cache_key}: {_e}")
            _TIKTOKEN_CACHE[cache_key] = None  # type: ignore
    return _TIKTOKEN_CACHE[cache_key]


def _extract_text_from_content(content: Any) -> str:
    """从 messages 的 content 字段（可能是 str / list[dict]）抽出纯文本用于估算。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "thinking":
                    parts.append(block.get("thinking", ""))
                elif t == "tool_use":
                    # 工具调用：序列化 input + name
                    try:
                        parts.append(block.get("name", ""))
                        parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
                    except Exception:
                        pass
                elif t == "tool_result":
                    # 工具结果：递归抽 text
                    parts.append(_extract_text_from_content(block.get("content", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _estimate_messages_tokens(messages: List[Any], model: str = "", system: Any = None, tools: Optional[List[Any]] = None) -> int:
    """估算 Anthropic/OpenAI messages 的输入 token 数。

    估算规则（参考 OpenAI 官方公式）：
        tokens = sum(每条 message: 4 + role + text) + 3 (priming)
    system / tools 单独累加。
    """
    enc = _get_tokenizer(model)
    if enc is None:
        # fallback：粗略按 4 字符 / token 估算
        total_chars = 0
        for m in messages:
            total_chars += len(_extract_text_from_content(getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")))
        if system:
            total_chars += len(_extract_text_from_content(system))
        return max(1, total_chars // 4)

    total = 3  # priming
    if system:
        sys_text = _extract_text_from_content(system)
        total += 4 + len(enc.encode(sys_text))
    if tools:
        for tool in tools:
            try:
                # tool 可能是 Pydantic 对象或 dict
                if hasattr(tool, "model_dump"):
                    tool_dict = tool.model_dump()
                else:
                    tool_dict = tool
                total += 4 + len(enc.encode(json.dumps(tool_dict, ensure_ascii=False)))
            except Exception:
                pass
    for m in messages:
        # m 可能是 dict 或 Pydantic Message
        if isinstance(m, dict):
            role = m.get("role", "")
            content = m.get("content")
        else:
            role = getattr(m, "role", "")
            content = getattr(m, "content", None)
        text = _extract_text_from_content(content)
        total += 4 + len(enc.encode(role)) + len(enc.encode(text))
    return total


def _estimate_text_tokens(text: str, model: str = "") -> int:
    """估算单段文本的 token 数（用于 output_tokens）。"""
    if not text:
        return 0
    enc = _get_tokenizer(model)
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))


def _convert_oai_to_anthropic(oai_data: dict, request, original_model: str):  # type: ignore
    """将 OpenAI chat completion 响应转换为 Anthropic messages 格式. 简化版."""
    from server import logger, MessagesResponse, Usage
    choice = oai_data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content_blocks = []

    # reasoning_content -> thinking block
    if msg.get("reasoning_content"):
        content_blocks.append({
            "type": "thinking",
            "thinking": msg["reasoning_content"],
        })

    # content -> text block
    if msg.get("content"):
        content_blocks.append({
            "type": "text",
            "text": msg["content"],
        })

    # tool_calls -> tool_use blocks
    for tc in msg.get("tool_calls", []):
        func = tc.get("function", {})
        try:
            inp = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            inp = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": func.get("name", ""),
            "input": inp,
        })

    # usage — QClaw 网关不返回 usage，缺失时用 tiktoken 本地估算
    usage = oai_data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    if prompt_tokens == 0 or completion_tokens == 0:
        # 估算 input：从 request.messages + system + tools
        try:
            req_msgs = getattr(request, "messages", []) or []
            req_system = getattr(request, "system", None)
            req_tools = getattr(request, "tools", None)
            est_in = _estimate_messages_tokens(req_msgs, original_model, req_system, req_tools)
            if prompt_tokens == 0:
                prompt_tokens = est_in
        except Exception as _e:
            logger.debug(f"tiktoken input estimate failed: {_e}")
        # 估算 output：从响应 content_blocks 抽文本
        if completion_tokens == 0:
            try:
                out_text = _extract_text_from_content(content_blocks)
                completion_tokens = _estimate_text_tokens(out_text, original_model)
            except Exception as _e:
                logger.debug(f"tiktoken output estimate failed: {_e}")

    # 用 model_validate 而非关键字构造：content/stop_reason 是运行时 dict/动态字符串，
    # 交给 Pydantic 校验与关键字构造完全等价，且避免静态类型层的字面量不匹配。
    return MessagesResponse.model_validate({
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": content_blocks or [{"type": "text", "text": ""}],
        "stop_reason": choice.get("finish_reason") or "stop",
        "stop_sequence": None,
        "usage": Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
    })


# ─── Provider 策略（开闭原则：新增 provider 只需在此注册） ───

def _default_provider(req, litellm_req, _orig):
    """标准 OpenAI"""
    from server import OPENAI_API_KEY, OPENAI_BASE_URL, logger
    litellm_req["api_key"] = OPENAI_API_KEY
    if OPENAI_BASE_URL:
        litellm_req["api_base"] = OPENAI_BASE_URL
        logger.debug(f"OpenAI: base={OPENAI_BASE_URL}")
    else:
        logger.debug(f"OpenAI: default")
    return None  # 继续走 LiteLLM


def _anthropic_provider(req, litellm_req, _orig):
    """Anthropic / 自定义 Anthropic API"""
    from server import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, logger
    litellm_req["api_key"] = ANTHROPIC_API_KEY
    if ANTHROPIC_BASE_URL:
        litellm_req["api_base"] = ANTHROPIC_BASE_URL
        logger.debug(f"Anthropic: base={ANTHROPIC_BASE_URL}")
    else:
        logger.debug(f"Anthropic: default")
    return None  # 继续走 LiteLLM


# _PROVIDER_STRATEGIES 经 PEP 562 模块 __getattr__ 惰性构建：_qclaw_provider 等策略函数
# 由 server.py 从各 gateway 模块导入，本模块若模块级引用会形成 server↔translate 循环导入
# （静态分析下会破坏 server 的符号推断），故延迟到首次访问时才从 server 拉取；
# 每次访问返回内容一致的新 dict，调用方仅使用 .get(...)，行为与原模块级 dict 完全等价。
def __getattr__(name: str) -> Any:
    if name == "_PROVIDER_STRATEGIES":
        from server import _qclaw_provider, _gemini_provider, _copilot_provider
        return {
            "openai": _default_provider,
            "qclaw": _qclaw_provider,

            "anthropic": _anthropic_provider,
            "gemini": _gemini_provider,
            "gemini-openai": _gemini_provider,
            "copilot": _copilot_provider,
        }
    raise AttributeError(name)


def _map_model_name(model: str) -> str:
    """把任意模型名按当前 PREFERRED_PROVIDER 映射到 LiteLLM 可用的带前缀名称。
    copilot provider 请用 _copilot_model_name() 代替。"""
    from server import PREFERRED_PROVIDER, BIG_MODEL, MEDIUM_MODEL, SMALL_MODEL
    clean = model
    for prefix in ("anthropic/", "openai/", "gemini/", "qclaw/", "copilot/"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    c = clean.lower()
    if "opus" in c:
        target = BIG_MODEL
    elif "sonnet" in c:
        target = MEDIUM_MODEL
    elif "haiku" in c:
        target = SMALL_MODEL
    else:
        target = clean  # 已经是目标 provider 的模型名，直接用
    # 加 provider 前缀
    if PREFERRED_PROVIDER == "anthropic":
        return f"anthropic/{target}"
    elif PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
        return f"gemini/{target}"
    elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
        return target  # qclaw/copilot 靠 api_base 路由，不需要前缀（model 在 provider 策略里覆盖）
    else:  # openai / default
        return f"openai/{target}"


# Helper function to clean schema for Gemini
def clean_gemini_schema(schema: Any) -> Any:
    """Recursively removes unsupported fields from a JSON schema for Gemini."""
    from server import logger
    if isinstance(schema, dict):
        # Remove specific keys unsupported by Gemini tool parameters
        schema.pop("additionalProperties", None)
        schema.pop("default", None)

        # Check for unsupported 'format' in string types
        if schema.get("type") == "string" and "format" in schema:
            allowed_formats = {"enum", "date-time"}
            if schema["format"] not in allowed_formats:
                logger.debug(
                    f"Removing unsupported format '{schema['format']}' for string type in Gemini schema."
                )
                schema.pop("format")

        # Recursively clean nested schemas (properties, items, etc.)
        for key, value in list(
            schema.items()
        ):  # Use list() to allow modification during iteration
            schema[key] = clean_gemini_schema(value)
    elif isinstance(schema, list):
        # Recursively clean items in a list
        return [clean_gemini_schema(item) for item in schema]
    return schema


def _close_json_fragment(fragment: str) -> str:
    """Best-effort close for streaming tool arguments JSON fragments."""
    if not isinstance(fragment, str) or not fragment:
        return ""
    try:
        json.loads(fragment)
        return ""
    except Exception:
        pass

    stack = []
    in_string = False
    escaped = False

    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and ch == stack[-1]:
            stack.pop()

    suffix = ""
    if in_string:
        suffix += '"'
    if stack:
        suffix += "".join(reversed(stack))

    if not suffix:
        return ""
    try:
        json.loads(fragment + suffix)
        return suffix
    except Exception:
        return ""


async def _litellm_oai_stream(response_generator):
    """把 LiteLLM 流式输出转成 OpenAI SSE 格式（bytes）"""
    from server import logger, _is_rate_limit_error
    try:
        async for chunk in response_generator:
            try:
                yield f"data: {json.dumps(chunk.model_dump())}\n\n".encode()
            except Exception:
                pass
    except Exception as e:
        # 流中途上游抛限流异常（headers 已发送，无法改状态码）：
        # 发出 SSE error 事件让客户端感知，而不是伪成功 [DONE]。
        if _is_rate_limit_error(e):
            logger.warning(f"🕐 LiteLLM stream rate-limited (SSE error event): {e}")
            err = {"error": {"type": "rate_limit_error", "message": str(e)}}
            yield f"data: {json.dumps(err)}\n\n".encode()
        else:
            # 非限流异常保持原行为：向上抛出中断流（由框架处理）
            raise
    yield b"data: [DONE]\n\n"
