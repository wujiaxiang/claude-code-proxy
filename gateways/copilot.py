# Copilot 网关模块（从 server.py 拆分，零行为变化）
# 此处符号原样剪切自 server.py，逻辑/参数/返回值/常量/正则均未改动。
#
# 覆盖两部分：
#   1. Copilot Responses API 桥接（/chat/completions ↔ /responses 双向转换）
#   2. Provider 策略（模型名映射 + LiteLLM 请求装配）
#
# 注：COPILOT_GHE_TOKEN / COPILOT_API_BASE / COPILOT_INTEGRATION_ID /
# COPILOT_{BIG,MEDIUM,SMALL}_MODEL 仍留在 server.py —— 其中 COPILOT_GHE_TOKEN 是
# 热重载可变全局（_load_vendor_targets/_reload_targets/_refresh_secrets 里 global
# 重赋值），import 绑定的是快照会丢失热更新，因此一律在函数内延迟导入取实时值。
import json
import time
import uuid
from typing import Dict, List, Optional


# ══════════════════════════════════════════════════════════════════════════════
# Copilot Responses API 桥接（/chat/completions ↔ /responses 双向转换）
# 上游部分模型（gpt-5.6-terra/gpt-5.6-luna/gpt-5.3-codex/gpt-5.4-mini/
# mai-code-1-flash-picker）只支持 /responses 协议，不支持 /chat/completions。
# 网关按 targets.json 的 responsesModels 列表判定，把客户端的标准 OpenAI
# chat.completions 请求转换为 Responses API 格式转发，再把响应转回 chat 格式。
# ══════════════════════════════════════════════════════════════════════════════

_RESPONSES_FINISH_REASON_MAP = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "content_filter",
    "cancelled": "stop",
}


def _copilot_chat_to_responses_body(body: dict) -> dict:
    """OpenAI chat.completions 请求体 → OpenAI Responses API 请求体。

    关键差异（依据 OpenAI 官方迁移指南 + VS Code Copilot 实现）：
      messages → input（system 提炼为顶层 instructions；
                   tool 消息 → function_call_output；
                   assistant 的 tool_calls 拆成平铺 function_call item，
                   不能保留 chat 格式的 tool_calls 字段，否则上游 400
                   "Unknown parameter: 'input[i].tool_calls'"）
      max_tokens/max_completion_tokens → max_output_tokens
      tools 从 {function:{...}} 嵌套 → 扁平 {name, description, parameters}
      tool_choice 指定函数 → {type:function, name}
      response_format → text.format
      reasoning_effort → reasoning.effort
      注入 store=false（代理无状态转发，避免上游存储对话）
    """
    out: dict = {}
    if body.get("model"):
        out["model"] = body["model"]

    # ── messages → input ──
    input_msgs = []
    system_parts = []
    for m in body.get("messages", []) or []:
        role = m.get("role", "")
        if role in ("system", "developer"):
            c = m.get("content", "")
            if isinstance(c, str):
                system_parts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                        system_parts.append(part.get("text", ""))
            continue
        if role == "tool":
            input_msgs.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content", ""), ensure_ascii=False),
            })
        elif role == "assistant" and m.get("tool_calls"):
            # 文本部分 → 独立 message item
            content = m.get("content")
            if content:
                input_msgs.append({
                    "type": "message",
                    "role": "assistant",
                    "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                })
            # 每个 tool_call → 平铺 function_call item（Responses API 标准）
            for c in m["tool_calls"]:
                fn = c.get("function", {}) or {}
                input_msgs.append({
                    "type": "function_call",
                    "call_id": c.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
        else:
            content = m.get("content", "")
            am: dict = {
                "type": "message",
                "role": role,
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            }
            if m.get("name"):
                am["name"] = m["name"]
            input_msgs.append(am)
    if system_parts:
        out["instructions"] = "\n\n".join(system_parts)
    if input_msgs:
        out["input"] = input_msgs

    # ── 输出上限 ──
    mt = body.get("max_tokens") or body.get("max_completion_tokens")
    if mt is not None:
        out["max_output_tokens"] = mt

    if "stream" in body:
        out["stream"] = bool(body["stream"])

    # 代理无状态转发：显式关闭上游对话存储（Responses API 默认 store=true）
    out["store"] = False

    # ── 采样参数（字段名两边一致，直接透传）──
    for k in ("temperature", "top_p", "stop", "user", "metadata",
              "frequency_penalty", "presence_penalty", "top_logprobs",
              "logprobs", "seed"):
        if body.get(k) is not None:
            out[k] = body[k]

    # ── tools 扁平化：{type,function:{name,...}} → {type,name,description,parameters} ──
    if body.get("tools"):
        flat_tools = []
        for t in body["tools"]:
            fn = t.get("function", {}) or {}
            ft: dict = {
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
            if fn.get("strict") is not None:
                ft["strict"] = fn["strict"]
            flat_tools.append(ft)
        out["tools"] = flat_tools

    # ── tool_choice ──
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("function"):
            out["tool_choice"] = {"type": "function", "name": tc["function"].get("name", "")}
        else:
            out["tool_choice"] = tc

    # ── reasoning_effort → reasoning.effort ──
    if body.get("reasoning_effort") is not None:
        out["reasoning"] = {"effort": body["reasoning_effort"]}

    # ── response_format → text.format ──
    if body.get("response_format"):
        rf = body["response_format"]
        ftype = rf.get("type", "text")
        if ftype == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif ftype == "json_schema":
            js = rf.get("json_schema", {}) or {}
            out["text"] = {"format": {
                "type": "json_schema",
                "name": js.get("name", "schema"),
                "schema": js.get("schema", {}),
            }}
    return out


def _copilot_responses_to_chat_body(resp: dict, model: str) -> dict:
    """OpenAI Responses API 响应体 → OpenAI chat.completions 响应体。"""
    content_parts: List[str] = []
    tool_calls: List[dict] = []
    for item in resp.get("output", []) or []:
        t = item.get("type")
        if t == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    content_parts.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    content_parts.append(part.get("refusal", ""))
        elif t == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments") or "{}",
                },
            })

    content = "".join(content_parts) or None
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls

    status = resp.get("status", "completed")
    has_tool = any((item.get("type") == "function_call") for item in resp.get("output", []) or [])
    if has_tool and status == "completed":
        finish_reason = "tool_calls"
    else:
        finish_reason = _RESPONSES_FINISH_REASON_MAP.get(status, "stop")
    u = resp.get("usage") or {}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
        },
    }


