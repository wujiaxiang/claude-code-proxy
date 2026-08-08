# ── Gemini 原生协议转换（handler=gemini-native）──
# 客户端走 OpenAI 协议（/v1/chat/completions），代理内部转换为
# Google 原生 generateContent / streamGenerateContent 调用。
# 从 server.py 原样拆分而来（Todo 4），共享符号（logger / _cfg / 等）
# 用函数内延迟导入 from server import X，避免循环依赖。
import json
import os
import re
import time
import uuid
import httpx


_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "OTHER": "stop",
}


def _openai_to_gemini_body(body: dict) -> dict:
    """OpenAI chat.completions 请求体 → Gemini generateContent 请求体。"""
    contents, system_parts = [], []
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")
        if role == "system":
            system_parts.append({"text": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        parts = []
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    img_url = (block.get("image_url") or {}).get("url", "")
                    if img_url.startswith("data:"):
                        try:
                            meta, b64 = img_url[5:].split(",", 1)
                            mime = meta.split(";")[0] or "image/png"
                            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                        except Exception:
                            parts.append({"text": "[image]"})
                    else:
                        parts.append({"text": "[image: " + img_url[:100] + "]"})
                elif block.get("type") == "tool_result" or block.get("type") == "tool_use":
                    t = block.get("content") or block.get("input") or ""
                    parts.append({"text": json.dumps(block, ensure_ascii=False)[:4000]})
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": parts})
    out: dict = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": system_parts}
    gc: dict = {}
    if "max_tokens" in body:
        gc["maxOutputTokens"] = body["max_tokens"]
    elif "max_completion_tokens" in body:
        gc["maxOutputTokens"] = body["max_completion_tokens"]
    if "temperature" in body:
        gc["temperature"] = body["temperature"]
    if "top_p" in body:
        gc["topP"] = body["top_p"]
    if gc:
        out["generationConfig"] = gc
    if body.get("tools"):
        fds = []
        for t in body["tools"]:
            fn = t.get("function", {}) if isinstance(t, dict) else {}
            fds.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters"),
            })
        if fds:
            out["tools"] = [{"functionDeclarations": fds}]
    return out


def _gemini_to_openai_response(gemini_resp: dict, model: str) -> dict:
    """Gemini generateContent 响应 → OpenAI chat.completions 响应。"""
    candidates = gemini_resp.get("candidates", []) or []
    choices = []
    for i, c in enumerate(candidates):
        parts = ((c.get("content") or {}).get("parts", []) or [])
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
        fr = c.get("finishReason", "STOP")
        choices.append({
            "index": i,
            "message": {"role": "assistant", "content": text},
            "finish_reason": _FINISH_REASON_MAP.get(fr, "stop"),
        })
    um = gemini_resp.get("usageMetadata", {}) or {}
    usage = {
        "prompt_tokens": um.get("promptTokenCount", 0),
        "completion_tokens": um.get("candidatesTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", 0),
    }
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": choices,
        "usage": usage,
    }


def _gemini_chunk_to_openai(gemini_chunk: dict, model: str) -> dict | None:
    """Gemini 流式 chunk → OpenAI chat.completion.chunk。

    无 candidates 的心跳/空帧返回 None，调用方按 `if oai_chunk:` 跳过（原语义）。
    """
    candidates = gemini_chunk.get("candidates", []) or []
    if not candidates:
        return None
    c = candidates[0]
    parts = ((c.get("content") or {}).get("parts", []) or [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
    fr = c.get("finishReason", "")
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": text} if text else {},
            "finish_reason": _FINISH_REASON_MAP.get(fr) if fr else None,
        }],
    }


