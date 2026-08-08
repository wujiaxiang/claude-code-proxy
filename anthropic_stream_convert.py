"""
OpenAI SSE → Anthropic SSE 流式转换（借鉴 LiteLLM，零依赖）

背景
----
8081 /v1/messages 把 Anthropic 请求翻译成 OpenAI 格式转发本地端口后，
流式响应需要译回 Anthropic SSE 事件序列。LiteLLM 用
`AnthropicStreamWrapper`（litellm/llms/anthropic/experimental_pass_through/
adapters/streaming_iterator.py）处理该转换——本模块移植其核心状态机，
去除 LiteLLM 的 compaction / applied_edits / 多 provider 分支，只保留
OpenAI→Anthropic 纯转换语义。

事件序列（Anthropic 标准）
----------------------------
    message_start → content_block_start → content_block_delta* →
    content_block_stop → message_delta(usage) → message_stop

关键边界（LiteLLM 踩坑沉淀，勿删勿改）：
1. content_block_start 的 body 恒为空（text:""/input:{}/thinking:""），
   内容全部走 delta——因此块切换时必须重发触发 chunk 自身的 delta，
   否则丢首 token / 丢整个 arguments blob。
2. 空白 delta（无 content/tool_calls/reasoning_content）必须吞掉，
   否则空 delta 会被误分类成 text_delta 打进 thinking 块 → 严格 SDK 崩溃。
3. message_delta 在 finish_reason 出现时先 hold，等末尾 usage chunk
   （choices=[]）到达后合并再发——Anthropic 不允许 message_delta 之后
   再有事件，而 usage 晚一个 chunk。
4. 并行工具调用：tool_use→tool_use 不是 no-op——新 function.name 出现
   即开新块（arguments 分片不带 name，续当前块）。
5. finish_reason 映射：stop→end_turn / length→max_tokens / tool_calls→tool_use
   （LiteLLM 漏了 function_call→tool_use，补上）；空串/None 视为未结束。
6. usage：Anthropic 的 input_tokens 不含缓存 token，OpenAI 的 prompt_tokens
   含——需减去 cache_read，否则 Claude Code 上下文计费双算。

输入约定：喂重组后的完整 SSE data 行（_SseLineBuffer 已重组，勿喂半截帧）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Iterator

_FINISH_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",  # LiteLLM 漏映射，补上
    "content_filter": "end_turn",
}

# Anthropic delta 类型 → 载荷字段名（_has_content 用）
_PAYLOAD_FIELD = {
    "text_delta": "text",
    "input_json_delta": "partial_json",
    "thinking_delta": "thinking",
    "signature_delta": "signature",
}


def _sse(ev: dict) -> bytes:
    """Anthropic SSE 帧：event: + data: 两行都要（客户端按 event 名分派）。"""
    payload = json.dumps(ev, ensure_ascii=False)
    return f"event: {ev['type']}\ndata: {payload}\n\n".encode("utf-8")


def _classify(delta: dict) -> tuple[str, dict]:
    """分类当前 delta 的块类型 + content_block_start 体（体恒为空）。"""
    tcs = delta.get("tool_calls") or []
    if tcs and tcs[0].get("function") is not None:
        tc = tcs[0]
        return "tool_use", {
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": (tc.get("function") or {}).get("name") or "",
            "input": {},
        }
    if delta.get("content"):
        return "text", {"type": "text", "text": ""}
    if delta.get("reasoning_content"):
        return "thinking", {"type": "thinking", "thinking": "", "signature": ""}
    # thinking_blocks（部分 provider）与 reasoning_content 并列处理
    thinking_blocks = delta.get("thinking_blocks") or []
    if thinking_blocks and isinstance(thinking_blocks[0], dict):
        tb0: dict = thinking_blocks[0]
        if tb0.get("type") == "thinking":
            return "thinking", {
                "type": "thinking",
                "thinking": tb0.get("thinking") or "",
                "signature": tb0.get("signature") or "",
            }
    return "text", {"type": "text", "text": ""}


def _to_delta(delta: dict) -> dict:
    """delta → Anthropic delta。优先级：tool > thinking > text。"""
    tcs = delta.get("tool_calls") or []
    if tcs:
        # 并行工具：拼接各片段的 arguments（空串也算，避免漏片）
        pj = "".join((t.get("function") or {}).get("arguments") or "" for t in tcs)
        return {"type": "input_json_delta", "partial_json": pj}
    if delta.get("reasoning_content"):
        return {"type": "thinking_delta", "thinking": delta["reasoning_content"]}
    thinking_blocks: list = delta.get("thinking_blocks") or []
    for tb_raw in thinking_blocks:
        if isinstance(tb_raw, dict):
            tb: dict[str, Any] = tb_raw
            sig = str(tb.get("signature") or "")
            th = str(tb.get("thinking") or "")
            if sig and not th:
                return {"type": "signature_delta", "signature": sig}
            if tb.get("type") == "thinking":
                return {"type": "thinking_delta", "thinking": th}
    return {"type": "text_delta", "text": delta.get("content") or ""}


def _has_content(d: dict) -> bool:
    """delta 是否携带有效载荷（空 delta 必须过滤，避免错误类型混入块）。"""
    field = _PAYLOAD_FIELD.get(str(d.get("type") or ""))
    return bool(field and d.get(field))


def _usage(u: dict) -> dict:
    """OpenAI usage → Anthropic usage（input_tokens 减缓存，防双算）。"""
    ptd = u.get("prompt_tokens_details") or {}
    read = ptd.get("cached_tokens") or 0
    cache_creation = ptd.get("cache_creation") or 0
    result = {
        "input_tokens": max((u.get("prompt_tokens") or 0) - read - cache_creation, 0),
        "output_tokens": u.get("completion_tokens") or 0,
    }
    if cache_creation:
        result["cache_creation_input_tokens"] = cache_creation
    if read:
        result["cache_read_input_tokens"] = read
    return result


def convert_openai_sse_to_anthropic(data_lines: Iterable[str], model: str) -> Iterator[bytes]:
    """把 OpenAI 流式 SSE 的 data 行序列转换为 Anthropic SSE 字节流。

    Args:
        data_lines: 每条为 SSE 的 data: 载荷（不含 "data:" 前缀，已重组完整行；
                    含 "[DONE]" 哨兵或完整 JSON）。
        model: 响应的 model 名（写入 message_start）。

    Yields:
        Anthropic SSE 帧字节（event: X\\ndata: {...}\\n\\n）。
    """
    idx = 0
    cur_type: str | None = None
    cur_start: dict | None = None
    started = False
    closed = False
    held: dict | None = None  # message_delta 等 usage 合并
    emitted_final = False

    yield _sse({
        "type": "message_start",
        "message": {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            # 全零 + cache 字段显式声明：向 Claude Code 表明支持 prompt caching
            "usage": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    })

    for raw in data_lines:
        if raw is None:
            continue
        raw = raw.strip()
        if raw == "[DONE]":
            break
        if not raw.startswith("{"):
            continue
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue  # 解析失败绝不中断流

        choices = chunk.get("choices") or []

        # choices 为空的 chunk = 末尾 usage 专帧 → 合并进 held message_delta
        if not choices:
            if held is not None and chunk.get("usage"):
                held["usage"] = _usage(chunk["usage"])
                yield _sse(held)
                held = None
                emitted_final = True
            continue

        ch0 = choices[0]
        delta = ch0.get("delta") or {}
        finish_reason = ch0.get("finish_reason")

        # ---- 结束：content_block_stop + message_delta（可能 hold 等 usage）----
        if finish_reason:
            if started and not closed:
                yield _sse({"type": "content_block_stop", "index": idx})
                closed = True
            md = {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _FINISH_MAP.get(finish_reason, "end_turn"),
                    "stop_sequence": None,
                },
                "usage": _usage(chunk["usage"]) if chunk.get("usage")
                         else {"input_tokens": 0, "output_tokens": 0},
            }
            if chunk.get("usage"):
                yield _sse(md)
                emitted_final = True
            else:
                held = md  # 等末尾 usage 专帧
            continue

        # ---- 空白 delta（role-only opener 等）→ 吞掉，避免开空块 ----
        blank = not (
            delta.get("content") or delta.get("tool_calls")
            or delta.get("reasoning_content") or delta.get("thinking_blocks")
        )
        if blank:
            continue

        btype, bstart = _classify(delta)
        adelta = _to_delta(delta)

        # ---- 首个内容块 ----
        if not started:
            started = True
            closed = False
            cur_type, cur_start = btype, bstart
            yield _sse({"type": "content_block_start", "index": idx, "content_block": bstart})
        else:
            # 块切换：类型变了，或并行工具的新 function.name 出现
            new_tool = btype == "tool_use" and bstart.get("name")
            if btype != cur_type or new_tool:
                yield _sse({"type": "content_block_stop", "index": idx})
                idx += 1
                cur_type, cur_start = btype, bstart
                yield _sse({"type": "content_block_start", "index": idx, "content_block": bstart})
                # 关键：重发触发块的自身 delta，否则丢首 token / arguments blob
                if _has_content(adelta):
                    yield _sse({"type": "content_block_delta", "index": idx, "delta": adelta})
                continue

        if _has_content(adelta):
            yield _sse({"type": "content_block_delta", "index": idx, "delta": adelta})

    # 收尾：未关块则关、held 的 message_delta 补发、message_stop
    if started and not closed:
        yield _sse({"type": "content_block_stop", "index": idx})
    if held is not None and not emitted_final:
        yield _sse(held)
    yield _sse({"type": "message_stop"})


# 便捷入口：接收整条 SSE 字节流（含 event/data 行或纯 data 行），内部按行解析
def convert_openai_sse_bytes_to_anthropic(sse_bytes: Iterable[bytes], model: str) -> Iterator[bytes]:
    """字节级入口：输入 OpenAI SSE 字节流（每块可能是完整帧或跨帧），
    内部重组 data: 行后调 convert_openai_sse_to_anthropic。"""
    import re as _re

    buf = b""
    for chunk in sse_bytes:
        buf += chunk
        # 按双换行切帧（SSE 帧分隔）
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            text = frame.decode("utf-8", errors="replace")
            m = _re.search(r"^data:\s*(.*)$", text, _re.M)
            if m:
                data = m.group(1).strip()
                if data:
                    yield from convert_openai_sse_to_anthropic([data], model)
        # 缓冲残留（跨帧半截）留在 buf 等待下一块
    # 流结束，处理缓冲残留
    if buf:
        text = buf.decode("utf-8", errors="replace")
        m = _re.search(r"^data:\s*(.*)$", text, _re.M)
        if m:
            data = m.group(1).strip()
            if data:
                yield from convert_openai_sse_to_anthropic([data], model)
