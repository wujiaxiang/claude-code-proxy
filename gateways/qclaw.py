# QClaw 网关模块（从 server.py 拆分，零行为变化）
# 此处符号原样剪切自 server.py，逻辑/参数/返回值/常量/正则均未改动。
import os
import json
import base64
import sys


# QClaw 网关只接受标准 OpenAI chat completion 字段，非标准字段会导致 9002
_QCLAW_ALLOWED_KEYS = {
    "model", "messages", "max_tokens", "max_completion_tokens",
    "stream", "temperature", "top_p", "stop", "tools", "tool_choice",
    "frequency_penalty", "presence_penalty", "n", "user", "seed",
    "logprobs", "top_logprobs", "response_format", "logit_bias",
    "cache_control",
}

def _clean_qclaw_body(body: dict) -> dict:
    """清理 body 中 QClaw 网关不认识的字段，避免非标准参数导致 9002。"""
    from server import logger
    cleaned = {}
    removed = []
    for k, v in body.items():
        if k in _QCLAW_ALLOWED_KEYS:
            cleaned[k] = v
        else:
            removed.append(k)
    if removed:
        logger.info(f"🧹 QClaw body cleaned: removed keys={removed}")
    return cleaned


async def _passthrough_to_qclaw(
    litellm_req: dict,
    request,  # type: ignore - MessagesRequest defined later
    original_model: str,
    request_id: str,
):
    """绕过 litellm，直接用 httpx 打 QClaw 网关的 /chat/completions。
    用于 9002 重试——litellm 内部缓存状态重置不彻底，只能绕过去。
    """
    from server import get_http_client, QCLAW_API_KEY, QCLAW_BASE_URL, StreamingResponse, HTTPException, _convert_oai_to_anthropic
    mapped_model = litellm_req["model"]
    if "/" in mapped_model:
        mapped_model = mapped_model.split("/", 1)[1]  # openai/xxx -> xxx

    body = {
        "model": mapped_model,
        "messages": litellm_req["messages"],
        "max_tokens": litellm_req.get("max_tokens") or litellm_req.get("max_completion_tokens", 4096),
    }
    if litellm_req.get("temperature") is not None:
        body["temperature"] = litellm_req["temperature"]
    if litellm_req.get("top_p") is not None:
        body["top_p"] = litellm_req["top_p"]
    if litellm_req.get("tools"):
        body["tools"] = litellm_req["tools"]

    headers = {
        "Authorization": f"Bearer {QCLAW_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "OpenAI/JS 6.39.1",  # 上游拒绝 python-httpx 默认 UA
    }

    client = await get_http_client()
    url = QCLAW_BASE_URL.rstrip("/") + "/chat/completions"

    if getattr(request, "stream", False):
        body["stream"] = True
        async def _stream():
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    error_text = await resp.aread()
                    yield f"data: {{\"error\":\"upstream {resp.status_code}: {error_text.decode('utf-8', errors='replace')[:200]}\"}}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                async for chunk in resp.aiter_bytes():
                    yield chunk
        return StreamingResponse(_stream(), media_type="text/event-stream")
    else:
        resp = await client.post(url, json=body, headers=headers, timeout=300.0)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"upstream: {resp.text[:500]}")
        data = resp.json()
        # 转换为 Anthropic 格式
        return _convert_oai_to_anthropic(data, request, original_model)


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
    """Windows DPAPI 解密（Chrome 风格 os_crypt 的 AES 密钥保护层）。"""
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # pyright: ignore[reportAttributeAccessIssue] - ctypes.windll 仅 Windows 存在，此函数仅 Windows 调用，Linux 下静态检查误报
    kernel32 = ctypes.windll.kernel32  # pyright: ignore[reportAttributeAccessIssue] - 同上，Windows 专属 DPAPI
    blob_in = _DATA_BLOB(len(encrypted_bytes),
                         ctypes.cast(ctypes.c_char_p(encrypted_bytes),
                                     ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB)
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed (WinError {ctypes.get_last_error()})")  # pyright: ignore[reportAttributeAccessIssue] - get_last_error 仅 Windows，此路径仅 Windows 执行
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _decrypt_qclaw_api_key() -> str:
    """从 QClaw 本地存储解密 API Key。

    解密链路（Windows）：
      Local State → os_crypt.encrypted_key (DPAPI) → AES-256 密钥
      app-store.json → authGateway.providers.qclaw.apiKey.cipherText (v10)
      → AES-256-GCM 解密 → API Key (sk-...)

    环境变量 QCLAW_API_KEY 优先；解密失败时返回空字符串（启动诊断会告警）。
    """
    from server import logger
    env_key = os.environ.get("QCLAW_API_KEY", "").strip()
    if env_key:
        return env_key

    try:
        appdata = os.environ.get("APPDATA", "")
        app_store = os.path.join(appdata, "QClaw", "app-store.json")
        local_state = os.path.join(appdata, "QClaw", "Local State")

        if not os.path.exists(app_store):
            logger.warning(f"QClaw app-store.json not found: {app_store}")
            return ""

        with open(app_store, "r", encoding="utf-8") as f:
            store = json.load(f)
        entry = store.get("authGateway.providers.qclaw.apiKey")
        if entry is None:
            logger.warning("authGateway.providers.qclaw.apiKey not found in app-store.json")
            return ""
        cipher_b64 = entry["cipherText"] if isinstance(entry, dict) else entry
        raw = base64.b64decode(cipher_b64)

        if sys.platform == "win32":
            # Chrome v10: 3-byte prefix + 12-byte nonce + ciphertext + 16-byte tag
            if raw[:3] != b"v10":
                logger.warning(f"Unexpected cipher prefix: {raw[:3]!r}")
                return ""
            if not os.path.exists(local_state):
                logger.warning(f"QClaw Local State not found: {local_state}")
                return ""
            with open(local_state, "r", encoding="utf-8") as f:
                ls = json.load(f)
            enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
            if enc_key[:5] != b"DPAPI":
                logger.warning("Unexpected key prefix (expected DPAPI)")
                return ""
            aes_key = _dpapi_unprotect(enc_key[5:])
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            encrypted = raw[3:]
            nonce = encrypted[:12]
            ct_and_tag = encrypted[12:]
            return AESGCM(aes_key).decrypt(nonce, ct_and_tag, None).decode("utf-8").strip()
        else:
            logger.warning(f"QClaw API key auto-decrypt not implemented for platform: {sys.platform}")
            return ""
    except Exception as e:
        logger.warning(f"Failed to decrypt QClaw API key: {e}")
        return ""


def _qclaw_provider(req, litellm_req, orig):
    """QClaw 上游直连（OpenAI 兼容接口）"""
    from server import QCLAW_API_KEY, QCLAW_BASE_URL, logger
    litellm_req["api_key"] = QCLAW_API_KEY
    litellm_req["api_base"] = QCLAW_BASE_URL
    litellm_req["extra_headers"] = {"User-Agent": "OpenAI/JS 6.39.1"}  # 上游拒绝 python-httpx 默认 UA
    # 清理 litellm 内部字段和 Anthropic 专属字段，防止上游拒绝非标准参数
    for k in ("stop", "top_k", "metadata", "thinking", "reasoning",
              "reasoning_effort", "extra_body", "provider_specific_fields",
              "custom_llm_provider", "model_info"):
        litellm_req.pop(k, None)
    msgs = litellm_req.get("messages", [])
    if not any(m.get("role") == "system" for m in msgs):
        msgs.insert(0, {"role": "system", "content": "You are Claude, a helpful AI assistant."})
    # 恢复上游原始 max_tokens（此值可能在 convert 阶段被 OpenAI/Gemini 截断）
    original_max = litellm_req.pop("_original_max_tokens", None)
    if original_max is not None and original_max != litellm_req.get("max_completion_tokens"):
        litellm_req["max_completion_tokens"] = original_max
        logger.debug(f"🐙 QClaw: restored max_tokens {litellm_req['max_completion_tokens']} -> {original_max}")

    req.model = orig
    max_tok = litellm_req.get("max_completion_tokens", "N/A")
    logger.debug(f"🐙 QClaw: {req.model} max_tokens={max_tok} stream={litellm_req.get('stream')} extra_body=(not set)")
    return None  # 继续走 LiteLLM