async def _handle_gemini_native(writer, target, method, path, headers, body, stats, label):
    """Gemini 原生协议代理：OpenAI 请求 → generateContent → OpenAI 响应。

    覆盖 /v1/chat/completions（含流式）与 /v1/models。
    认证：客户端 x-goog-api-key/Authorization 优先，其次 secrets.json / 环境变量。
    """
    import json as _json
    # 共享符号延迟导入（server.py 运行时 sys.modules["server"] 已指向 __main__，避免循环依赖）
    from server import (
        _cfg,
        _SECRETS,
        logger,
        _write_error_response,
        _bump_model_stats,
        _write_response,
    )
    gemini_key = _cfg.resolve_secret(target, _SECRETS) or os.environ.get("GEMINI_API_KEY", "")
    api_headers = {"Content-Type": "application/json"}
    if gemini_key:
        api_headers["x-goog-api-key"] = gemini_key
    # 客户端传入的 key 优先（free 类透传场景）
    for hk in ("x-goog-api-key", "authorization"):
        if headers.get(hk):
            api_headers[hk if hk != "authorization" else "x-goog-api-key"] = headers[hk]

    try:
        # ── /v1/models：原生模型列表 → OpenAI 格式 ──
        if path == "/v1/models" and method == "GET":
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), trust_env=False) as c:
                resp = await c.get(f"{_GEMINI_NATIVE_BASE}/models", headers=api_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = [
                        {"id": m["name"].replace("models/", "", 1), "object": "model",
                         "created": 1700000000, "owned_by": "google"}
                        for m in (data.get("models", []) or [])
                        if m.get("name", "").startswith("models/")
                    ]
                    payload = _json.dumps({"data": models, "object": "list", "has_more": False}).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
                    await writer.drain()
                    writer.close(); return
                await _write_error_response(writer, resp.status_code, f"Gemini /models upstream HTTP {resp.status_code}"); return

        # ── /v1/chat/completions：转换 + 转发 ──
        if path == "/v1/chat/completions" and method == "POST":
            try:
                body_json = _json.loads(body.decode("utf-8"))
            except Exception:
                await _write_error_response(writer, 400, "invalid json"); return
            model = body_json.get("model", "gemini-2.5-flash")
            is_stream = bool(body_json.get("stream", False))
            stats["totalRequests"] += 1
            _bump_model_stats(label, model, "ok")

            gemini_body = _openai_to_gemini_body(body_json)
            endpoint = (f"{_GEMINI_NATIVE_BASE}/models/{model}:streamGenerateContent?alt=sse"
                        if is_stream else f"{_GEMINI_NATIVE_BASE}/models/{model}:generateContent")
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as c:
                req = c.build_request("POST", endpoint, headers=api_headers,
                                      content=_json.dumps(gemini_body).encode())
                resp = await c.send(req, stream=True)

                if resp.status_code >= 400:
                    resp_body = await resp.aread()
                    await _write_error_response(writer, resp.status_code,
                                                f"Gemini upstream HTTP {resp.status_code}: {resp_body.decode('utf-8', errors='replace')[:300]}")
                    return

                # ── 非流式 ──
                if not is_stream:
                    resp_body = await resp.aread()
                    try:
                        gemini_json = _json.loads(resp_body.decode("utf-8"))
                        out = _gemini_to_openai_response(gemini_json, model)
                    except Exception:
                        await _write_error_response(writer, 502, "Gemini response parse failed")
                        return
                    payload = _json.dumps(out, ensure_ascii=False).encode()
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                    writer.write(payload)
                    await writer.drain()
                    stats["passthroughOk"] += 1
                    writer.close(); return

                # ── 流式：Gemini SSE → OpenAI SSE ──
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                async for chunk in resp.aiter_bytes():
                    line = chunk.decode("utf-8", errors="replace")
                    for raw in line.split("\n"):
                        raw = raw.strip()
                        if not raw.startswith("data:"):
                            continue
                        data_str = raw[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            gemini_chunk = _json.loads(data_str)
                        except Exception:
                            continue
                        oai_chunk = _gemini_chunk_to_openai(gemini_chunk, model)
                        if oai_chunk:
                            writer.write(("data: " + _json.dumps(oai_chunk, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
                stats["passthroughOk"] += 1
                writer.close(); return

        # ── 其他路径：透传原生端点 ──
        upstream_url = f"{_GEMINI_NATIVE_BASE}{path}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False) as c:
            req = c.build_request(method, upstream_url, headers=api_headers, content=body if body else None)
            resp = await c.send(req, stream=True)
            status, _ = await _write_response(writer, resp, stats=stats)
            if status and status >= 400:
                logger.warning(f"[{label}] gemini-native {path} HTTP {status}")
            return
    except Exception as e:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] gemini-native proxy exception")
        try:
            await _write_error_response(writer, 503, f"Gemini proxy error: {e}")
        except Exception:
            pass



