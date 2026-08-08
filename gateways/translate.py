# 翻译层模块（从 server.py 拆分，零行为变化）
# 8081 legacy 单端口模式大清理后，本模块只保留 token 估算族（count_tokens /
# 目标端口 usage 估算依赖）；LiteLLM 翻译链与 provider 策略已随分支 C 一并删除。
# 对 server 的共享依赖（logger 等）一律用函数内延迟导入 `from server import X`。
import json
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