class _ClientDisconnected(Exception):
    """客户端已断开（流式转发中 TCP 关闭），用于静默收尾。"""


def _copilot_responses_usage_to_chat(usage: Optional[dict]) -> dict:
    """Responses usage → chat usage（流式 completed 事件用）。

    usage 允许为 None（上游 response 可能不带 usage 字段），函数体内已 `usage or {}` 兜底。
    """
    return {
        "prompt_tokens": (usage or {}).get("input_tokens", 0),
        "completion_tokens": (usage or {}).get("output_tokens", 0),
        "total_tokens": (usage or {}).get("total_tokens", 0),
    }


def _copilot_stream_chunk(model: str, delta: dict, finish_reason=None, usage: Optional[dict] = None) -> dict:
    """构造 OpenAI chat.completion.chunk。"""
    chunk: dict = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


async def _write_copilot_responses_stream(writer, resp, model: str, label: str) -> None:
    """上游 /responses SSE 事件流 → OpenAI chat.completions SSE，写回客户端。

    事件映射：
      response.created                          → 首 chunk（role: assistant）
      response.output_item.added (function_call)→ tool_call 首 chunk（id/name/index=output_index）
      response.output_text.delta                → content chunk
      response.refusal.delta                    → content chunk（拒绝内容）
      response.function_call_arguments.delta    → tool_calls arguments chunk（index=output_index）
      response.completed                        → usage chunk + [DONE]
      response.failed                           → 记日志 + finish chunk + [DONE]（不发裸 error）

    定位依据：上游 function_call 的 item_id 是每次 delta 都不同的加密密文，
    不能用作 tool_call 归组 key；必须用稳定的 output_index（VS Code Copilot
    官方实现同样用 chunk.output_index 管理 toolCallInfo）。出现过 tool_call 时
    结束 finish_reason 应为 "tool_calls"（chat 协议语义）。
    """
    from server import logger
    started = False
    saw_tool_call = False
    seen_items: Dict[int, str] = {}   # output_index → call_id

    async def _send(chunk: dict):
        try:
            writer.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            await writer.drain()
        except (RuntimeError, ConnectionResetError, BrokenPipeError):
            # 客户端已断开（超时/取消）：静默收尾，不冒泡触发外层 503
            raise _ClientDisconnected()

    try:
        async for raw in resp.aiter_lines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                ev = json.loads(data_str)
            except Exception:
                continue
            etype = ev.get("type", "")

            if etype == "response.created":
                if not started:
                    started = True
                    await _send(_copilot_stream_chunk(model, {"role": "assistant", "content": ""}))

            elif etype == "response.output_item.added":
                item = ev.get("item", {}) or {}
                if item.get("type") == "function_call":
                    saw_tool_call = True
                    oidx = ev.get("output_index", 0)
                    if oidx not in seen_items:
                        seen_items[oidx] = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                    await _send(_copilot_stream_chunk(model, {
                        "tool_calls": [{
                            "index": oidx,
                            "id": seen_items[oidx],
                            "type": "function",
                            "function": {"name": item.get("name", ""), "arguments": ""},
                        }],
                    }))

            elif etype == "response.output_text.delta":
                d = ev.get("delta")
                if d:
                    await _send(_copilot_stream_chunk(model, {"content": d}))

            elif etype == "response.refusal.delta":
                d = ev.get("delta")
                if d:
                    await _send(_copilot_stream_chunk(model, {"content": d}))

            elif etype == "response.function_call_arguments.delta":
                d = ev.get("delta")
                if d:
                    saw_tool_call = True
                    oidx = ev.get("output_index", 0)
                    if oidx not in seen_items:
                        seen_items[oidx] = ev.get("call_id") or f"call_{uuid.uuid4().hex[:12]}"
                    await _send(_copilot_stream_chunk(model, {
                        "tool_calls": [{"index": oidx, "function": {"arguments": d}}],
                    }))

            elif etype == "response.completed":
                r = ev.get("response", {}) or {}
                status = r.get("status", "completed")
                if saw_tool_call and status == "completed":
                    finish = "tool_calls"
                else:
                    finish = _RESPONSES_FINISH_REASON_MAP.get(status, "stop")
                usage = _copilot_responses_usage_to_chat(r.get("usage") or {})
                await _send(_copilot_stream_chunk(model, {}, finish_reason=finish, usage=usage))
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
                return

            elif etype == "response.failed":
                r = ev.get("response", {}) or {}
                err = (r.get("error") or ev.get("error")) or {"message": "upstream response failed"}
                logger.warning(f"[{label}] responses failed: {json.dumps(err, ensure_ascii=False)[:300]}")
                await _send(_copilot_stream_chunk(model, {}, finish_reason=("tool_calls" if saw_tool_call else "stop")))
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
                return

        # 流未正常结束（无 completed/failed）→ 补 finish + [DONE]
        if not started:
            await _send(_copilot_stream_chunk(model, {"role": "assistant", "content": ""}))
        await _send(_copilot_stream_chunk(model, {}, finish_reason=("tool_calls" if saw_tool_call else "stop")))
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()
    except _ClientDisconnected:
        # 客户端已断开（HTTP 200 头已发出），静默收尾，不写 503
        logger.debug(f"[{label}] responses stream: client disconnected")
    except Exception:
        logger.warning(f"[{label}] responses stream conversion failed")
        try:
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        except Exception:
            pass
        raise


def _copilot_model_name(anthropic_model: str) -> str:
    """把 Anthropic 模型名映射到 Copilot 企业可用模型名"""
    from server import COPILOT_BIG_MODEL, COPILOT_MEDIUM_MODEL, COPILOT_SMALL_MODEL
    m = anthropic_model.lower()
    # 去掉 provider 前缀
    for prefix in ("anthropic/", "openai/", "copilot/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    if "opus" in m:
        return COPILOT_BIG_MODEL
    if "sonnet" in m:
        return COPILOT_MEDIUM_MODEL
    if "haiku" in m:
        return COPILOT_SMALL_MODEL
    # 如果已经是 copilot 的模型名（如 claude-sonnet-4.6），直接使用
    return m


def _is_claude_family_model(model_name: str) -> bool:
    m = (model_name or "").lower()
    for prefix in ("anthropic/", "openai/", "copilot/", "gemini/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
            break
    return m.startswith("claude-") or "claude" in m


def _copilot_provider(req, litellm_req, orig):
    """GitHub Copilot Enterprise — 走 LiteLLM（与 qclaw 同一路径）"""
    # COPILOT_GHE_TOKEN 为热重载可变全局，必须在调用时取实时值（勿改为模块级 import）
    from server import (
        COPILOT_GHE_TOKEN,
        COPILOT_API_BASE,
        COPILOT_INTEGRATION_ID,
        logger,
    )
    # 模型映射
    target_model = _copilot_model_name(orig)
    litellm_req["model"] = f"openai/{target_model}"
    litellm_req["api_key"] = COPILOT_GHE_TOKEN
    litellm_req["api_base"] = COPILOT_API_BASE
    litellm_req["extra_headers"] = {"Copilot-Integration-Id": COPILOT_INTEGRATION_ID}

    # 模型能力分流：Claude 家族不接受采样参数，GPT 家族保留
    if _is_claude_family_model(target_model):
        for k in ("temperature", "top_p", "top_k", "min_p"):
            litellm_req.pop(k, None)

    # Copilot 不接受空/None 消息 content
    for msg in litellm_req.get("messages", []):
        c = msg.get("content")
        if c is None or (isinstance(c, str) and not c.strip()):
            msg["content"] = "."

    # Copilot 不接受没有 tools 时的 tool_choice
    if litellm_req.get("tool_choice") and not litellm_req.get("tools"):
        litellm_req.pop("tool_choice")

    logger.debug(f"🤖 Copilot via LiteLLM: → {litellm_req['model']}")
    return None  # 继续走 LiteLLM
