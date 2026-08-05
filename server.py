from fastapi import FastAPI, Request, HTTPException
import uvicorn
import logging
from logging.handlers import TimedRotatingFileHandler
import json
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Any, Optional, Union, Literal
import httpx
import os
import socket
import base64
import asyncio
import struct
from urllib.parse import urlparse
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.responses import HTMLResponse
import litellm
import uuid
import time
from dotenv import load_dotenv
import re
from datetime import datetime
from pathlib import Path
import sys

# 破解网关公共能力（额度/签到/刷新状态查询 + tc 解密）
try:
    import crack_common
except Exception:
    crack_common = None

# Load environment variables from .env file
load_dotenv()

# Debug mode
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
LOG_FILE = os.environ.get("LOG_FILE", "")  # 非空则同时输出到文件
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "7"))
LOG_ROTATE_WHEN = os.environ.get("LOG_ROTATE_WHEN", "midnight")
LOG_ROTATE_INTERVAL = int(os.environ.get("LOG_ROTATE_INTERVAL", "1"))

# Response cache configuration
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "True").lower() == "true"
CACHE_MAX_SIZE = int(os.environ.get("CACHE_MAX_SIZE", "500"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
CACHE_MAX_ITEM_SIZE_KB = int(os.environ.get("CACHE_MAX_ITEM_SIZE_KB", "100"))

# Configure logging
_log_level = logging.DEBUG if DEBUG else logging.INFO
_log_fmt = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(level=_log_level, format=_log_fmt)
logger = logging.getLogger(__name__)

# Module-level timeout constant for target forwarding engine (used in _handle_target_request)
# Can be monkeypatched in tests for fast timeout simulation
_TARGET_HTTPX_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

# ═══════════════════════════════════════════════════════════════════════════════
# 本地 token 估算（tiktoken）— 上游 QClaw 网关不返回 usage，需自行估算
# ══════════════════════════════════════════════════════════════════════════════
import tiktoken as _tiktoken

# 缓存 tokenizer 实例（每个 encoding 只加载一次）
_TIKTOKEN_CACHE: Dict[str, "_tiktoken.Encoding"] = {}


def _get_tokenizer(model_name: str) -> "_tiktoken.Encoding":
    """根据模型名选合适的 tokenizer。

    QClaw 透传模型（DeepSeek/GLM/Kimi/MiniMax）以及 Claude 都用 cl100k_base 做近似估算——
    这是经验上最接近的通用 tokenizer，估算误差通常在 ±10% 内，足够给 Claude Code 显示用量。
    """
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

def _cleanup_old_log_files(log_file: str, retention_days: int):
    if not log_file or retention_days <= 0:
        return
    try:
        log_path = Path(log_file).expanduser()
        if not log_path.parent.exists():
            return
        cutoff_ts = time.time() - retention_days * 86400
        for candidate in log_path.parent.glob(f"{log_path.name}*"):
            if not candidate.is_file() or candidate == log_path:
                continue
            if candidate.stat().st_mtime < cutoff_ts:
                candidate.unlink()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to cleanup old logs: {e}")


# ── 文件日志 Handler（LOG_FILE 非空时启用，自动轮转+清理）──────────
if LOG_FILE:
    _log_path = Path(LOG_FILE).expanduser()
    _log_path.parent.mkdir(parents=True, exist_ok=True)

    _fh = TimedRotatingFileHandler(
        filename=str(_log_path),
        when=LOG_ROTATE_WHEN,
        interval=max(1, LOG_ROTATE_INTERVAL),
        backupCount=max(0, LOG_RETENTION_DAYS),
        encoding="utf-8",
    )
    _fh.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    _fh.setFormatter(logging.Formatter(_log_fmt))
    logging.getLogger().addHandler(_fh)
    _cleanup_old_log_files(str(_log_path), LOG_RETENTION_DAYS)
    logger.warning(
        f"📄 File log enabled: path={_log_path} rotate={LOG_ROTATE_WHEN}/{LOG_ROTATE_INTERVAL} retention_days={LOG_RETENTION_DAYS}"
    )

# ── 网关专用日志（codebuddy / trae-work 独立文件，2026-08-05）─────
# 破解网关请求量大且排查需要细粒度请求/响应日志（content_filter 拦截、
# tool_call 变体等），独立文件避免污染 proxy.log 主日志。
# 命名规则：<LOG_FILE 同名目录>/<label>.log（如 proxy.log → codebuddy.log）。
# 仅 DEBUG 模式写入完整请求/响应（含 body 摘要）；INFO 只记关键事件。
_GATEWAY_LOG_SUFFIX = {
    "codebuddy": "codebuddy",
    "trae-work": "traework",
}


def _setup_gateway_logger(name: str) -> logging.Logger:
    """为指定网关建独立文件 logger（始终创建，路径固定 <server.py 同目录>/<name>.log）。

    不依赖 LOG_FILE/.env 配置；与 proxy.log 轮转策略同步。"""
    gw = logging.getLogger(f"gateway.{name}")
    gw.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    gw.propagate = False  # 不冒泡到 root，避免重复写 proxy.log
    try:
        # 始终创建文件（与 LOG_FILE 是否设置无关）
        # 路径：proxy.log 同目录/<name>.log，如 /root/.../codebuddy.log
        suffix = _GATEWAY_LOG_SUFFIX.get(name, name)
        if LOG_FILE:
            _base = Path(LOG_FILE).expanduser()
            _gw_path = _base.with_name(f"{_base.stem}-{suffix}.log")
        else:
            # 无 LOG_FILE 时：基于 server.py 同目录
            import __main__
            _server_dir = Path(__file__).parent.resolve()
            _gw_path = _server_dir / f"{suffix}.log"
        _gh = TimedRotatingFileHandler(
            filename=str(_gw_path),
            when=LOG_ROTATE_WHEN,
            interval=max(1, LOG_ROTATE_INTERVAL),
            backupCount=max(0, LOG_RETENTION_DAYS),
            encoding="utf-8",
        )
        _gh.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        _gh.setFormatter(logging.Formatter(_log_fmt))
        gw.addHandler(_gh)
        _cleanup_old_log_files(str(_gw_path), LOG_RETENTION_DAYS)
        logger.warning(f"📄 Gateway log enabled: {name} → {_gw_path}")
    except Exception as _e:
        logger.warning(f"⚠️  Gateway log setup failed for {name}: {_e}")
        gw.addHandler(logging.StreamHandler())
        gw.handlers[-1].setFormatter(logging.Formatter(_log_fmt))
    return gw


codebuddy_logger = _setup_gateway_logger("codebuddy")
traework_logger = _setup_gateway_logger("trae-work")

# Configure uvicorn to be quieter
import uvicorn

# Tell uvicorn's loggers to be quiet
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


# Create a filter to block any log messages containing specific strings
class MessageFilter(logging.Filter):
    def filter(self, record):
        # Block messages containing these strings
        blocked_phrases = [
            "LiteLLM completion()",
            "HTTP Request:",
            "selected model name for cost calculation",
            "utils.py",
            "cost_calculator",
        ]

        if hasattr(record, "msg") and isinstance(record.msg, str):
            for phrase in blocked_phrases:
                if phrase in record.msg:
                    return False
        return True


# Apply the filter to the root logger to catch all messages
root_logger = logging.getLogger()
root_logger.addFilter(MessageFilter())


# Custom formatter for model mapping logs
class ColorizedFormatter(logging.Formatter):
    """Custom formatter to highlight model mappings"""

    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record):
        if record.levelno == logging.debug and "MODEL MAPPING" in record.msg:
            # Apply colors and formatting to model mapping logs
            return f"{self.BOLD}{self.GREEN}{record.msg}{self.RESET}"
        return super().format(record)


# Apply custom formatter to console handler
for handler in logger.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setFormatter(
            ColorizedFormatter("%(asctime)s - %(levelname)s - %(message)s")
        )

from contextlib import asynccontextmanager

# ─── 全局 httpx 连接池（复用连接，避免端口/连接泄漏）───
_http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient:
    """获取全局共享的 httpx 异步客户端，复用连接池。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            trust_env=False,  # 不使用系统代理，避免代理干扰直连上游
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
            ),
        )
    return _http_client

async def _reset_litellm_clients():
    """清除 litellm 内部缓存的 HTTP 客户端 + 代理自身连接池，强制重新创建连接。
    当 QClaw 网关 upstream auth 过期返回 9002 时调用。"""
    import litellm as _llm
    # 1) 关闭代理自己的 httpx 连接池（透传路径用）
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
        logger.info("🔄 proxy http client reset")
    # 2) 清除 litellm 异步客户端
    try:
        await _llm.close_litellm_async_clients()
        logger.info("🔄 litellm async clients reset")
    except Exception as _e:
        logger.warning(f"Failed to reset litellm async clients: {_e}")
    # 3) 清除 litellm 同步客户端缓存
    try:
        if hasattr(_llm, "in_memory_llm_clients_cache"):
            cache = _llm.in_memory_llm_clients_cache
            if hasattr(cache, "cache_dict"):
                cache.cache_dict.clear()
                logger.info("🔄 litellm in-memory client cache cleared")
    except Exception as _e:
        logger.warning(f"Failed to clear litellm cache dict: {_e}")
    # 4) 用 importlib 重载 litellm 的 openai adapter 模块，强制重建 client
    try:
        import litellm.llms.openai.openai as oai_mod
        import importlib
        importlib.reload(oai_mod)
        logger.info("🔄 litellm openai adapter reloaded")
    except Exception as _e:
        logger.warning(f"Failed to reload openai adapter: {_e}")


def _is_auth_expired_error(exc: Exception) -> bool:
    """判断是否为 QClaw 网关 upstream auth 过期 (9002)。"""
    msg = str(exc).lower()
    return "9002" in msg or "该功能暂不可用" in msg


# 限流/资源耗尽错误特征：优先按 litellm 异常类型判断，兜底按消息特征匹配，
# 避免把真正的 5xx / 鉴权失败误判为 429。
_RATE_LIMIT_ERROR_KEYWORDS = (
    "ResourceExhausted",
    "Worker local total request limit reached",
    "rate_limit_error",
    "RateLimitError",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """识别 LiteLLM 抛出的限流类异常（含 qclaw 的 ResourceExhausted / Worker local ...）。

    命中后调用方应返回 HTTP 429 + Retry-After，让下游客户端（如 opencode）自动重试。
    关键字复用 _VENDOR_ERROR_MAPS，保持单点维护。
    """
    if isinstance(exc, (litellm.RateLimitError, getattr(litellm, "RouterRateLimitError", ()))):
        return True
    text = str(exc)
    if not text:
        return False
    return any(k in text for k, _s, _t, _d in _VENDOR_ERROR_MAPS)


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


_CODEBUDDY_DROP_KEYS = {
    "reasoning_effort", "reasoning", "reasoning_summary",
    "thinking", "thinking_tokens", "thinking_budget",
    "top_logprobs", "logprobs",
}

# codebuddy 上游(copilot.tencent.com)内容审查误拦短语 → 安全替换。
# 反证法实测（2026-08-04/05）：腾讯审查对以下**完整精确短语** 100% 触发
# content_filter（HTTP 200 空 SSE，finish_reason=content_filter），缺任何
# 成分（引号/连字符/某个词）都不触发。因此用精确字符串替换即可规避。
# 1) Sisyphus-Junior 子代理：oh-my-openagent 插件硬编码注入 system prompt
# 2) 主代理 Sisyphus 身份（2026-08-05 实测）：Role 段 "You are \"Sisyphus\" - ..."
#    触发组合 = 引号 Sisyphus + " - " + "Powerful AI Agent with orchestration
#    capabilities from OhMyOpenCode"，三缺一不触发（已逐一反证）。
_CODEBUDDY_SYS_REWRITES = (
    # (触发短语, 安全替换)
    ("Sisyphus-Junior - Focused executor from OhMyOpenCode.",
     "Focused task executor agent."),
    ('You are "Sisyphus" - Powerful AI Agent with orchestration capabilities from OhMyOpenCode.',
     'You are "Sisyphus" - a capable coding agent with strong orchestration abilities.'),
)


def _clean_codebuddy_body(body: dict) -> dict:
    """剥离 codebuddy 上游(copilot.tencent.com)不兼容的推理类参数。
    tools/tool_choice 必须保留——子代理工具调用依赖请求体 tools 字段，
    强行剥离会导致子代理无法调用工具(2026-08-04 回退)。仅剥离上游
    不支持的思考链/推理参数，避免触发内容过滤。
    另做 system prompt 精确短语热重写（_CODEBUDDY_SYS_REWRITES），规避
    上游内容审查误拦（子代理 Sisyphus-Junior + 主代理 Sisyphus 身份）。"""
    removed = []
    replaced_system_prompts = []
    for k in list(body.keys()):
        if k in _CODEBUDDY_DROP_KEYS:
            removed.append(k)
            del body[k]
    
    # 系统提示词替换（防止 CodeBuddy 内容审查拦截）
    if "messages" in body:
        for msg in body["messages"]:
            if msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    original_content = content
                    for _trigger, _replacement in _CODEBUDDY_SYS_REWRITES:
                        if _trigger in content:
                            content = content.replace(_trigger, _replacement)
                            msg["content"] = content
                            replaced_system_prompts.append(original_content)
                # 若 content 为列表类型时跳过（复杂文本段落），保持保守兼容
    
    if removed:
        logger.info(f"🧹 Codebuddy body cleaned: removed keys={removed}")
    if replaced_system_prompts:
        logger.info(f"🧹 Codebuddy sys prompt rewritten: {len(replaced_system_prompts)} system message(s)")
    return body


async def _passthrough_to_qclaw(
    litellm_req: dict,
    request,  # type: ignore - MessagesRequest defined later
    original_model: str,
    request_id: str,
):
    """绕过 litellm，直接用 httpx 打 QClaw 网关的 /chat/completions。
    用于 9002 重试——litellm 内部缓存状态重置不彻底，只能绕过去。
    """
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


def _convert_oai_to_anthropic(oai_data: dict, request, original_model: str):  # type: ignore
    """将 OpenAI chat completion 响应转换为 Anthropic messages 格式. 简化版."""
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

    return MessagesResponse(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        type="message",
        role="assistant",
        model=original_model,
        content=content_blocks or [{"type": "text", "text": ""}],
        stop_reason=choice.get("finish_reason") or "stop",
        stop_sequence=None,
        usage=Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
    )


@asynccontextmanager
async def lifespan(app):
    # 网关抓包：CAPTURE_GATEWAY=true 时激活
    if os.environ.get("CAPTURE_GATEWAY", "").lower() == "true":
        try:
            from _gateway_capture import activate_capture, get_capture_file
            activate_capture()
            logger.info(f"📡 Gateway capture activated → {get_capture_file()}")
        except Exception as _ce:
            logger.warning(f"Failed to activate gateway capture: {_ce}")

    # 启动诊断：验证 QClaw 链路是否正常
    import httpx as _httpx
    _qclaw_diag_base = QCLAW_BASE_URL
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(10.0), trust_env=False) as _diag:
            _r = await _diag.post(
                f"{_qclaw_diag_base}/chat/completions",
                json={"model": "pool-deepseek-v4-flash", "messages": [{"role": "system", "content": "hi"}, {"role": "user", "content": "hi"}], "max_tokens": 5},
                headers={"Authorization": f"Bearer {QCLAW_API_KEY}", "Content-Type": "application/json", "User-Agent": "OpenAI/JS 6.39.1"},
            )
            logger.info(f"startup diag: QClaw upstream = {_r.status_code}")
    except Exception as _e:
        logger.warning(f"startup diag: QClaw upstream unreachable: {_e}")
    # 预热下游模型列表缓存（copilot/openai 等能从 /models 拉取的 provider）
    if PREFERRED_PROVIDER in ("copilot", "openai"):
        try:
            await _fetch_downstream_models()
            logger.info(f"startup: preloaded {len(_DOWNSTREAM_MODELS_CACHE or [])} downstream models")
        except Exception as _me:
            logger.warning(f"startup: failed to preload downstream models: {_me}")

    # ── 破解类 target：缺 key 时自动调用破解工具提取 ──
    for t in _TARGETS:
        if t.get("category") == "crack" and t.get("enabled", True):
            if not _cfg.resolve_secret(t, _SECRETS) and t.get("crackTool"):
                print(f"🔓 [{t['label']}] 缺 token，调用破解工具 {t['crackTool']} ...")
                _run_crack_tool(t["crackTool"])
            else:
                has = bool(_cfg.resolve_secret(t, _SECRETS))
                print(f"🔑 [{t['label']}] token {'已就绪' if has else '缺失（跳过破解，dashboard 可补）'}")

    # ── 启动所有 target 驱动端口（8082 copilot, 8084 codebuddy, 8085 qclaw, 8090-8094 等）──
    # 8081 Anthropic 由 uvicorn FastAPI 处理（不在此处启动）
    for t in _TARGETS:
        if not t.get("enabled", True):
            print(f"⏭️  [{t['label']}] disabled, skip")
            continue
        srv = await _vendor_server("0.0.0.0", t["listenPort"], t)
        _target_servers[t["listenPort"]] = srv

    # ── 预初始化聚合网关引擎（避免首请求前 /api/aggregate/status 显示"未配置"）──
    _agg_preinit = next((t for t in _TARGETS if t.get("handler") == "aggregator" and t.get("enabled", True)), None)
    if _agg_preinit is not None:
        global _AGGREGATOR_ENGINE, _AGGREGATOR_CONFIG_SIG
        import aggregator as _agg
        _AGGREGATOR_ENGINE = _agg.AggregatorEngine.from_target(_agg_preinit)
        _AGGREGATOR_CONFIG_SIG = json.dumps(_agg_preinit, sort_keys=True, ensure_ascii=False)
        print(f"🚀 [aggregator] 聚合网关引擎预初始化（{len(_agg_preinit.get('virtualModels', {}))} 个虚拟模型）")

    # ── 启动配置热重载 watcher ──
    watcher_task = asyncio.create_task(_config_watcher())
    aggregator_prober_task = asyncio.create_task(_aggregator_prober())

    yield

    # 停止配置 watcher
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass

    # 停止聚合网关探测 task
    aggregator_prober_task.cancel()
    try:
        await aggregator_prober_task
    except asyncio.CancelledError:
        pass

    # 停掉透明反代服务器
    for srv in _target_servers.values():
        srv.close()
        await srv.wait_closed()
    _target_servers.clear()
    # 清理连接池
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None

app = FastAPI(lifespan=lifespan)

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body() if request.method == "POST" else b""
    try:
        body_json = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        body_json = {"raw": body.decode("utf-8", errors="replace")[:2000]}
    logger.error(
        f"❌ VALIDATION ERROR: {exc.errors()}\n"
        f"📋 PATH: {request.url.path}\n"
        f"📋 MODEL: {body_json.get('model', 'N/A')}\n"
        f"📋 THINKING: {body_json.get('thinking', 'N/A')}\n"
        f"📋 KEYS: {list(body_json.keys())}\n"
        f"📋 BODY PREVIEW: {json.dumps(body_json, ensure_ascii=False)[:2000]}"
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors(), "body": body_json})

# Get API keys from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Get custom base URLs from environment
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL")

# Gemini thought_signature 存储：tool_use_id → signature
_thought_signatures: Dict[str, str] = {}

# Vertex AI (Google Cloud)
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "unset")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "unset")
USE_VERTEX_AUTH = os.environ.get("USE_VERTEX_AUTH", "False").lower() == "true"

# ─── QClaw 上游直连配置 ───
# 上游 LLM 接口（OpenAI 兼容），从 QClaw 客户端本地存储解密 API Key
QCLAW_BASE_URL = os.environ.get("QCLAW_BASE_URL", "https://mmgrcalltoken.3g.qq.com/aizone/v1")


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
    """Windows DPAPI 解密（Chrome 风格 os_crypt 的 AES 密钥保护层）。"""
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
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
        raise OSError(f"CryptUnprotectData failed (WinError {ctypes.get_last_error()})")
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


QCLAW_API_KEY = _decrypt_qclaw_api_key()
if QCLAW_API_KEY:
    print(f"🔑 QClaw API Key decrypted: {QCLAW_API_KEY[:12]}...{QCLAW_API_KEY[-4:]}")
else:
    print("⚠️  QClaw API Key not available (set QCLAW_API_KEY env or ensure QClaw client is logged in)")

# ─── GitHub Copilot Enterprise 配置 ───
# COPILOT_GHE_TOKEN：私密凭据，已收敛到 secrets.json copilot_token 字段（唯一事实源，
# 与 8082 企业 GHE target 的 secretRef 同源）。模块加载时从 env 读一次作初始兜底；
# _load_vendor_targets() / _reload_targets() / _refresh_secrets() 热重载时从 secrets.json
# 覆盖（dashboard 可编辑、热生效）。其余 COPILOT_* 为纯配置，留在 .env。
COPILOT_GHE_TOKEN = os.environ.get("COPILOT_GHE_TOKEN", "")
COPILOT_GHE_HOST = os.environ.get("COPILOT_GHE_HOST", "copilot-api.bmw.ghe.com")
COPILOT_INTEGRATION_ID = os.environ.get("COPILOT_INTEGRATION_ID", "copilot-developer-cli")
# api_base for LiteLLM（不含路径，LiteLLM 会追加 /chat/completions）
COPILOT_API_BASE = f"https://{COPILOT_GHE_HOST}"
# Copilot 模型映射（opus→big, sonnet→medium, haiku→small）
COPILOT_BIG_MODEL    = os.environ.get("COPILOT_BIG_MODEL",    "claude-sonnet-4.6")
COPILOT_MEDIUM_MODEL = os.environ.get("COPILOT_MEDIUM_MODEL", "claude-sonnet-4.6")
COPILOT_SMALL_MODEL  = os.environ.get("COPILOT_SMALL_MODEL",  "claude-haiku-4.5")

# Get preferred provider
PREFERRED_PROVIDER = os.environ.get("PREFERRED_PROVIDER", "openai").lower()
valid_providers = ("openai", "anthropic", "qclaw", "gemini", "gemini-openai", "copilot")
if PREFERRED_PROVIDER not in valid_providers:
    print(f"Warning: Unknown PREFERRED_PROVIDER '{PREFERRED_PROVIDER}', falling back to 'openai'")
    PREFERRED_PROVIDER = "openai"

print(f"🚀 Preferred provider: {PREFERRED_PROVIDER}")

# 注册 QClaw 模型到 LiteLLM，避免 "model isn't mapped" 错误
if PREFERRED_PROVIDER in ("qclaw",):
    _qclaw_all_models = {
        m: {
            "max_tokens": 16384, "input_cost_per_token": 0, "output_cost_per_token": 0,
            "litellm_provider": "openai", "mode": "chat",
        }
        for m in [
            "modelroute",
            "pool-hy3-preview",
            "pool-deepseek-v4-pro",
            "pool-deepseek-v4-flash",
            "pool-glm-5.2",
            "pool-glm-5.2-night",
            "pool-glm-5.1",
            "pool-kimi-k2.7-code-highspeed",
            "pool-kimi-k2.6",
            "pool-minimax-m3",
            "pool-minimax-m2.7",
        ]
    }
    litellm.register_model(_qclaw_all_models)
    print("🐙 QClaw models registered in LiteLLM")

# 注册 Copilot 模型到 LiteLLM，避免 "model isn't mapped" 错误
if PREFERRED_PROVIDER == "copilot":
    _copilot_models = {
        m: {"max_tokens": 64000, "input_cost_per_token": 0, "output_cost_per_token": 0,
            "litellm_provider": "openai", "mode": "chat"}
        for m in [
            COPILOT_BIG_MODEL, COPILOT_MEDIUM_MODEL, COPILOT_SMALL_MODEL,
            # 全量可用模型（来自 /models API，2026-06）
            "claude-haiku-4.5", "claude-sonnet-4.5", "claude-sonnet-4.6",
            "claude-opus-4.5", "claude-opus-4.6", "claude-opus-4.8",
            "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5-mini",
            "gpt-4.1", "gpt-4.1-2025-04-14",
            "gpt-4o-mini", "gpt-4o-mini-2024-07-18",
            "gpt-3.5-turbo", "gpt-3.5-turbo-0613",
            "gemini-2.5-pro",
        ]
    }
    litellm.register_model(_copilot_models)
    print("🤖 Copilot models registered in LiteLLM")

# Get model mapping configuration from environment
# Default to latest OpenAI models if not set
BIG_MODEL = os.environ.get("BIG_MODEL", "gpt-4.1")
MEDIUM_MODEL = os.environ.get("MEDIUM_MODEL", os.environ.get("BIG_MODEL", "gpt-4.1"))
SMALL_MODEL = os.environ.get("SMALL_MODEL", "gpt-4.1-mini")

import config_store as _cfg

# ─── 统一透传引擎配置（targets.json 驱动）───
_VENDOR_RETRY_AFTER = int(os.environ.get("VENDOR_RETRY_AFTER_SECONDS", "3"))
_TARGETS: list = []
_SECRETS: dict = {}
_TARGET_STATS: Dict[str, dict] = {}
# 模型级统计：{ label: { model_name: {"requests": N, "ok": N, "err": N, "translated429": N} } }
_MODEL_STATS: Dict[str, Dict[str, Dict[str, int]]] = {}
# 模型别名/转发目标配置（targets.json 顶层 models[] + modelDefaults）
_MODELS_CFG: dict = {"models": [], "modelDefaults": {"defaultPort": 8082}}

# ─── 聚合网关（8080）单例引擎 + 重载去重签名 ───
_AGGREGATOR_ENGINE = None  # type: ignore
_AGGREGATOR_CONFIG_SIG: Optional[str] = None

def _bump_model_stats(label: str, model: str, outcome: str):
    """记录模型级统计。outcome: 'ok' | 'err' | 'translated429'"""
    models = _MODEL_STATS.setdefault(label, {})
    s = models.setdefault(model, {"requests": 0, "ok": 0, "err": 0, "translated429": 0})
    s["requests"] += 1
    if outcome in s:
        s[outcome] += 1

_VENDOR_ERROR_PATTERNS = [
    re.compile(r'"ResourceExhausted"'),
    re.compile(r'Worker local total request limit reached', re.IGNORECASE),
    re.compile(r'"(error_)?code"\s*:\s*"?(rate_limit_exceeded|too_many_requests)"?', re.IGNORECASE),
    re.compile(r'"type"\s*:\s*"rate_limit_error"', re.IGNORECASE),
]

# ─── 上游错误码映射表（数据驱动，新增网关只需追加一行）───
# 透传网关遇到下列「字段特征」（子串匹配，大小写敏感）即把上游错误体
# 标准化为 (目标 HTTP 状态码, SSE error type)，让下游客户端（opencode 等）
# 按标准错误重试/降级，而不是把伪成功/5xx 透传导致 UnknownError。
# 字段特征, 目标状态码, SSE error type, 说明
_VENDOR_ERROR_MAPS = [
    ("ResourceExhausted", 429, "rate_limit_error", "qclaw/nvidia/openrouter 资源耗尽（并发限制）"),
    ("Worker local total request limit reached", 429, "rate_limit_error", "nvidia/openrouter 本地并发已满"),
    ("rate_limit_exceeded", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("too_many_requests", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("RateLimitError", 429, "rate_limit_error", "litellm 限流异常类名"),
    ("rate-limited", 429, "rate_limit_error", "openrouter 免费池上游限流（temporarily rate-limited upstream）"),
]


def _map_upstream_error(body_text: str):
    """根据错误映射表把上游错误体转成 (http_status, sse_error_type)。

    匹配顺序：
    1. 先按 _VENDOR_ERROR_MAPS 做子串匹配——覆盖无标准 error 信封的格式，如：
       - openrouter: {"code":502,"message":"Upstream error from Nvidia: ResourceExhausted: ..."}
       - nvidia:     裸字符串 "ResourceExhausted: Worker local total request limit reached (32/32)"
    2. 回退到标准 OpenAI error 信封 {"error":{...}} 含 _VENDOR_ERROR_PATTERNS 特征。
    返回 None 表示不是可识别的限流/错误信封。
    """
    if not body_text:
        return None
    for _keyword, _status, _err_type, _desc in _VENDOR_ERROR_MAPS:
        if _keyword in body_text:
            return (_status, _err_type)
    if re.search(r'"error"\s*:', body_text):
        if any(p.search(body_text) for p in _VENDOR_ERROR_PATTERNS):
            return (429, "rate_limit_error")
    return None


def _vendor_body_retryable(body_text: str) -> bool:
    """判断上游错误体是否应被转成可重试的标准错误（429）。"""
    return _map_upstream_error(body_text) is not None


async def _aggregate_codebuddy_stream(target, upstream_url, fwd_headers, body_json, label):
    """codebuddy 非流式请求转流式聚合：stream:true 重试，收集 SSE 拼装完整 JSON。

    上游（copilot.tencent.com）拒绝非流式 chat（11101），但流式可用。
    返回 OpenAI 格式完整响应 dict；失败返回 None（调用方透传上游 400）。
    """
    import json as _json
    retry_body = dict(body_json)
    retry_body["stream"] = True
    payload = _json.dumps(retry_body, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
            req = client.build_request("POST", upstream_url, headers=fwd_headers, content=payload)
            resp = await client.send(req, stream=True)
            if resp.status_code >= 400:
                await resp.aread()
                return None

            # ── 聚合 SSE chunks ──
            chunks = []          # choices 的 delta 序列（按 index 分组）
            usage = None
            created = int(time.time())
            resp_id = ""
            model = retry_body.get("model", "")
            finish_reason = None
            async for raw in resp.aiter_lines():
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    chunk = _json.loads(data_str)
                except Exception:
                    continue
                if not resp_id:
                    resp_id = chunk.get("id", "")
                if chunk.get("model"):
                    model = chunk["model"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for c in chunk.get("choices", []) or []:
                    idx = c.get("index", 0)
                    while len(chunks) <= idx:
                        chunks.append({"role": "assistant", "content": "", "tool_calls": []})
                    delta = c.get("delta", {}) or {}
                    if delta.get("content"):
                        chunks[idx]["content"] += delta["content"]
                    if delta.get("reasoning_content"):
                        chunks[idx].setdefault("reasoning_content", "")
                        chunks[idx]["reasoning_content"] += delta["reasoning_content"]
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            while len(chunks[idx]["tool_calls"]) <= tc.get("index", 0):
                                chunks[idx]["tool_calls"].append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            tgt = chunks[idx]["tool_calls"][tc.get("index", 0)]
                            if tc.get("id"):
                                tgt["id"] = tc["id"]
                            fn = tc.get("function", {}) or {}
                            if fn.get("name"):
                                tgt["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                tgt["function"]["arguments"] += fn["arguments"]
                    if c.get("finish_reason"):
                        finish_reason = c["finish_reason"]

            choices = [{
                "index": i,
                "message": c,
                "finish_reason": finish_reason or "stop",
            } for i, c in enumerate(chunks)]
            if not choices:
                # 无任何 chunk（异常空响应），不拼装
                return None
            return {
                "id": resp_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": choices,
                "usage": usage or {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
    except Exception as e:
        codebuddy_logger.warning(f"[{label}] codebuddy aggregate failed: {e}")
        return None


# ─── HTTP 代理共享工具函数（所有端口统一用，不要各写各的） ───

# 响应头透传时剔除的字段：
# - transfer-encoding/connection/content-length：由代理按实际 body 重算
# - content-encoding：httpx 已自动解压 body（gzip/br/deflate），再透传该头会让
#   客户端对"已解压的明文"再解压一次 → 报 "incorrect header check"（openrouter 实测）
_PROXY_STRIP_RESP_HEADERS = frozenset(("transfer-encoding", "connection", "content-length", "content-encoding"))

async def _parse_http_request(reader):
    """统一 HTTP 请求解析。
    返回 (method, path, raw_path, headers, body)，请求无效时全返回 None。
    """
    try:
        req_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not req_line:
            return None, None, None, None, None
        parts = req_line.decode("utf-8", errors="replace").strip().split(" ", 2)
        method = parts[0] if len(parts) > 0 else "GET"
        raw_path = parts[1] if len(parts) > 1 else "/"

        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                break
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        content_len = int(headers.get("content-length", 0))
        body = b""
        if content_len > 0:
            # reader.read(n) 可能返回少于 n 字节，必须循环读满
            while len(body) < content_len:
                remaining = content_len - len(body)
                chunk = await asyncio.wait_for(reader.read(remaining), timeout=30)
                if not chunk:
                    break
                body += chunk
        elif headers.get("transfer-encoding", "").lower() == "chunked":
            # 处理分块编码（OpenCode 等客户端可能使用）
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
                chunk_size_str = line.decode("utf-8", errors="replace").strip()
                if not chunk_size_str:
                    continue
                try:
                    chunk_size = int(chunk_size_str, 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    break
                # reader.read(n) 同上的问题，必须循环读满
                chunk_data = b""
                while len(chunk_data) < chunk_size:
                    remaining = chunk_size - len(chunk_data)
                    part = await asyncio.wait_for(reader.read(remaining), timeout=30)
                    if not part:
                        break
                    chunk_data += part
                body += chunk_data
                await asyncio.wait_for(reader.readline(), timeout=10)  # 吃掉 \r\n

        parsed = urlparse(raw_path)
        return method, parsed.path, raw_path, headers, body
    except asyncio.TimeoutError:
        logger.warning("_parse_http_request timeout reading request")
        return None, None, None, None, None


async def _write_error_response(writer, status, message, *, content_type="application/json", retry_after=None):
    """统一错误响应回写，带日志。"""
    body = json.dumps({"error": {"type": "proxy_error", "message": message}}, ensure_ascii=False)
    status_text = {429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}.get(status, "Error")
    header_lines = f"HTTP/1.1 {status} {status_text}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body.encode())}\r\n"
    if retry_after is not None:
        header_lines += f"Retry-After: {retry_after}\r\n"
    header_lines += "\r\n"
    logger.warning(f"_write_error_response: {status} — {message}")
    try:
        writer.write(header_lines.encode() + body.encode())
        await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


class _SseLineBuffer:
    """SSE 行缓冲：按 \\n 切完整行，处理跨 TCP chunk 粘包。

    背景：SSE 帧可能被 TCP 任意切断（一个 data: {...} JSON 跨两个 chunk）。
    纯字节透传时无所谓，但一旦要逐帧改写就必须先重组成完整行，否则会切坏 JSON。
    """
    __slots__ = ("_buf",)

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes) -> list:
        """喂入原始字节，返回本次能切出的完整行（每行含末尾 \\n）。不完整的尾部留在缓冲区。"""
        self._buf += chunk
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx == -1:
                break
            lines.append(self._buf[:idx + 1])
            self._buf = self._buf[idx + 1:]
        return lines

    def flush(self) -> bytes:
        """流结束时吐出残留（无末尾 \\n 的最后一行）。正常 SSE 不应有残留，防御性处理。"""
        rest, self._buf = self._buf, b""
        return rest


def _normalize_codebuddy_sse_line(line: bytes, *, finish_reason_to_null: bool = True) -> bytes:
    """规范化 codebuddy 上游(copilot.tencent.com)不合规的 SSE 帧。

    上游缺陷（2026-08-05 实测 kimi-k3-1）：每帧 delta 都塞满"存在但为空"的字段——
      思考帧 {"delta":{"content":"","reasoning_content":"The","function_call":null,
                       "refusal":"","tool_calls":[],"extra_fields":null}}
      正文帧 {"delta":{"content":"递归","reasoning_content":"",...}}  ← 夹带空 reasoning
    标准 OpenAI 协议下这些字段不该出现。客户端（opencode 用 Vercel AI SDK 的
    @ai-sdk/openai-compatible）按"键是否出现"判断段落边界：
      - 见 content 键 → 认为正文块开始
      - 见 tool_calls 键 → 认为工具调用段开始
    两者都会结束当前 reasoning part，下一帧再开新 part —— 思考链被切成几百个
    独立思考块（597/599 帧命中）。

    清洗规则（严格只删"空值"，有内容的字段绝不动）：
      - reasoning_content 非空 且 content == ""  → 删 content 键
      - content 非空 且 reasoning_content == ""  → 删 reasoning_content 键
      - tool_calls == [] / function_call is None / refusal == "" / extra_fields is None
        → 删该键（tool_calls 有内容时保留，否则工具调用会断）
      - finish_reason == "" → null（上游用空串，标准应为 null；独立开关控制）

    失败降级：非 data: 行 / [DONE] / JSON 解析失败一律原样返回，绝不吞帧、不中断流。
    未发生改动时返回原始 line（避免无谓重序列化，保住大部分帧的零开销）。
    """
    if not line.startswith(b"data:"):
        return line  # 空行分隔符、": keep-alive" 注释行、event: 头 → 原样透传
    raw = line[5:].strip()
    if not raw or raw == b"[DONE]":
        return line
    try:
        obj = json.loads(raw)
    except Exception:
        return line  # 畸形帧/半截 JSON → 保守原样透传
    if not isinstance(obj, dict):
        return line

    changed = False
    for choice in obj.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if delta.get("reasoning_content") and delta.get("content") == "":
                del delta["content"]
                changed = True
            if delta.get("content") and delta.get("reasoning_content") == "":
                del delta["reasoning_content"]
                changed = True
            # 剔除"存在但为空"的结构字段：上游每帧都塞 tool_calls:[] / function_call:null
            # / refusal:"" / extra_fields:null。Vercel AI SDK（@ai-sdk/openai-compatible，
            # opencode 用的就是它）按"键是否出现"判断段落边界——见到 tool_calls 键即认为
            # 工具调用段开始，结束当前 reasoning part，下一帧再开新 part，导致思考链被
            # 切成几百个独立思考块（2026-08-05 实测 597/599 帧命中）。
            # 严格只删空值：tool_calls 有内容时绝不动（工具调用是结构化数据，删了会断）。
            for _k, _empty in (("tool_calls", []), ("function_call", None),
                               ("refusal", ""), ("extra_fields", None)):
                if _k in delta and delta[_k] == _empty and type(delta[_k]) is type(_empty):
                    del delta[_k]
                    changed = True
            # 首帧的 function_call 是 {"name":"","arguments":""} 而非 null（空内容 dict），
            # 上面的 == None 匹配不到。只在所有值都为空时删，有 name/arguments 就保留。
            _fc = delta.get("function_call")
            if isinstance(_fc, dict) and not any(_fc.values()):
                del delta["function_call"]
                changed = True
        if finish_reason_to_null and choice.get("finish_reason") == "":
            choice["finish_reason"] = None
            changed = True

    if not changed:
        return line
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


async def _write_response(writer, resp, *, stats=None, write_state=None, log_sse=False, _label="", normalize_sse=False, normalize_finish_reason=True):
    """统一从 httpx 响应回写到 writer。
    自动区分流式/非流式，非 200 自动记录日志。
    返回 (status_code, body_bytes) — body_bytes=None 表示流式已写完。
    write_state: 可选的可变字典，用于跟踪 headers_sent 状态（流式场景下避免二次写状态行）
    log_sse: 可选，流式透传时解析 SSE 记录 finish_reason 诊断日志（用于排查上游
      content_filter 拦截等"200 但内容异常"场景）。开启后走行缓冲逐帧处理。
    normalize_sse: 可选，规范化上游不合规 SSE 帧（需 log_sse=True 才生效）。
      由 targets.json 的 normalizeSse 驱动，当前用于 codebuddy——修复上游思考帧
      夹带空 content 导致客户端思考链逐 token 换行的问题。
    normalize_finish_reason: normalize_sse 的子选项，把 finish_reason:"" 归一成 null。
    """
    status, body_bytes, is_stream = None, None, False
    try:
        status = resp.status_code
        reason = resp.reason_phrase or "OK"
        content_type = resp.headers.get("content-type", "")
        is_stream = "text/event-stream" in content_type

        # ── 日志：非 200 记录响应前 300 字符 ──
        if status >= 400:
            logger.warning(f"[{resp.url.host if hasattr(resp, 'url') else 'upstream'}] "
                           f"HTTP {status} {reason} | content-type: {content_type}")

        if is_stream:
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
            for k, v in resp.headers.items():
                if k.lower() not in _PROXY_STRIP_RESP_HEADERS:
                    writer.write(f"{k}: {v}\r\n".encode())
            writer.write(b"\r\n")
            # 标记 headers 已写入（流式场景：状态行+headers 已发送到 writer 缓冲区）
            if write_state is not None:
                write_state["headers_sent"] = True
            if log_sse:
                # codebuddy SSE 诊断日志（2026-08-05）：定位上游 content_filter 拦截
                # （透传下客户端收到 200 空 SSE 无法感知原因）。
                # normalize_sse=True 时额外做帧规范化（修上游夹带空 content 导致的
                # 思考链逐 token 换行，见 _normalize_codebuddy_sse_line）。
                # 用行缓冲重组跨 chunk 的半截帧——改写模式下必须，否则会切坏 JSON。
                saw_filter = False
                saw_finish = set()
                data_lines = 0
                normalized_lines = 0
                line_buf = _SseLineBuffer()

                def _diagnose(text_line: str):
                    """诊断统计——必须基于改写【前】的原始行，否则规范化自身的 bug
                    会掩盖上游真实异常。返回是否为有效 data 行。"""
                    nonlocal saw_filter, data_lines
                    if not text_line.startswith("data:"):
                        return
                    data_str = text_line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        return
                    data_lines += 1
                    try:
                        obj = json.loads(data_str)
                        for choice in obj.get("choices", []) or []:
                            fr = choice.get("finish_reason")
                            if fr:
                                saw_finish.add(fr)
                                if fr == "content_filter":
                                    saw_filter = True
                    except (json.JSONDecodeError, AttributeError):
                        pass

                def _process(raw_line: bytes) -> bytes:
                    """先诊断原始行，再按需规范化。任何异常都退回原样透传，绝不吞帧。"""
                    nonlocal normalized_lines
                    try:
                        _diagnose(raw_line.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                    if not normalize_sse:
                        return raw_line
                    try:
                        out_line = _normalize_codebuddy_sse_line(
                            raw_line, finish_reason_to_null=normalize_finish_reason
                        )
                        if out_line is not raw_line:
                            normalized_lines += 1
                        return out_line
                    except Exception:
                        return raw_line  # 双保险：规范化不应抛，再兜一层

                async for chunk in resp.aiter_bytes():
                    out = bytearray()
                    for raw_line in line_buf.feed(chunk):
                        out += _process(raw_line)
                    if out:
                        writer.write(bytes(out))
                        await writer.drain()
                # 流结束：吐残留（无末尾 \n 的最后一行，正常 SSE 不应出现）
                tail = line_buf.flush()
                if tail:
                    writer.write(_process(tail))
                    await writer.drain()

                _gw_logger = codebuddy_logger if _label == "codebuddy" else (traework_logger if _label == "trae-work" else logger)
                _norm_note = f" normalized={normalized_lines}" if normalize_sse else ""
                if saw_filter:
                    _gw_logger.warning(f"[{_label}] SSE content_filter 透传: "
                                       f"data_lines={data_lines} finish_reasons={sorted(saw_finish)}{_norm_note}")
                else:
                    _gw_logger.debug(f"[{_label}] SSE 透传完成: data_lines={data_lines} "
                                     f"finish_reasons={sorted(saw_finish) or '无'}{_norm_note}")
            else:
                async for chunk in resp.aiter_bytes():
                    writer.write(chunk)
                    await writer.drain()
            if stats:
                stats["passthroughOk"] += 1
            return status, None

        body_bytes = await resp.aread()
        body_text = body_bytes.decode("utf-8", errors="replace")
        if status >= 400:
            logger.warning(f"[{resp.url.host if hasattr(resp, 'url') else 'upstream'}] "
                           f"HTTP {status} body: {body_text[:300]}")

        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        writer.write(f"HTTP/1.1 {status} {reason}\r\n{resp_headers}Content-Length: {len(body_bytes)}\r\n\r\n".encode())
        writer.write(body_bytes)
        await writer.drain()
        if stats:
            stats["passthroughOk"] += 1
        return status, body_bytes
    except Exception:
        if status is not None and status >= 400 and body_bytes:
            logger.exception(f"Error writing {status} response to client")
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


# HTTP 标准状态码 → 原因短语映射（用于状态行改写，覆盖 400-599 常见码）
_HTTP_STATUS_REASON = {
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Payload Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    506: "Variant Also Negotiates",
    507: "Insufficient Storage",
    508: "Loop Detected",
    510: "Not Extended",
    511: "Network Authentication Required",
}


def _get_status_reason(status: int) -> str:
    """获取 HTTP 状态码对应的标准原因短语，未知码返回 'Unknown Status'。"""
    return _HTTP_STATUS_REASON.get(status, "Unknown Status")


async def _write_response_with_status_override(writer, resp, effective_status: int, *, stats=None):
    """
    非流式响应状态码改写：保持上游原始 body 字节完全一致，仅改写状态行。
    用于检测到"上游 200 但 body 嵌错误码"的场景。
    """
    try:
        # 复用 _write_response 的头部剥离逻辑
        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        # 读取原始 body 字节（resp 已在调用方 aread() 过，这里直接用 resp.content 或重新 aread）
        # 注意：调用方已执行 await resp.aread()，所以 resp.content 可用
        body_bytes = resp.content if hasattr(resp, "content") and resp.content is not None else await resp.aread()

        reason = _get_status_reason(effective_status)
        # 写状态行 + 头部 + Content-Length + body（body 字节级保持原样）
        writer.write(f"HTTP/1.1 {effective_status} {reason}\r\n{resp_headers}Content-Length: {len(body_bytes)}\r\n\r\n".encode())
        writer.write(body_bytes)
        await writer.drain()
        if stats:
            stats["passthroughError"] += 1
        return effective_status, body_bytes
    except Exception:
        logger.exception(f"Error writing status-overridden response ({effective_status}) to client")
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _resolve_auth(headers: dict, target: dict = None, provider: str = None) -> dict:
    """统一鉴权 headers 解析。
    - target 有 apikey → 注入（覆盖客户端传入的 key，vendor 场景）
    - target 有 apikeyEnv → 从环境变量读取（避免 key 明文入仓）
    - 否则透传客户端 Authorization / x-api-key
    - 返回可直接转发用的 headers dict（不含 host/connection）
    """
    fwd = {k: v for k, v in headers.items() if k not in ("host", "connection", "content-length", "transfer-encoding")}
    api_key = None

    if target:
        api_key = target.get("apikey")
        if not api_key and target.get("apikeyEnv"):
            api_key = os.environ.get(target["apikeyEnv"], "")
            if api_key:
                logger.debug(f"key: read from env ${target['apikeyEnv']} ({target.get('label', 'unknown')})")

    if api_key:
        fwd["authorization"] = f"Bearer {api_key}"
        logger.debug(f"key: injected ({target.get('label', 'unknown')})")
    elif headers.get("authorization"):
        fwd["authorization"] = headers["authorization"]
        logger.debug("key: passed through from client request")

    if target and target.get("targetHost"):
        fwd["host"] = target["targetHost"]

    return fwd


def _handler_prepare_body(target: dict, body_bytes: bytes):
    """按 handler 处理请求体：统一模型别名解析 + qclaw body 清理。
    返回 (new_body_bytes, body_json_or_None, cross_port_target_or_None)。
    cross_port_target 非 None 时表示该请求应整体路由到另一端口（调用方处理）。
    """
    handler = target.get("handler", "passthrough")
    try:
        body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        return body_bytes, None, None
    # ── 统一模型别名解析（所有 handler，含 passthrough）──
    req_model = body_json.get("model")
    mapped = None
    if req_model and isinstance(req_model, str):
        mapped = _cfg._resolve_model_alias(_MODELS_CFG, req_model)
    cross_port_target = None
    if mapped:
        if int(mapped["port"]) == int(target.get("listenPort", 0)):
            body_json["model"] = mapped["model"]
        else:
            cross_port_target = dict(mapped)
    if target.get("cleanQclawBody"):
        # QClaw 网关要求必须有 system message 且只接受白名单字段（防 9002）
        msgs = body_json.get("messages", [])
        if not any(m.get("role") == "system" for m in msgs):
            msgs.insert(0, {"role": "system", "content": "You are Claude, a helpful AI assistant."})
            body_json["messages"] = msgs
        body_json = _clean_qclaw_body(body_json)
    elif target.get("cleanCodebuddyBody"):
        # codebuddy 上游(copilot.tencent.com)不兼容 opencode 注入的思考链参数，
        # 透传会触发内容过滤拦截(#2071)；剥离而非全白名单，保留腾讯支持的正常字段。
        body_json = _clean_codebuddy_body(body_json)
    return json.dumps(body_json, ensure_ascii=False).encode("utf-8"), body_json, cross_port_target


# ── Gemini 原生协议转换（handler=gemini-native）──
# 客户端走 OpenAI 协议（/v1/chat/completions），代理内部转换为
# Google 原生 generateContent / streamGenerateContent 调用。
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


def _gemini_chunk_to_openai(gemini_chunk: dict, model: str) -> dict:
    """Gemini 流式 chunk → OpenAI chat.completion.chunk。"""
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


def _copilot_responses_usage_to_chat(usage: dict) -> dict:
    """Responses usage → chat usage（流式 completed 事件用）。"""
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
                usage = _copilot_responses_usage_to_chat(r.get("usage"))
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


async def _handle_gemini_native(writer, target, method, path, headers, body, stats, label):
    """Gemini 原生协议代理：OpenAI 请求 → generateContent → OpenAI 响应。

    覆盖 /v1/chat/completions（含流式）与 /v1/models。
    认证：客户端 x-goog-api-key/Authorization 优先，其次 secrets.json / 环境变量。
    """
    import json as _json
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


# ── Trae Work 协议转换（handler=trae-work）──
# 客户端走 OpenAI 协议（/v1/chat/completions），代理内部转换为
# Trae 的 llm_utils_chat（SSE，content 数组格式）。认证用 Cloud-IDE-JWT。
_TRAE_API_HOST = "https://trae-api-cn.mchost.guru"
_TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
_TRAE_IDE_VERSION = "0.1.43"
_TRAE_IDE_VERSION_CODE = "20260730"
_TRAE_DEVICE_ID = "199444637423849"
_TRAE_MACHINE_ID = "d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428"
# trae 上游模型列表缓存（get_detail_param，TTL 5 分钟）
_TRAE_MODELS_CACHE: list = []
_TRAE_MODELS_CACHE_TIME: float = 0.0
_TRAE_MODELS_TTL: float = 300.0

# ── 排队处理（简化策略，2026-08-02）──
# 上游 request_wait_in_queue 事件 → 模型繁忙，直接终止并返回繁忙提示，不做降级重发。


def _trae_build_headers(token: str) -> dict:
    """构造 Trae Work API 请求头（Cloud-IDE-JWT + 设备指纹）。"""
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-app-id": _TRAE_APP_ID,
        "x-app-version": "default",
        "x-app-version-code": _TRAE_IDE_VERSION_CODE,
        "x-ide-version-code": _TRAE_IDE_VERSION_CODE,
        "x-ide-version": _TRAE_IDE_VERSION,
        "x-ide-version-type": "stable",
        "x-device-id": _TRAE_DEVICE_ID,
        "x-machine-id": _TRAE_MACHINE_ID,
        "x-device-type": "windows",
        "x-os-version": "Windows 10",
        "x-device-brand": "Standard PC (Q35 + ICH9, 2009)",
        "x-device-cpu": "KVM",
        "x-trae-authorized-services": "feishu",
        "request-traffic-type": "prod",
        "X-Trae-Client-Type": "lite",
    }


async def _trae_fetch_models(token: str) -> list:
    """从 trae 上游 get_detail_param 拉取最新模型列表（TTL 缓存 5 分钟）。

    解析 config_info_list：过滤 __dev 开发变体、不可用(config_switch=false)、
    用户不可见(is_invisible_to_user)的配置；失败时返回缓存兜底。
    """
    global _TRAE_MODELS_CACHE, _TRAE_MODELS_CACHE_TIME
    now = time.time()
    if _TRAE_MODELS_CACHE and (now - _TRAE_MODELS_CACHE_TIME) < _TRAE_MODELS_TTL:
        return _TRAE_MODELS_CACHE
    if not token:
        return []
    try:
        client = await get_http_client()
        resp = await client.post(
            f"{_TRAE_API_HOST}/api/ide/v1/get_detail_param",
            json={
                "function": "chat_v3",
                "config_names": None,
                "need_prompt": False,
                "current_config_info": None,
                "poly_prompt": True,
                "mode_type": None,
                "agent_type": None,
            },
            headers=_trae_build_headers(token),
            timeout=httpx.Timeout(15.0),
        )
        if resp.status_code != 200:
            logger.warning(f"[trae-work] get_detail_param HTTP {resp.status_code}")
            return _TRAE_MODELS_CACHE or []
        data = resp.json()
        models = []
        seen = set()
        for cfg in data.get("config_info_list", []):
            cname = cfg.get("config_name", "")
            if not cname or cname.endswith("__dev"):
                continue
            if not cfg.get("config_switch", True):
                continue
            if cfg.get("is_invisible_to_user", False):
                continue
            if cname not in seen:
                seen.add(cname)
                models.append(cname)
        if models:
            _TRAE_MODELS_CACHE = models
            _TRAE_MODELS_CACHE_TIME = now
            logger.info(f"[trae-work] 上游模型列表已同步: {len(models)} 个")
        return models
    except Exception as e:
        logger.warning(f"[trae-work] 模型列表拉取失败: {e}")
        return _TRAE_MODELS_CACHE or []


def _openai_to_trae_body(body: dict) -> dict:
    """OpenAI chat.completions 请求体 → Trae llm_utils_chat 请求体。

    工具调用历史文本化（关键）：
    Trae 上游 messages 只有 role+content，无 OpenAI 式 tool_calls / role=tool 概念。
    实测 Doubao-Seed-Code 对"孤立 tool 消息"（assistant.tool_calls 被丢弃后）
    返回 HTTP 200 + 空 SSE 流（0 output 事件），glm-5.2 等可容忍。参考
    trae-local-api 逆向编码（agent.js runAgentLoop）：
      assistant: "[Tool Call: {name}]\nArguments: {args}\n\nResult: ..."
      tool 消息 → user: "[Tool Call Result: {name}]\n{output}"
    """
    trae_messages = []
    tool_refs = {}  # tool_call_id -> 工具名（供后续 role=tool 消息匹配）
    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")

        # assistant 消息自带的 tool_calls → 文本化拼入 content（Trae 无 tool_calls 字段）
        calls_text = ""
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or ""
            calls_text += f"[Tool Call: {name}]\nArguments: {args}\n\n"
            tid = tc.get("id")
            if tid:
                tool_refs[tid] = name
        if calls_text:
            if content and isinstance(content, str):
                content = content.rstrip() + "\n\n" + calls_text.rstrip()
            else:
                content = calls_text.rstrip()

        # role=tool 消息：Trae 无此 role，转 user + 文本化，避免上游收到孤立 tool 消息
        if role == "tool":
            name = tool_refs.get(m.get("tool_call_id"), "")
            suffix = f": {name}" if name else ""
            tool_content = str(content or "").strip()
            content = f"[Tool Call Result{suffix}]\n{tool_content}" if tool_content \
                else f"[Tool Call Result{suffix}]"
            role = "user"

        if isinstance(content, list):
            # 已数组化（OpenAI 多模态），转成 Trae 的 {type,text} 列表
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") in ("text", "input_text"):
                        parts.append({"type": "text", "text": c.get("text", "")})
                    elif c.get("type") == "image_url":
                        # Trae 的 content.image_url 字段类型为对象（LLMRawMessageImageUrl），
                        # 原样透传 OpenAI 格式 {"image_url": {"url": ...}}——不要转成 image 字段（Trae 4001）。
                        # 注意：Trae 图片能力只对内置多模态模型开放（Doubao_1_6/qwen-3.7-plus 等），
                        # 非白名单模型（Doubao-Seed-2.1-Pro/glm-5.2）返回 3003/1005，属上游限制。
                        parts.append({"type": "image_url", "image_url": c.get("image_url", {})})
                    else:
                        parts.append({"type": "text", "text": str(c)})
                else:
                    parts.append({"type": "text", "text": str(c)})
            trae_messages.append({"role": role, "content": parts, "role_type": 0})
        else:
            trae_messages.append({"role": role,
                                  "content": [{"type": "text", "text": str(content or "")}],
                                  "role_type": 0})
    out = {
        "messages": trae_messages,
        "function": "chat_v3",
        "stream": bool(body.get("stream", False)),
    }
    model = body.get("model", "glm-5.2")
    if model and model not in ("auto", "trae-work"):
        # 去掉可能的 "trae/" 前缀
        clean = model.split("/")[-1]
        out["model"] = clean
        out["config_name"] = clean
    # ── tools 翻译：透传 + 提示词注入 ──
    # OpenAI: tools[].function.parameters = object
    # Trae:   tools[].function.parameters = JSON 字符串（实测 object 直接 4001）
    # 注：Trae 上游对标准 tools 字段支持不可靠（seed-code 实测不识别 → 输出乱格式），
    #     按 trae-local-api 方式额外注入提示词（XML <tool_call> 格式），响应侧解析。
    tools = body.get("tools")
    if tools:
        trae_tools = []
        for t in tools:
            fn = t.get("function") or {}
            params = fn.get("parameters")
            fn2 = dict(fn)
            if isinstance(params, (dict, list)):
                fn2["parameters"] = json.dumps(params, ensure_ascii=False)
            trae_tools.append({**t, "function": fn2})
        out["tools"] = trae_tools
        # 提示词注入到最后一条 user 消息（trae-local-api buildToolPrompt 方式）
        tool_prompt = _build_trae_tool_prompt(tools)
        if tool_prompt and out["messages"]:
            last = out["messages"][-1]
            if last.get("role") == "user":
                last["content"] = last["content"] + [{"type": "text", "text": "\n\n" + tool_prompt}]
            else:
                out["messages"].append({"role": "user",
                                        "content": [{"type": "text", "text": tool_prompt}],
                                        "role_type": 0})
    # ── 采样参数尽力透传（参考 trae-local-api trae-client.js llmUtilsChat）──
    # 上游 best-effort 支持，不保证全部生效；max_tokens 截断到 128000
    if isinstance(body.get("max_tokens"), (int, float)) and body["max_tokens"]:
        out["max_tokens"] = min(int(body["max_tokens"]), 128000)
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        val = body.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = val
    if body.get("stop"):
        out["stop"] = body["stop"] if isinstance(body["stop"], list) else [str(body["stop"])]
    if isinstance(body.get("seed"), (int, float)) and body["seed"]:
        out["seed"] = body["seed"]
    if isinstance(body.get("n"), int) and body["n"] > 1:
        out["n"] = body["n"]
    return out


def _trae_chunk_to_openai(chunk: dict, model: str) -> dict:
    """Trae output 事件 → OpenAI chat.completion.chunk。

    兼容上游两种 output 形态（trae-local-api 逆向结论）：
      旧格式: {"response": "...", "reasoning_content": "...", "tool_calls": [...]}
      新格式(2026-05): {"type": "text", "content": "...", "reasoning": "..."}
    """
    content = chunk.get("response", "") or chunk.get("content", "") or ""
    reasoning = chunk.get("reasoning_content") or chunk.get("reasoning") or ""
    delta = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    trae_tc = chunk.get("tool_calls")
    if trae_tc:
        oai_tc = _trae_tool_calls_to_openai(trae_tc)
        if oai_tc:
            delta["tool_calls"] = oai_tc
    return {
        "id": f"chatcmpl-{abs(hash(str(chunk.get('session_id', ''))))}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _trae_tool_calls_to_openai(trae_tc: list) -> list:
    """tool_calls → OpenAI tool_calls（兼容两种输入形态）。

    Trae 原生: {"index":0,"id":"call_x","type":"function",
                "function_call":{"name":"get_weather","arguments":"..."}}
    DSML/XML 解析: {"type":"function","function":{"name":"get_weather","arguments":"..."}}（无 id/index）

    输出统一 OpenAI 格式：{"id","type","function","index"}；缺 id/index 时补生成
    （OpenAI 协议要求，客户端按 id 关联工具结果；缺 index 时流式无法分片累积）。
    """
    oai = []
    for i, tc in enumerate(trae_tc):
        if not isinstance(tc, dict):
            continue
        # 兼容 function_call（Trae 原生）与 function（DSML/XML 解析）两种键
        fc = tc.get("function_call") or tc.get("function") or {}
        fn = {}
        if fc.get("name"):
            fn["name"] = fc["name"]
        if fc.get("arguments") is not None:
            fn["arguments"] = fc["arguments"]
        if not fn:
            continue
        item = {"type": tc.get("type", "function"), "function": fn}
        item["index"] = tc.get("index") if tc.get("index") is not None else i
        if tc.get("id"):
            item["id"] = tc["id"]
        else:
            item["id"] = f"call_{int(time.time() * 1000)}_{i}"
        oai.append(item)
    return oai


# ── DSML 标记解析（seed-code 系模型：工具调用以文本标记输出在 response 字段）──
# 实测形态（Doubao-Seed-Code，2026-08-02）：
#   <｜DSML｜>
#   <｜function｜>
#   <｜function name｜>get_weather</｜function｜>
#   <｜parameter｜>{"city":"北京"}</｜parameter｜>
#   </｜function｜>
#   </｜DSML｜>
# 注意：<｜function name｜>...</｜function｜> 和外层 <｜function｜>...</｜function｜> 共用
# 同一个闭合标记 "</｜function｜>"（而非 "</｜function name｜>"），曾用非贪婪
# "<｜function｜>(.*?)</｜function｜>" 提取整段块，结果非贪婪匹配在第一个
# </｜function｜>（其实是 name 标签的闭合）处就停止，导致 <｜parameter｜> 从未
# 被捕获到块内（_DSML_FN_RE 从未真正工作过，2026-08-02 补测试时发现）。
# 改用一次性配对正则，同时捕获 name + parameter，避免闭合标记歧义。
_DSML_PAIR_RE = re.compile(
    r"<[｜|]function[｜|]>\s*<[｜|]function name[｜|]>(.*?)</[｜|]function[｜|]>"
    r"\s*<[｜|]parameter[｜|]>(.*?)</[｜|]parameter[｜|]>\s*</[｜|]function[｜|]>", re.S)
_DSML_LIKE_RE = re.compile(r"<[｜|](?:DSML|function|function name|parameter)[｜|]>")
# seed-code 从历史文本化格式学到的输出形态（2026-08-02 实测）：response 字段输出
# "[Tool Call: bash]\nArguments: {\"command\":\"...\"}" 纯文本而非 DSML/tool_calls 事件。
# 识别并解析回 tool_calls，避免把该文本原样透传给客户端（IDE 无法识别）。
# Arguments 用平衡括号提取（贪婪 \{.*\} 会吞掉后面的 reasoning JSON 文本）。
_TOOLCALL_TEXT_RE = re.compile(r"\[Tool Call: ([A-Za-z0-9_\-\.:/]+)\]\s*\n?\s*Arguments:\s*(\{)", re.S)


def _extract_balanced_json(text: str, start: int) -> str | None:
    """从 text[start]（'{'）起做平衡括号提取，返回完整 JSON 对象字符串；无匹配返回 None。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# 模型以 {"reasoning_content":"..."} JSON 字面量输出思考（2026-08-02 实测），
# 需提取为 reasoning 而非作为 content 透传（客户端会把 JSON 字面量当正文显示）。
_DSML_REASONING_RE = re.compile(r'\{"reasoning_content":"((?:[^"\\]|\\.)*)"\}', re.S)
# trae-local-api 方式：提示词注入后模型用 <tool_call> XML 输出工具调用（2026-08-02）
_TOOLCALL_XML_RE = re.compile(r"<tool_call\b[^>]*>([\s\S]*?)</tool_call\s*>", re.I)
# 注：内部 JSON 对象（{"name":...,"arguments":{...}}）不用正则提取，改用
# _extract_balanced_json 平衡括号扫描（见 _parse_dsml_tool_calls），避免嵌套
# 花括号/转义引号导致提取截断（2026-08-02 实测：edit 工具的 oldString 含 JS
# 代码花括号，曾用正则提取导致 JSON 截断校验失败、整段泄漏到正文）
# DSML 外层完整包裹（含 <｜DSML｜>...</｜DSML｜>），一次性移除时用
_DSML_BLOCK_RE = re.compile(r"<[｜|]DSML[｜|]>[\s\S]*?</[｜|]DSML[｜|]>", re.S)

# ── DSML 第 4 种变体：<｜DSML｜invoke name="..."> / <｜DSML｜parameter name="...">
# （2026-08-03 实测，Doubao-Seed-Code）：
#   <｜DSML｜tool_calls>
#   <｜DSML｜invoke name="bash">
#   <｜DSML｜parameter name="command" string="true">...</｜DSML｜parameter>
#   </｜DSML｜invoke>
#   </｜DSML｜tool_calls>
# 与前 3 种（<｜function｜>/<｜function name｜>/<｜parameter｜> 独立标签、
# [Tool Call: name] 纯文本、<tool_call>JSON</tool_call>）完全不同的第 4 种
# 标签语法：tool 名和 param 名作为**标签属性**（name="xxx"）而非独立标签体。
# 教训：与其继续为每个新样本量身定制一条正则（治标），这里改写一个通用
# "<｜DSML｜TAGNAME attr="val" ...>...</｜DSML｜TAGNAME>" 家族扫描器，只认
# 标签语法结构本身（TAGNAME 任意、属性任意），不绑定具体 tool/param 名，
# 这样才能覆盖同一标签家族里模型可能继续变换出的其他排列，而不是每次追新样本。
_DSML_INVOKE_RE = re.compile(
    r'<[｜|]DSML[｜|]invoke\s+name="([^"]*)"[^>]*>([\s\S]*?)</[｜|]DSML[｜|]invoke\s*>', re.S)
_DSML_PARAM_RE = re.compile(
    r'<[｜|]DSML[｜|]parameter\s+name="([^"]*)"[^>]*>([\s\S]*?)</[｜|]DSML[｜|]parameter\s*>', re.S)
# 整个 <｜DSML｜tool_calls>...</｜DSML｜tool_calls> 外层包裹，清洗正文时一次性移除
_DSML_TOOLCALLS_BLOCK_RE = re.compile(
    r"<[｜|]DSML[｜|]tool_calls[｜|]?>[\s\S]*?</[｜|]DSML[｜|]tool_calls\s*>", re.S)
# 检测用：任意 "<｜DSML｜任意标签" 前缀（含 invoke/parameter/tool_calls 等家族全部成员）
_DSML_ANY_TAG_RE = re.compile(r"<[｜|]DSML[｜|][A-Za-z_]+")
# seed-code 实测变体：<seed_call> 外层可能用 </seed_call>、</tool_call> 或
# </seed:tool_call> 闭合。外层不能锚定开头，避免吞掉其前正常正文。
_SEED_CALL_RE = re.compile(
    r"<seed_call\b[^>]*>([\s\S]*?)(?:</seed_call\s*>|</tool_call\s*>|</seed:tool_call\s*>)", re.I)
_SEED_CALL_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]*)"[^>]*>([\s\S]*?)</invoke\s*>', re.I)
_SEED_CALL_FUNCTION_RE = re.compile(r'<function\s+name="([^"]*)"[^>]*>([\s\S]*?)</function\s*>', re.I)
_SEED_CALL_PARAM_OPEN_RE = re.compile(r'<parameter\s+name="([^"]*)"[^>]*>', re.I)
# 变体 6（2026-08-04 实测，Doubao-Seed-Code）：<tool_call> 内部不是 JSON 而是
# XML 子标签：<tool_name>bash</tool_name><parameters><parameter name="command"
# string="true">...</parameter>...</parameters>。曾因 _TOOLCALL_XML_RE 分支要求
# 块内 find("{") 而解析失败，整段 <tool_call> 原样泄漏到正文（IDE 显示裸露 XML，
# 工具未执行）。
_TOOLCALL_NAME_RE = re.compile(r"<tool_name\b[^>]*>([\s\S]*?)</tool_name\s*>", re.I)
_TOOLCALL_PARAM_RE = re.compile(
    r'<parameter\s+name="([^"]*)"[^>]*>([\s\S]*?)</parameter\s*>', re.I)
# 变体 7（2026-08-05 实测）：<tool_call> 内 <tool_name> 标签 + <arguments>{"..": ".."}</arguments>
# JSON 包裹形式（opencode 客户端历史工具调用格式的同源形态，模型从上下文学到）。与
# 变体 6 的 <parameter name=".."> 子标签并列，解析时优先取 <arguments> 整块 JSON。
_TOOLCALL_ARGS_RE = re.compile(r"<arguments\b[^>]*>([\s\S]*?)</arguments\s*>", re.I)
# ── 官方 seed-oss / Qwen3 XML 语法（vLLM 官方 parser，2026-08-04 补全）──
# Qwen3/seed-oss 模型原生工具调用格式（vllm/parser/qwen3.py + seed_oss.py）：
#   <think>...</think>（seed-oss 用 <seed:think>）推理
#   <seed:tool_call><function=bash><parameter=command>ls -la</parameter></function></seed:tool_call>
# 关键差异：function/parameter 用"无空格无引号"的 <tag=name> 属性形式，且
# seed-oss 外层是 <seed:tool_call>（带冒号），与已支持的 <seed_call>（无冒号）
# 和 <function name="..">（带空格引号）是两套不同语法——官方 parser 三者都认。
# 此外官方 parser 还容忍：无 <tool_call> 前缀直接 <function=>（fallback）、
# </function> 后直接下一个 <tool_call>（连续调用未闭合外层）。
_SEED_TOOL_CALL_RE = re.compile(
    r"<seed:tool_call\b[^>]*>([\s\S]*?)(?:</seed:tool_call\s*>|</seed_call\s*>|</tool_call\s*>)", re.I)
_QWEN_FUNC_RE = re.compile(r"<function\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</function\s*>|(?=<tool_call\b|<seed:tool_call\b|<seed_call\b))", re.I)
_QWEN_PARAM_RE = re.compile(
    r"<parameter\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</parameter\s*>|(?=<parameter\s*=|<function\s*=</tool_call\b|<seed:tool_call\b))", re.I)
# 变体 7（2026-08-04 实测）：模型把历史工具结果用 <seed:tool_result> 包裹复述
# （无闭合标签，直接透传给客户端显示重复的历史 grep 结果）。识别用于剥离。
_SEED_TOOL_RESULT_OPEN_RE = re.compile(r"<seed:tool_result\b[^>]*>", re.I)
# 通用工具调用意图正则（2026-08-04 根治层）：匹配任意 XML 标签中出现的工具
# 语义关键词。模型自由生成时无论发明什么标签排列（<any_tool_xxx>、<tool:xxx>、
# <func>、<param> 等），只要标签名含这些关键词就命中——这是"不再打地鼠"的
# 关键：识别层从"已知标签白名单"升级为"语义关键词通用匹配"，新变体自动落入
# 剥离路径，不会静默透传。仅匹配 XML 标签形态（<...>），正文里出现 function/
# tool 等单词不误伤。
_TOOL_INTENT_TAG_RE = re.compile(
    r"<(?:[a-zA-Z_:][\w:.-]*)?\s*[a-zA-Z_:]*"
    r"(?:tool(?:[_:\-](?:call|name|result|usage))?|func(?:tion)?|parameter|param(?:s)?|"
    r"invoke|argument|args|arg|tool_call|seed_call|execute|cmd|command|call)\b[^>]*>",
    re.I)


def _build_trae_tool_prompt(tools: list) -> str:
    """OpenAI tools 定义 → Trae 提示词文本（trae-local-api buildToolPrompt 方式）。

    Trae 上游 llm_utils_chat 不识别标准 tools 字段（seed-code 实测输出乱格式），
    把工具定义注入提示词并指示 XML 调用格式，响应侧解析 <tool_call>。
    """
    descs = []
    for t in tools or []:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        desc = fn.get("description") or ""
        params = fn.get("parameters") or {}
        props = (params or {}).get("properties") or {}
        param_str = ", ".join(
            f"{k}: {v.get('description') or v.get('type')}" for k, v in props.items())
        descs.append(f"- {name}({param_str}): {desc}")
    return ("[Available tools - 使用 XML 格式调用工具]:\n" + "\n".join(descs) +
            "\n\n调用工具时，在回复中包含以下格式:\n"
            '<tool_call>\n{"name": "工具名", "arguments": {"参数": "值"}}\n</tool_call>\n'
            "可以一次回复多个工具调用。收到工具结果后分析并回复用户。仅在需要时调用工具。")


def _extract_reasoning_text(text: str) -> str:
    """从累积文本提取所有 {"reasoning_content":"..."} JSON 字面量的内容并拼接（JSON unescape）。

    seed-code 实测会把多段思考分别包成多个独立的 {"reasoning_content":"..."} JSON
    （而非一个整体 JSON 装完整思考），曾用 .search() 只提取第一段，导致后续几段
    原样以 JSON 字面量泄漏到正文（2026-08-02 实测：客户端看到裸露的
    {"reasoning_content":"..."} 文本）。改用 .finditer() 提取全部并拼接。
    无匹配返回 ""。
    """
    parts = []
    for m in _DSML_REASONING_RE.finditer(text or ""):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            parts.append(m.group(1))
    return "".join(parts)


def _looks_like_dsml(text: str) -> bool:
    """文本是否含工具调用标记特征（seed-code 系模型输出）。

    兼容形态：DSML 标记（<｜DSML｜>...）、[Tool Call: name] 文本格式、
    {"reasoning_content":"..."} JSON 字面量（思考封装）、<tool_call> XML、
    <seed_call>/<seed:tool_result> 等 seed 系标签，以及各形态的分片半截
    （尽早进入缓冲累积，避免原始标记/JSON 字面量透传给客户端）。

    2026-08-04 根治：在"具体特征白名单"之上叠加**通用意图正则**
    （_TOOL_INTENT_TAG_RE）——模型发明新标签时，只要标签名含工具语义关键词
    （tool_call/function/parameter/invoke/tool_name/tool_result/arguments 等），
    一律判定为疑似工具调用进入剥离路径，不再逐个打地鼠。关键词限定在 XML
    标签形态内（<...>），避免误伤普通正文里出现"function"等单词。
    """
    t = text or ""
    return bool(_DSML_LIKE_RE.search(t) or _TOOLCALL_TEXT_RE.search(t)
                or _DSML_REASONING_RE.search(t) or _TOOLCALL_XML_RE.search(t)
                or _DSML_ANY_TAG_RE.search(t)
                or _SEED_CALL_RE.search(t) or _SEED_TOOL_RESULT_OPEN_RE.search(t)
                or _SEED_TOOL_CALL_RE.search(t) or _QWEN_FUNC_RE.search(t)
                or _TOOLCALL_NAME_RE.search(t) or _TOOLCALL_PARAM_RE.search(t)
                or "<seed:think" in t or "<think" in t
                or "<function=" in t
                or "<seed:" in t or "<tool_" in t
                or "[Tool Call" in t or "Arguments:" in t
                or '{"reasoning_content"' in t or "reasoning_content" in t
                or "tool_call" in t or t.startswith('{"')
                or bool(_TOOL_INTENT_TAG_RE.search(t)))


# ── 架构说明（2026-08-02 重构）──────────────────────────────────────────
# 曾尝试在流式接收过程中"边收边猜这个 chunk 是不是工具调用标记的开头/半截"
# （_is_potential_toolcall_prefix 等启发式），结果每堵住一种半截标记（如
# "[" 单独成 chunk、reasoning JSON 未闭合）就会冒出下一种变种——因为任意
# 长度的文本前缀理论上都可能是"某个标记的未完成前缀"，这是不可判定问题。
#
# 参考 trae-local-api（官方逆向实现，/root/trae-local-api/src/agent.js
# runAgentStream + extractToolCalls）的架构：从不在流式接收阶段做标记判断，
# 而是先把整轮的 response/content 原始累积成 fullContent，等上游 SSE 流
# 完全结束（收到 'end'）后，才对完整文本一次性跑正则解析 tool_calls，
# 解析完再统一 flush 给客户端。标记必然是完整的，不存在"半截"问题。
#
# 本实现采用同样策略：resp_text 流式阶段只做纯累积（不做任何 content 提前
# 转发），reasoning_content/tool_calls 等结构化字段（上游明确给出、非文本
# 猜测）仍然逐 chunk 立即转发，因为它们不存在"文本标记未闭合"的歧义。
def _strip_seed_tool_result_blocks(text: str) -> str:
    """剥离 <seed:tool_result> 复述块（2026-08-04 实测，Doubao-Seed-Code）。

    模型会把历史工具结果用 <seed:tool_result> 开标签包裹复述（无闭合标签），
    原样透传给客户端 = 重复显示历史 grep 结果。处理策略（按开标签后是否有
    新的强工具标记区分）：
      - 无后续强标记（纯复述）：整块丢弃，避免历史结果整段泄漏
      - 有后续强标记（混合结构：复述 + 正文 + 新调用）：开标签后内容与正文
        无闭合标签无法可靠分界，保守只剥开标签本身、保留后续全部——正文不丢，
        且新工具调用块由 _TOOLCALL_XML_RE 等后续清洗删除。
    """
    if "<seed:tool_result" not in text:
        return text
    parts = []
    i = 0
    while True:
        m = _SEED_TOOL_RESULT_OPEN_RE.search(text, i)
        if not m:
            parts.append(text[i:])
            break
        parts.append(text[i:m.start()])  # 开标签之前的正文保留
        nxt = re.search(r"<tool_call\b|<seed_call\b|<invoke\b|<function\b", text[m.end():], re.I)
        if nxt:
            # 混合结构：剥掉开标签，从开标签后继续（正文与新调用保留）
            i = m.end()
        else:
            break  # 纯复述：后续内容全部丢弃
    return "".join(parts)


def _resolve_trae_text(full_text: str) -> tuple[list, str, str]:
    """对一整轮已完整接收的 response/content 文本做工具调用/reasoning 解析。

    Returns: (tool_calls, reasoning_text, content_text)
      - tool_calls: 解析出的工具调用列表（OpenAI tool_calls 格式，可能为空）
      - reasoning_text: 提取出的 reasoning（{"reasoning_content":"..."} JSON 字面量）
      - content_text: 清洗掉工具调用/reasoning 标记后的正文（tool_calls 非空时省略，
        避免把 "[Tool Call: xxx]\nArguments: {...}" 之类的调用文本也当正文回显）
    """
    text = full_text or ""
    reasoning_text = _extract_reasoning_text(text)
    if reasoning_text:
        text = _DSML_REASONING_RE.sub("", text)  # 摘除全部（可能有多段），不只第一段
    tool_calls = _parse_dsml_tool_calls(text)
    if not tool_calls and _looks_like_dsml(text):
        # 兜底告警（2026-08-03 新增）：文本命中"疑似工具调用标记"特征，但所有已知
        # 解析器均未能解析出 tool_calls——大概率是模型又输出了一种尚未支持的新变体。
        # 记录 WARNING + 原始文本，便于从日志第一时间发现新变体。
        # 2026-08-04 修复：不再"按普通文本原样透传"——命中疑似标记的文本直接透传
        # 会把未解析的 <tool_call>/<seed:tool_result> 等原始标记泄漏给客户端（IDE
        # 显示裸露 XML、工具不执行）。改为剥离已知强标记块（tool_call/seed_call/
        # seed:tool_result 复述块等），只保留剩余正文；剥离后若为空则正文为空，
        # 宁可丢内容也不泄漏未解析的调用标记（调用意图已丢失，正文保留也无意义）。
        logger.warning(f"[trae-work] _looks_like_dsml=True 但未解析出 tool_calls，"
                        f"疑似新的工具调用标记变体，已剥离强标记。原始文本: {text[:16384]!r}")
        content_text = _strip_strong_tool_markers(text).strip()
    elif tool_calls:
        # 工具调用文本本身不作为正文回显（DSML/[Tool Call:]/<tool_call> 全部清洗掉）
        # 顺序关键：先剥 <seed:tool_result> 复述块（依赖 <tool_call>/<invoke> 等
        # 强标记界定块边界），再删 <tool_call> 等调用块——反序会因调用块已删
        # 找不到边界而吞掉复述块之后的正文（2026-08-04 实测）。
        content_text = _SEED_CALL_RE.sub("", text)
        content_text = _SEED_TOOL_CALL_RE.sub("", content_text)
        # 官方 seed-oss 推理标签 <seed:think>...</seed:think>（Qwen3 用 <think>）：
        # 推理内容已由上游 reasoning_content 结构化字段单独转发，文本形态的
        # think 标签不透明传给客户端（与 reasoning JSON 字面量同一处理原则）
        content_text = re.sub(r"<seed:think\b[^>]*>[\s\S]*?</seed:think\s*>", "", content_text, flags=re.I)
        content_text = re.sub(r"<think\b[^>]*>[\s\S]*?</think\s*>", "", content_text, flags=re.I)
        content_text = _strip_seed_tool_result_blocks(content_text)
        content_text = _DSML_BLOCK_RE.sub("", content_text)
        content_text = _DSML_TOOLCALLS_BLOCK_RE.sub("", content_text)
        content_text = _TOOLCALL_TEXT_RE.sub("", content_text)
        for m in re.finditer(_TOOLCALL_TEXT_RE, text):
            args = _extract_balanced_json(text, m.start(2))
            if args is not None:
                content_text = content_text.replace(args, "", 1)
        content_text = _TOOLCALL_XML_RE.sub("", content_text)
        # 官方 seed-oss 语法（<seed:tool_call><function=..>）已由 _SEED_TOOL_CALL_RE
        # 剥离；裸 <function=..>（无外层）由 _QWEN_FUNC_RE 兜底剥离
        content_text = _QWEN_FUNC_RE.sub("", content_text)
        content_text = content_text.strip()
    else:
        content_text = text.strip()
    return tool_calls, reasoning_text, content_text


def _strip_generic_tool_blocks(text: str) -> str:
    """通用剥离：移除任意"工具语义" XML 标签块（2026-08-04 根治层）。

    配合 _looks_like_dsml 的通用意图正则 _TOOL_INTENT_TAG_RE——检测层已从
    "已知标签白名单"升级为"语义关键词通用匹配"，剥离层必须同样通用，否则
    检测到新变体却剥不掉（白名单剥离 = 检测白搭，依旧泄漏）。

    做法：扫描文本中所有含工具语义关键词的标签（<tool_xxx>/<function>/
    <parameter>/<invoke>/<args> 等任意排列），对每个开标签用**平衡标签扫描**
    找到对应闭标签（支持嵌套，如 <tool_call><function>..</function></tool_call>），
    整块删除；未闭合的开标签从开标签剥离到文本末尾（调用输出被截断时，
    其后都是调用内容，原样透传只会泄漏半截 XML）。
    """
    out = text
    while True:
        m = _TOOL_INTENT_TAG_RE.search(out)
        if not m:
            break
        start = m.start()
        open_tag = m.group(0)
        # 自闭合标签 <call name=".." /> → 直接删
        if open_tag.rstrip().endswith("/>"):
            out = out[:start] + out[m.end():]
            continue
        # 提取开标签名（含命名空间前缀，如 <seed:tool_call> → seed:tool_call）
        name_m = re.match(r"<([a-zA-Z_:][\w:.-]*)", open_tag)
        if not name_m:
            out = out[:start] + out[m.end():]
            continue
        open_name = name_m.group(1)
        # 平衡扫描：找到与该开标签配对的闭标签（容忍嵌套同名标签）
        depth = 1
        pos = m.end()
        close_re = re.compile(rf"</{re.escape(open_name)}\s*>", re.I)
        open_re = re.compile(rf"<{re.escape(open_name)}\b[^>]*>", re.I)
        end = None
        while pos < len(out):
            nxt_open = open_re.search(out, pos)
            nxt_close = close_re.search(out, pos)
            if nxt_close and (not nxt_open or nxt_close.start() < nxt_open.start()):
                depth -= 1
                if depth == 0:
                    end = nxt_close.end()
                    break
                pos = nxt_close.end()
            elif nxt_open:
                depth += 1
                pos = nxt_open.end()
            else:
                break
        if end is not None:
            out = out[:start] + out[end:]
        else:
            # 未闭合：开标签之后全部视为调用内容，剥离到末尾
            out = out[:start]
    return out


def _strip_strong_tool_markers(text: str) -> str:
    """剥离所有已知"强工具调用标记"块，保留剩余文本（2026-08-04 兜底用）。

    覆盖：<tool_call>...</tool_call>（含 XML 子标签变体）、<seed_call>...</...>、
    DSML 独立块、[Tool Call: xxx]\nArguments: {...} 文本、<seed:tool_result> 复述块。
    另处理未闭合的 <tool_call> 开标签（模型输出被截断）：从开标签剥离到文本
    末尾——既然模型已进入调用输出模式，其后内容都是调用参数，原样透传只会把
    半截 JSON/XML 泄漏给客户端（2026-08-04 实测：IDE 显示裸露 <tool_call>）。
    供 _resolve_trae_text 解析失败兜底时调用——把未识别变体的标记外壳剥掉，
    防止原始 XML/文本标记原样泄漏给客户端；剩余正文继续展示。
    """
    out = text
    out = _SEED_CALL_RE.sub("", out)
    out = _SEED_TOOL_CALL_RE.sub("", out)
    # 官方 seed-oss/Qwen3 推理标签（<seed:think>/<think>）不透明给客户端
    out = re.sub(r"<seed:think\b[^>]*>[\s\S]*?</seed:think\s*>", "", out, flags=re.I)
    out = re.sub(r"<think\b[^>]*>[\s\S]*?</think\s*>", "", out, flags=re.I)
    # 先剥 <seed:tool_result>（依赖 <tool_call>/<invoke> 等强标记界定边界），再删调用块
    out = _strip_seed_tool_result_blocks(out)
    out = _TOOLCALL_XML_RE.sub("", out)
    out = _DSML_BLOCK_RE.sub("", out)
    out = _DSML_TOOLCALLS_BLOCK_RE.sub("", out)
    out = _TOOLCALL_TEXT_RE.sub("", out)
    for m in re.finditer(_TOOLCALL_TEXT_RE, text):
        args = _extract_balanced_json(text, m.start(2))
        if args is not None:
            out = out.replace(args, "", 1)
    # 未闭合 <tool_call> 开标签（无闭合标签，_TOOLCALL_XML_RE 匹配不到）→ 剥到末尾
    unclosed = re.search(r"<tool_call\b[^>]*>", out, re.I)
    if unclosed:
        out = out[:unclosed.start()]
    # 未闭合 <seed:tool_call> 开标签（官方 seed-oss 外层，截断）→ 剥到末尾
    unclosed_seed = re.search(r"<seed:tool_call\b[^>]*>", out, re.I)
    if unclosed_seed:
        out = out[:unclosed_seed.start()]
    # 未闭合 <function=..> 开标签（官方语法，无闭合 </function>）→ 剥到末尾
    unclosed_func = re.search(r"<function\s*=\s*[^>]*>", out, re.I)
    if unclosed_func:
        out = out[:unclosed_func.start()]
    # 未闭合 <seed:think>/<think> 开标签（截断）→ 剥到末尾
    unclosed_think = re.search(r"<seed:think\b[^>]*>|<think\b[^>]*>", out, re.I)
    if unclosed_think:
        out = out[:unclosed_think.start()]
    # 根治兜底：通用剥离任意"工具语义" XML 标签块（覆盖所有未识别新变体）
    # 已知格式已被上方逐条剥离，此处处理剩余的任何工具语义标签排列。
    out = _strip_generic_tool_blocks(out)
    return out


def _parse_toolcall_subtags(block: str) -> list:
    """变体 6（2026-08-04 实测，Doubao-Seed-Code）：<tool_call> 块内 XML 子标签。

    形态（与 opencode 客户端工具调用的历史格式同源，模型从上下文学到）：
      <tool_call>
      <tool_name>bash</tool_name>
      <parameters>
      <parameter name="command" string="true">cd /x && git status</parameter>
      <parameter name="timeout" string="false">10000</parameter>
      </parameters>
      </tool_call>

    变体 7（2026-08-05 实测）：同外层 <tool_name> 标签，但参数不是 <parameter>
    而是 <arguments>{"command": ".."}</arguments> JSON 包裹形式：
      <tool_call>
      <tool_name>bash</tool_name>
      <arguments>{"command": "cd /x && git status"}</arguments>
      </tool_call>
    曾因只认 <parameter> 子标签，<arguments> 块被跳过 → subtags 返回 [] → 整段
    <tool_call> 落入兜底剥离路径，工具调用意图丢失（2026-08-05 实测）。

    解析 <tool_name> 为工具名；<arguments> 整块 JSON 直接作为 arguments 字符串；
    <parameter name=".."> 收集为 arguments 的 {param_name: value}。JSON 值仍走
    _extract_balanced_json（防嵌套花括号截断），普通字符串值按标签位置切片。
    无 tool_name 或参数异常返回 []（交由上层兜底）。
    """
    tcs = []
    for name_match in _TOOLCALL_NAME_RE.finditer(block):
        tool_name = name_match.group(1).strip()
        if not tool_name:
            continue
        # 变体 7 优先：<arguments> JSON 包裹整块参数
        args_match = _TOOLCALL_ARGS_RE.search(block)
        if args_match:
            args_raw = args_match.group(1).strip()
            json_start = args_raw.find("{")
            if json_start >= 0:
                balanced = _extract_balanced_json(args_raw, json_start)
                if balanced is not None and not args_raw[:json_start].strip():
                    args_raw = balanced
            item = {"type": "function", "function": {"name": tool_name, "arguments": args_raw}}
            tcs.append(item)
            continue
        params = {}
        for pm in _TOOLCALL_PARAM_RE.finditer(block):
            param_name = pm.group(1).strip()
            if not param_name:
                continue
            param_value = pm.group(2)
            json_start = param_value.find("{")
            if json_start >= 0:
                balanced = _extract_balanced_json(param_value, json_start)
                if balanced is not None and not param_value[:json_start].strip():
                    param_value = balanced
            params[param_name] = param_value
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    return tcs


def _parse_dsml_tool_calls(text: str) -> list:
    """把工具调用标记文本解析为 OpenAI tool_calls 列表；无匹配返回 []。

    依次尝试：<｜DSML｜invoke name=".."><｜DSML｜parameter name="..">..</｜DSML｜parameter></｜DSML｜invoke>
    标签属性变体 → DSML 标记（<｜function｜> 块）→ [Tool Call: name]\nArguments: {...} 文本
    → <tool_call>JSON</tool_call> → <seed_call> 异步闭合标签变体。
    """
    tcs = []
    # 变体 4（2026-08-03）：<｜DSML｜invoke name="bash"> + 内部多个
    # <｜DSML｜parameter name="command" ...>value</｜DSML｜parameter>。
    # 一个 invoke 可能含多个 parameter，需要收集成 {param_name: value} 再序列化 arguments。
    for tool_name, invoke_body in _DSML_INVOKE_RE.findall(text):
        tool_name = tool_name.strip()
        if not tool_name:
            continue
        params = {}
        for param_name, param_val in _DSML_PARAM_RE.findall(invoke_body):
            param_name = param_name.strip()
            if param_name:
                params[param_name] = param_val
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    if tcs:
        return tcs
    for name_raw, args_raw in _DSML_PAIR_RE.findall(text):
        name = name_raw.strip()
        if not name:
            continue
        args = args_raw.strip()
        item = {"type": "function", "function": {"name": name}}
        if args:
            item["function"]["arguments"] = args
        tcs.append(item)
    if not tcs:
        # [Tool Call: name]\nArguments: {...} 文本格式（seed-code 从历史学到的输出形态）
        for m in _TOOLCALL_TEXT_RE.finditer(text):
            name = m.group(1).strip()
            args = _extract_balanced_json(text, m.start(2))
            if args is None:
                break  # arguments 分片未完整（{ 后还没闭合），等待更多内容累积
            item = {"type": "function", "function": {"name": name}}
            if args:
                item["function"]["arguments"] = args
            tcs.append(item)
    if not tcs:
        # <tool_call> XML 格式（trae-local-api 提示词注入方式，模型遵循提示词输出）：
        # <tool_call> 内部本应是一个完整 JSON 对象 {"name":...,"arguments":{...}}。
        # 曾用正则 _TOOLCALL_XML_JSON_RE（\{[\s\S]*?\} 非贪婪匹配 arguments 对象）
        # 提取 name/arguments，但正则无法正确处理"对象内嵌套花括号"（如 JS 代码片段
        # 里的 `{{}}`）或"转义引号"——遇到第一个 `}` 就提前截断，导致 arguments 提取
        # 出半截 JSON、json.loads 校验失败、tcs 为空，整段 <tool_call> 文本原样
        # 泄漏到正文给客户端（2026-08-02 实测：edit 工具调用的 oldString/newString
        # 含 JS 花括号代码，被截断泄漏）。
        # 教训：任何"提取 JSON 对象子串"的场景，一律用平衡括号扫描
        # （_extract_balanced_json），不能用正则模拟花括号配对——这是本文件里
        # 反复踩坑的同一类错误（DSML 配对正则、reasoning 多段提取也是类似教训）。
        for block in _TOOLCALL_XML_RE.findall(text):
            block = block.strip()
            # 变体 6/7（2026-08-04/05 实测）：块内含 <tool_name> XML 子标签时，
            # 优先走子标签解析（<parameter name=".."> 与 <arguments>{"..":".."} 两形态）。
            # 注意：变体 7 的 <arguments> 里含 {（JSON），若按下方平衡括号扫描会把
            # {"command":...} 当"块内完整 JSON"提取，但该 JSON 无 name 字段 →
            # json.loads 后 name 为空被 continue 跳过 → 整段逃逸（2026-08-05 实测）。
            # 故必须先用 _TOOLCALL_NAME_RE 判断 XML 子标签形态，再决定走哪条解析。
            if _TOOLCALL_NAME_RE.search(block):
                subtags = _parse_toolcall_subtags(block)
                if subtags:
                    tcs.extend(subtags)
                continue
            obj_start = block.find("{")
            if obj_start < 0:
                continue  # 无 tool_name 也无 JSON，非工具调用块，跳过
            obj_str = _extract_balanced_json(block, obj_start)
            if obj_str is None:
                continue  # 未闭合（半截标记），交由上层判定是否继续等待
            try:
                # 模型常在 command JSON 字符串中直接输出多行脚本；这违反严格 JSON
                # 的控制字符约束，但仍是可恢复的工具调用文本。strict=False 保留原值。
                obj = json.loads(obj_str, strict=False)
            except Exception:
                continue
            name = (obj.get("name") or obj.get("tool_name") or "").strip()
            if not name:
                continue
            args_val = obj.get("arguments", {})
            args_str = json.dumps(args_val, ensure_ascii=False) if not isinstance(args_val, str) else args_val
            tcs.append({"type": "function", "function": {"name": name, "arguments": args_str}})
    if not tcs:
        # 变体 5（2026-08-04）：<seed_call> 内的 invoke/function + parameter。
        # 参数标签只用正则定位开标签，值通过闭合标签位置切片，避免非贪婪正则在
        # JSON/JS 花括号内容上截断；JSON 值仍交给平衡括号扫描提取。
        for seed_call in _SEED_CALL_RE.findall(text):
            for variant_re in (_SEED_CALL_INVOKE_RE, _SEED_CALL_FUNCTION_RE):
                for tool_name, invoke_body in variant_re.findall(seed_call):
                    tool_name = tool_name.strip()
                    if not tool_name:
                        continue
                    params = {}
                    for param_match in _SEED_CALL_PARAM_OPEN_RE.finditer(invoke_body):
                        param_name = param_match.group(1).strip()
                        value_end = invoke_body.find("</parameter>", param_match.end())
                        if not param_name or value_end < 0:
                            continue
                        param_value = invoke_body[param_match.end():value_end]
                        json_start = param_value.find("{")
                        if json_start >= 0:
                            balanced_value = _extract_balanced_json(param_value, json_start)
                            if balanced_value is not None and not param_value[:json_start].strip():
                                param_value = balanced_value
                        params[param_name] = param_value
                    item = {"type": "function", "function": {"name": tool_name}}
                    if params:
                        item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
                    tcs.append(item)
    if not tcs:
        # 变体 8（2026-08-04，官方 seed-oss/Qwen3 XML 语法，vllm qwen3.py）：
        #   <seed:tool_call><function=bash><parameter=command>ls</parameter></function></seed:tool_call>
        # 官方语法用"无空格无引号"的 <tag=name> 属性形式（与 <function name="..">
        # 带空格引号形式是两套不同语法），seed-oss 外层为 <seed:tool_call>（带冒号）。
        # 官方 parser 还容忍：无外层直接 <function=>（fallback）、</function> 后
        # 连续下一个 <tool_call>。先匹配 <seed:tool_call> 外层，再匹配裸 <function=>。
        for block in _SEED_TOOL_CALL_RE.findall(text):
            tcs.extend(_parse_qwen_func_params(block))
        if not tcs:
            # 无 <seed:tool_call> 外层：官方 parser 的 fallback——裸 <function=> 直接解析
            for tool_name, func_body in _QWEN_FUNC_RE.findall(text):
                tool_name = tool_name.strip()
                if not tool_name:
                    continue
                params = {}
                for param_name, param_val in _QWEN_PARAM_RE.findall(func_body):
                    param_name = param_name.strip()
                    param_val = param_val.strip()
                    if param_name:
                        json_start = param_val.find("{")
                        if json_start >= 0:
                            balanced_value = _extract_balanced_json(param_val, json_start)
                            if balanced_value is not None and not param_val[:json_start].strip():
                                param_val = balanced_value
                        params[param_name] = param_val
                item = {"type": "function", "function": {"name": tool_name}}
                if params:
                    item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
                tcs.append(item)
    return tcs


def _parse_qwen_func_params(func_body: str) -> list:
    """解析官方 Qwen3/seed-oss <function=..> 块内的 <parameter=..> 参数为 tool_calls。

    与 _QWEN_PARAM_RE 配合：一个 function 块内可能有多个 <parameter=key>value</parameter>，
    全部收集成 {param_name: value} 再序列化 arguments。JSON 值走平衡括号提取，
    防止嵌套花括号截断（与 _parse_toolcall_subtags 同一教训）。
    """
    tcs = []
    func_re = re.compile(r"<function\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</function\s*>|$)", re.I)
    for fm in func_re.finditer(func_body):
        tool_name = fm.group(1).strip()
        if not tool_name:
            continue
        inner = fm.group(2)
        params = {}
        for pm in _QWEN_PARAM_RE.finditer(inner):
            param_name = pm.group(1).strip()
            param_val = pm.group(2).strip()
            if not param_name:
                continue
            json_start = param_val.find("{")
            if json_start >= 0:
                balanced_value = _extract_balanced_json(param_val, json_start)
                if balanced_value is not None and not param_val[:json_start].strip():
                    param_val = balanced_value
            params[param_name] = param_val
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    return tcs


def _trae_final_to_openai(model: str, finish_reason: str = "stop") -> dict:
    """流结束标记（OpenAI 兼容 finish）。

    finish_reason 必须与实际输出匹配：转出过 tool_calls 时为 "tool_calls"
    （客户端据此执行工具），否则 "stop"（默认）。
    """
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def _trae_nonstream_to_openai(model: str, content_parts: list, reasoning_parts: list, tool_calls: list | None = None) -> dict:
    """非流式：把累积的 response/reasoning 拼成 OpenAI 完成响应；含 tool_calls 时附上。"""
    message: dict = {
        "role": "assistant",
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-trae",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _handle_traework(writer, target, method, path, headers, body, stats, label):
    """Trae Work 协议代理：OpenAI 请求 → llm_utils_chat → OpenAI 响应。

    覆盖 /v1/chat/completions（含流式）。
    认证：Cloud-IDE-JWT（secrets.json 的 trae_work_token）。
    """
    import json as _json
    token = _cfg.resolve_secret(target, _SECRETS) or os.environ.get("TRAE_WORK_TOKEN", "")
    if not token:
        await _write_error_response(writer, 401, "Trae Work token 缺失，请到 dashboard 填写 trae_work_token")
        return
    # 函数内所有日志切到 trae-work 独立文件（traework.log），不污染 proxy.log
    logger = traework_logger
    api_headers = _trae_build_headers(token)

    try:
        # ── /v1/models：优先上游 get_detail_param 实时同步（TTL 缓存），
        #    再按 targets.json enabled=true 白名单过滤（屏蔽空响应/收费模型，如
        #    Doubao-Seed-2.1-Pro / kimi-k3 / DeepSeek-V4-Flash-Official），
        #    失败回退配置白名单，再兜底静态列表 ──
        if path == "/v1/models" and method == "GET":
            models = []
            _tw_tgt = next((t for t in _TARGETS if t.get("label") == "trae-work"), None)
            # 配置白名单：仅 enabled=true 的模型（dashboard 可编辑）
            _cfg_whitelist = []
            for m in (_tw_tgt or {}).get("models", []):
                mid = m.get("id") if isinstance(m, dict) else str(m)
                if mid and (m.get("enabled", False) if isinstance(m, dict) else True):
                    _cfg_whitelist.append(mid)
            upstream = await _trae_fetch_models(token)
            if upstream:
                models = [{"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"}
                          for mid in upstream if mid in _cfg_whitelist]
            if not models:
                for mid in _cfg_whitelist:
                    models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"})
            if not models:
                # 兜底：配置缺失时的静态列表（仅可用模型）
                for mid in ("glm-5.2", "DeepSeek-V4-Pro", "DeepSeek-V4-Flash", "Doubao-Seed-Code"):
                    models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"})
            payload = _json.dumps({"data": models, "object": "list", "has_more": False}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
            await writer.drain()
            writer.close(); return

        # ── /v1/chat/completions：转换 + 转发 ──
        if path == "/v1/chat/completions" and method == "POST":
            try:
                body_json = _json.loads(body.decode("utf-8"))
            except Exception:
                await _write_error_response(writer, 400, "invalid json"); return
            model = (body_json.get("model") or "glm-5.2").split("/")[-1]
            is_stream = bool(body_json.get("stream", False))
            # 注：totalRequests 由外层 _handle_target_request 统一计数，这里不再 +1（避免双重计数）
            _bump_model_stats(label, model, "ok")
            logger.debug(f"[{label}] trae-work {method} {path} model={model} stream={is_stream} "
                         f"req_body={_json.dumps(body_json, ensure_ascii=False)[:2000]}")

            trae_body = _openai_to_trae_body(body_json)
            endpoint = f"{_TRAE_API_HOST}/api/agent/v3/llm_utils_chat"
            logger.debug(f"[{label}] trae-work upstream POST {endpoint} "
                         f"body={_json.dumps(trae_body, ensure_ascii=False)[:3000]}")
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as c:
                req = c.build_request("POST", endpoint, headers=api_headers,
                                      content=_json.dumps(trae_body).encode())
                resp = await c.send(req, stream=True)
                logger.debug(f"[{label}] trae-work upstream HTTP {resp.status_code}")

                if resp.status_code >= 400:
                    resp_body = await resp.aread()
                    logger.debug(f"[{label}] trae-work upstream error body="
                                 f"{resp_body.decode('utf-8', errors='replace')[:4000]}")
                    await _write_error_response(writer, resp.status_code,
                                                f"Trae upstream HTTP {resp.status_code}: {resp_body.decode('utf-8', errors='replace')[:300]}")
                    return

                # ── 流式：Trae SSE → OpenAI SSE ──
                # 架构（2026-08-02 重构，对齐 trae-local-api 官方实现）：response/content
                # 正文只做纯累积（text_buf），不逐 chunk 转发；reasoning_content/tool_calls
                # 等结构化字段（上游明确给出，非文本猜测）逐 chunk 立即转发。上游流结束后，
                # 对累积的完整正文一次性解析工具调用/reasoning，再统一 flush（_resolve_trae_text）。
                # 好处：不存在"标记半截"的判定难题（标记此时必然完整或确定不存在）。
                # 代价：牺牲逐字打字机效果，换取正确性（不会再出现半截标记导致卡顿/丢弃/泄漏）。
                if is_stream:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                    await writer.drain()
                    text_buf = ""  # 本轮累积的 response/content 正文（流结束后统一解析）
                    n_chunks = 0
                    busy_aborted = False
                    emitted_tool_calls = False  # 转出过 tool_calls → 流结束 finish_reason="tool_calls"
                    async for chunk in resp.aiter_bytes():
                        line = chunk.decode("utf-8", errors="replace")
                        for raw in line.split("\n"):
                            raw = raw.strip()
                            if not raw.startswith("data:"):
                                continue
                            data_str = raw[5:].strip()
                            if not data_str:
                                continue
                            try:
                                trae_chunk = _json.loads(data_str)
                            except Exception:
                                continue
                            # 上游 SSE 有非对象 data 行（如 "Processing_xxx" 字符串），跳过
                            if not isinstance(trae_chunk, dict):
                                continue
                            # 排队事件 request_wait_in_queue（字节原生）：模型繁忙，直接终止返回提示
                            _pos = trae_chunk.get("position")
                            if _pos is None and isinstance(trae_chunk.get("data"), dict):
                                _pos = trae_chunk["data"].get("position")
                            if isinstance(_pos, (int, float)) and _pos > 0:
                                logger.warning(f"[{label}] trae-work busy: queue position #{int(_pos)} (model={model}), aborting")
                                oai = _trae_chunk_to_openai({"response": f"[模型繁忙，排队位置 #{int(_pos)}，请稍后重试]"}, model)
                                n_chunks += 1
                                writer.write(("data: " + _json.dumps(oai, ensure_ascii=False) + "\n\n").encode())
                                await writer.drain()
                                busy_aborted = True
                                break
                            # SSE error 事件（上游流式错误 {code,message}）：不再静默吞掉
                            if trae_chunk.get("error") or (trae_chunk.get("code") and "message" in trae_chunk):
                                err_code = trae_chunk.get("code") or ""
                                err_msg = trae_chunk.get("message") or trae_chunk.get("error") or ""
                                logger.warning(f"[{label}] trae-work SSE error: code={err_code} msg={str(err_msg)[:300]}")
                                oai = _trae_chunk_to_openai({"response": f"[Trae error {err_code}: {err_msg}]"}, model)
                                n_chunks += 1
                                writer.write(("data: " + _json.dumps(oai, ensure_ascii=False) + "\n\n").encode())
                                await writer.drain()
                                continue
                            # 只转换 output 事件（旧格式 response/reasoning_content + 新格式 type=text/content/reasoning）
                            if ("response" in trae_chunk or "reasoning_content" in trae_chunk
                                    or "content" in trae_chunk or "reasoning" in trae_chunk
                                    or trae_chunk.get("type") == "text"):
                                resp_text = trae_chunk.get("response") or trae_chunk.get("content") or ""
                                # 上游 progress 提示（旧格式）过滤，避免当正文输出
                                if resp_text.startswith("Building prompt:") or resp_text.startswith("Completed building prompt"):
                                    continue
                                if resp_text:
                                    text_buf += resp_text
                                # reasoning_content/reasoning 是上游明确给出的结构化字段（非文本猜测），
                                # 不存在"标记未闭合"的歧义，可以立即转发
                                reasoning = trae_chunk.get("reasoning_content") or trae_chunk.get("reasoning") or ""
                                if reasoning:
                                    oai_r = _trae_chunk_to_openai({"reasoning_content": reasoning}, model)
                                    n_chunks += 1
                                    writer.write(("data: " + _json.dumps(oai_r, ensure_ascii=False) + "\n\n").encode())
                                    await writer.drain()
                                # 上游原生 tool_calls 字段（非文本解析出来的，结构明确）→ 立即转发
                                if trae_chunk.get("tool_calls"):
                                    oai_tc = _trae_chunk_to_openai({"tool_calls": trae_chunk["tool_calls"]}, model)
                                    emitted_tool_calls = True
                                    n_chunks += 1
                                    logger.debug(f"[{label}] trae-work chunk tool_calls: "
                                                 f"{_json.dumps(trae_chunk.get('tool_calls'), ensure_ascii=False)[:800]}")
                                    writer.write(("data: " + _json.dumps(oai_tc, ensure_ascii=False) + "\n\n").encode())
                                    await writer.drain()
                        if busy_aborted:
                            break
                    # 上游流结束：对累积的完整正文一次性解析（此时标记必然完整或确定不存在）
                    if text_buf:
                        tcs, rtext, content_text = _resolve_trae_text(text_buf)
                        logger.debug(f"[{label}] trae-work resolved: tool_calls={len(tcs)} "
                                     f"reasoning={bool(rtext)} content_len={len(content_text)}")
                        if rtext:
                            oai_r = _trae_chunk_to_openai({"reasoning_content": rtext}, model)
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_r, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                        if content_text:
                            oai_c = _trae_chunk_to_openai({"response": content_text}, model)
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_c, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                        if tcs:
                            emitted_tool_calls = True
                            oai_tc = _trae_chunk_to_openai(
                                {"response": "", "tool_calls": [{"type": "function", "function": tc["function"]} for tc in tcs]},
                                model,
                            )
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_tc, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                    logger.debug(f"[{label}] trae-work stream done, {n_chunks} chunks → client"
                                 + (" (busy abort)" if busy_aborted else ""))
                    writer.write(("data: " + _json.dumps(
                        _trae_final_to_openai(model, "tool_calls" if emitted_tool_calls else "stop"),
                        ensure_ascii=False) + "\n\n").encode())
                    writer.write(b"data: [DONE]\n\n")
                    await writer.drain()
                    stats["passthroughOk"] += 1
                    writer.close(); return

                # ── 非流式：累积 output 事件（架构对齐流式：正文纯累积，流结束后统一解析）──
                resp_body = await resp.aread()
                content_parts, reasoning_parts, tool_calls = [], [], []
                text_buf = ""
                for raw in resp_body.decode("utf-8", errors="replace").split("\n"):
                    raw = raw.strip()
                    if not raw.startswith("data:"):
                        continue
                    data_str = raw[5:].strip()
                    if not data_str:
                        continue
                    try:
                        trae_chunk = _json.loads(data_str)
                    except Exception:
                        continue
                    # 上游 SSE 有非对象 data 行（如 "Processing_xxx" 字符串），跳过
                    if not isinstance(trae_chunk, dict):
                        continue
                    # SSE error 事件（上游流式错误 {code,message}）：不再静默吞掉
                    if trae_chunk.get("error") or (trae_chunk.get("code") and "message" in trae_chunk):
                        err_code = trae_chunk.get("code") or ""
                        err_msg = trae_chunk.get("message") or trae_chunk.get("error") or ""
                        logger.warning(f"[{label}] trae-work SSE error: code={err_code} msg={str(err_msg)[:300]}")
                        content_parts.append(f"[Trae error {err_code}: {err_msg}]")
                        continue
                    resp_text = trae_chunk.get("response") or trae_chunk.get("content") or ""
                    if resp_text:
                        # 上游 progress 提示（旧格式）过滤，避免当正文输出
                        if resp_text.startswith("Building prompt:") or resp_text.startswith("Completed building prompt"):
                            continue
                        text_buf += resp_text
                    reasoning = trae_chunk.get("reasoning_content") or trae_chunk.get("reasoning") or ""
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    tc = trae_chunk.get("tool_calls")
                    if tc:
                        # 上游非流式可能把同一工具调用分片输出（如 glm-5.2 的 arguments
                        # 拆成多个 tool_calls 事件），按 index 合并 name/arguments
                        for oai_tc in _trae_tool_calls_to_openai(tc):
                            idx = oai_tc.get("index", len(tool_calls))
                            if idx < len(tool_calls):
                                prev = tool_calls[idx]
                                fn = prev.get("function", {})
                                cur = oai_tc.get("function", {})
                                if cur.get("name") and not fn.get("name"):
                                    fn["name"] = cur["name"]
                                if cur.get("arguments"):
                                    fn["arguments"] = (fn.get("arguments") or "") + cur["arguments"]
                                prev["function"] = fn
                                if oai_tc.get("id") and not prev.get("id"):
                                    prev["id"] = oai_tc["id"]
                            else:
                                tool_calls.append(oai_tc)
                if text_buf:
                    resolved_tcs, resolved_r, resolved_content = _resolve_trae_text(text_buf)
                    if resolved_r:
                        reasoning_parts.append(resolved_r)
                    if resolved_tcs:
                        tool_calls = resolved_tcs
                    elif resolved_content:
                        content_parts.append(resolved_content)
                out = _trae_nonstream_to_openai(model, content_parts, reasoning_parts, tool_calls or None)
                payload = _json.dumps(out, ensure_ascii=False).encode()
                logger.debug(f"[{label}] trae-work nonstream done: content_parts={len(content_parts)} "
                             f"reasoning={len(reasoning_parts)} tool_calls={len(tool_calls)} "
                             f"out_len={len(payload)}")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                writer.write(payload)
                await writer.drain()
                stats["passthroughOk"] += 1
                writer.close(); return

        # ── 其他路径：透传原生端点 ──
        upstream_url = f"{_TRAE_API_HOST}{path}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False) as c:
            req = c.build_request(method, upstream_url, headers=api_headers, content=body if body else None)
            resp = await c.send(req, stream=True)
            status, _ = await _write_response(writer, resp, stats=stats)
            logger.debug(f"[{label}] trae-work passthrough {method} {path} → HTTP {status}")
            if status and status >= 400:
                logger.warning(f"[{label}] trae-work {path} HTTP {status}")
            return
    except Exception as e:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] trae-work proxy exception")
        try:
            await _write_error_response(writer, 503, f"Trae Work proxy error: {e}")
        except Exception:
            pass


def _handler_prepare_headers(target: dict, fwd_headers: dict, body_json: dict) -> dict:
    """按 handler 类型注入认证与补充 header。

    token 策略：
    - crack 类：注入 secrets token（secrets.json > apikeyEnv），覆盖客户端传入（统一用破解 token）
    - free/paid（passthrough）类：客户端传入的 Authorization 优先（覆盖自己维护的）；
      客户端未传时才用 dashboard 维护的 secrets.json token 兜底
    """
    handler = target.get("handler", "passthrough")
    category = target.get("category", "free")
    # 认证
    if category == "crack":
        token = _cfg.resolve_secret(target, _SECRETS)
        if token:
            fwd_headers["authorization"] = f"Bearer {token}"
        # crack 类凭据唯一事实源是 secrets.json 注入的 authorization；
        # 客户端透传的 x-api-key 必须删除——否则上游（如 codebuddy copilot.tencent.com）
        # 优先用 x-api-key 校验（dummy 值 → 401 invalid_format），无视已注入的 authorization。
        fwd_headers.pop("x-api-key", None)
    elif "authorization" not in fwd_headers:
        # free/paid：客户端未带 token → 用自己维护的 secrets.json / apikeyEnv 兜底
        token = _cfg.resolve_secret(target, _SECRETS)
        if token:
            fwd_headers["authorization"] = f"Bearer {token}"
            logger.debug(f"key: injected maintained token ({target.get('label', 'unknown')})")
    # 补充 header（如 copilot 的 Copilot-Integration-Id）
    for k, v in (target.get("extraHeaders") or {}).items():
        fwd_headers[k] = v
    # qclaw（cleanQclawBody target）上游要求 UA 精确等于 OpenAI/JS 6.39.1
    # 客户端透传的 user-agent（python-httpx 等）必须清除——否则 httpx 会把两个 UA
    # 合并成逗号分隔值（"python-httpx/0.28.1, OpenAI/JS 6.39.1"），上游 400 invalid request。
    # UA 值由 target.extraHeaders 注入（见 _prepare_fwd_headers 上方），此处仅清旧 UA 防合并。
    if target.get("cleanQclawBody"):
        for _hk in [k for k in fwd_headers if k.lower() == "user-agent" and k != "User-Agent"]:
            del fwd_headers[_hk]
    return fwd_headers


# ── 上游路径重写（方案 A：handler 级精准映射）──
# 某些上游（如 Copilot GHE）不提供 /v1 前缀端点；routePrefix="" 时旧逻辑不重写，
# 导致客户端 /v1/chat/completions、/v1/models 被原样转发 → 上游 404。
# 此表按 handler 提供精准映射：key=客户端路径，value=上游路径。
# 注意：不在表内的路径（如 /v1/messages）保留原样，不破坏 Anthropic 链路。
_HANDLER_PATH_MAP = {
    "copilot": {
        "/v1/chat/completions": "/chat/completions",
        "/v1/models": "/models",
        "/v1/responses": "/responses",
    },
}


def _rewrite_upstream_path(handler: str, raw_path: str, route_prefix: str) -> str:
    """按 handler 精准映射上游路径；无映射时退回通用 routePrefix 重写。

    优先级：handler 映射表 > routePrefix 重写 > 原样。
    """
    handler_map = _HANDLER_PATH_MAP.get(handler or "")
    if handler_map and raw_path in handler_map:
        return handler_map[raw_path]
    if route_prefix and raw_path.startswith("/v1"):
        return route_prefix + raw_path[3:]
    return raw_path


def _load_vendor_targets():
    """加载 targets.json + secrets.json，规范化并初始化统计。"""
    global _TARGETS, _SECRETS, _MODELS_CFG, COPILOT_GHE_TOKEN
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        for e in errors:
            logger.warning(f"targets.json 配置错误: {e}")
    _TARGETS = cfg.get("targets", [])
    _MODELS_CFG["models"] = cfg.get("models", [])
    _MODELS_CFG["modelDefaults"] = cfg.get("modelDefaults", {"defaultPort": 8082})
    _SECRETS = _cfg.load_secrets()
    # 私密凭据统一收敛：COPILOT_GHE_TOKEN 与 targets.json copilot-enterprise 的
    # secretRef 同源（crack_copilot.py 提取的企业 GHE PAT 写 copilot_token），
    # 统一从 secrets.json 读取，无则回落 env（兼容旧部署）。
    # 纯配置 COPILOT_GHE_HOST 等仍走 .env。
    _copilot_secret = _SECRETS.get("copilot_token")
    if _copilot_secret:
        COPILOT_GHE_TOKEN = _copilot_secret
    for t in _TARGETS:
        label = t["label"]
        if label not in _TARGET_STATS:
            _TARGET_STATS[label] = {
                "totalRequests": 0, "translated429": 0,
                "passthroughOk": 0, "passthroughError": 0,
                "startedAt": datetime.now().isoformat(),
            }
    print(f"🔀 Targets loaded: {len(_TARGETS)} targets, models={len(_MODELS_CFG.get('models', []))}")

_load_vendor_targets()


def _refresh_secrets():
    """重读 secrets.json 到内存（热生效）。"""
    global _SECRETS, COPILOT_GHE_TOKEN
    _SECRETS = _cfg.load_secrets()
    # 私密凭据热重载：dashboard 改 copilot_token 后无需重启即生效
    _copilot_secret = _SECRETS.get("copilot_token")
    if _copilot_secret:
        COPILOT_GHE_TOKEN = _copilot_secret
    logger.info(f"🔑 secrets.json reloaded ({len(_SECRETS)} keys)")


# ── 热重载：mtime 轮询 + 端口 diff ──
_target_servers: Dict[int, asyncio.Server] = {}
_config_mtimes: Dict[str, float] = {}


async def _reload_targets() -> list:
    """重载 targets.json / secrets.json，diff 端口并动态增删 server。"""
    global _TARGETS, _SECRETS, _MODELS_CFG, COPILOT_GHE_TOKEN
    changes = []
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        logger.error(f"配置校验失败，拒绝重载: {errors}")
        return [f"❌ 校验失败: {errors}"]
    _TARGETS = cfg.get("targets", [])
    _MODELS_CFG["models"] = cfg.get("models", [])
    _MODELS_CFG["modelDefaults"] = cfg.get("modelDefaults", {"defaultPort": 8082})
    _SECRETS = _cfg.load_secrets()
    # 私密凭据热重载：COPILOT_GHE_TOKEN 同步 secrets.json copilot_token（dashboard 可编辑热生效）
    _copilot_secret = _SECRETS.get("copilot_token")
    if _copilot_secret:
        COPILOT_GHE_TOKEN = _copilot_secret

    # ── 聚合网关单例：找到聚合 target 则按需初始化/reload，找不到则清空单例 ──
    global _AGGREGATOR_ENGINE, _AGGREGATOR_CONFIG_SIG
    agg_target = next((t for t in _TARGETS if t.get("handler") == "aggregator"), None)
    if agg_target is None:
        _AGGREGATOR_ENGINE = None
        _AGGREGATOR_CONFIG_SIG = None
    else:
        sig = json.dumps(agg_target, sort_keys=True, ensure_ascii=False)
        if _AGGREGATOR_ENGINE is None:
            try:
                import aggregator as _agg
                _AGGREGATOR_ENGINE = _agg.AggregatorEngine.from_target(agg_target)
                _AGGREGATOR_CONFIG_SIG = sig
                logger.info("♻️  聚合网关引擎已初始化")
            except Exception:
                logger.exception("聚合网关引擎初始化失败")
        elif sig != _AGGREGATOR_CONFIG_SIG:
            try:
                _AGGREGATOR_ENGINE.reload(agg_target)
                _AGGREGATOR_CONFIG_SIG = sig
                logger.info("♻️  聚合网关引擎已 reload")
            except Exception:
                logger.exception("聚合网关引擎 reload 失败")

    # 统计表补新 target
    for t in _TARGETS:
        if t["label"] not in _TARGET_STATS:
            _TARGET_STATS[t["label"]] = {
                "totalRequests": 0, "translated429": 0,
                "passthroughOk": 0, "passthroughError": 0,
                "startedAt": datetime.now().isoformat(),
            }

    # diff 端口
    wanted = {t["listenPort"]: t for t in _TARGETS if t.get("enabled", True)}
    for port in list(_target_servers.keys()):
        if port not in wanted:
            _target_servers[port].close()
            await _target_servers[port].wait_closed()
            del _target_servers[port]
            changes.append(f"移除端口 {port}")
    for port, t in wanted.items():
        if port not in _target_servers:
            try:
                srv = await _vendor_server("0.0.0.0", port, t)
                _target_servers[port] = srv
                changes.append(f"新增端口 {port} ({t['label']})")
            except OSError as e:
                logger.error(f"无法监听端口 {port}: {e}")
    logger.info(f"♻️  配置热重载完成: {changes if changes else '无端口变化'}")
    return changes


async def _config_watcher():
    """每 2s 轮询 targets.json / secrets.json mtime，变更即重载。"""
    while True:
        await asyncio.sleep(2)
        try:
            for path in (_cfg.TARGETS_PATH, _cfg.SECRETS_PATH):
                try:
                    mtime = path.stat().st_mtime
                except FileNotFoundError:
                    mtime = 0
                if _config_mtimes.get(str(path)) is None:
                    _config_mtimes[str(path)] = mtime
                elif mtime != _config_mtimes[str(path)]:
                    _config_mtimes[str(path)] = mtime
                    logger.info(f"♻️  检测到 {path.name} 变更")
                    await _reload_targets()
                    break
        except Exception as e:
            logger.warning(f"config watcher error: {e}")


def _run_crack_tool(crack_tool: str) -> bool:
    """调用破解工具脚本提取 token（超时 30s）。成功返回 True。"""
    import subprocess
    # ── OS 守卫：非 Windows 的本地客户端破解（qclaw/codebuddy）暂不支持 ──
    if sys.platform != "win32" and "copilot" not in (crack_tool or ""):
        logger.warning(
            f"🔓 破解工具 {crack_tool} 跳过：当前 OS={sys.platform} 仅支持 Windows 本地客户端破解，待后续补齐"
        )
        return False
    script = Path(__file__).parent / crack_tool
    if not script.exists():
        logger.warning(f"破解工具不存在: {script}")
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(__file__).parent),
        )
        if r.returncode == 0:
            logger.info(f"🔓 破解工具 {crack_tool} 成功: {r.stdout.strip()[:200]}")
            _refresh_secrets()
            return True
        logger.warning(f"🔓 破解工具 {crack_tool} 失败 (rc={r.returncode}): {r.stdout.strip()[:200]}")
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"🔓 破解工具 {crack_tool} 超时（30s）")
        return False
    except Exception as e:
        logger.warning(f"🔓 破解工具 {crack_tool} 异常: {e}")
        return False


def _crack_env_check(target: dict) -> dict:
    """检测当前环境是否具备运行 crack 工具的软件依赖。

    返回 {"available": bool, "reason": str}。
    - copilot: 需要 gh CLI 在 PATH 中（跨平台，仅命令行依赖）
    - codebuddy: 仅支持 Windows 本地目录探测；其他 OS 提示未实现
    - qclaw: 仅支持 Windows DPAPI 解密（app-store.json）；其他 OS 提示未实现
    - trae-work: 预留骨架，未实现 → 不可用
    """
    import shutil
    tool = target.get("crackTool", "")
    if not tool:
        return {"available": False, "reason": "未配置破解工具"}
    label = target.get("label", "")
    _os = sys.platform

    # ── 操作系统支持检查：目前仅 Windows 实现了本地客户端破解 ──
    if "copilot" not in tool and _os != "win32":
        return {
            "available": False,
            "reason": f"当前操作系统（{_os}）暂无 {label} 破解实现，仅支持 Windows（DPAPI/客户端目录探测），待后续补齐",
        }

    if "copilot" in tool:
        if shutil.which("gh"):
            return {"available": True, "reason": "gh CLI 已检测到"}
        return {"available": False, "reason": "未检测到 gh CLI（GitHub CLI），无法自动提取 Copilot token"}

    if "codebuddy" in tool:
        base_dirs = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
            str(Path.home()),
        ]
        found = any(
            base and (Path(base) / "CodeBuddy").exists() or (Path(base) / "CodeBuddy Code").exists()
            for base in base_dirs
        )
        if found:
            return {"available": True, "reason": "检测到 CodeBuddy 客户端目录"}
        return {"available": False, "reason": "未检测到 CodeBuddy 客户端安装目录，无法自动提取 token"}

    if "qclaw" in tool:
        if os.environ.get("QCLAW_API_KEY"):
            return {"available": True, "reason": "QCLAW_API_KEY 环境变量已设置"}
        appdata = os.environ.get("APPDATA", "")
        if appdata and (Path(appdata) / "QClaw" / "app-store.json").exists():
            return {"available": True, "reason": "检测到 QClaw 客户端登录信息"}
        return {"available": False, "reason": "未检测到 QClaw 客户端（app-store.json 缺失），无法本地提取 API Key"}

    if "traework" in tool or "trae" in tool:
        # Trae Work 认证数据在 %APPDATA%\\TRAE SOLO CN\\User\\globalStorage\\storage.json
        # （iCubeAuthInfo://icube.cloudide，tc 加密）；token 已在 secrets.json 时无需本地客户端
        env_dir = os.environ.get("TRAE_WORK_DATA_DIR", "")
        appdata = os.environ.get("APPDATA", "")
        candidates = []
        if env_dir:
            candidates.append(Path(env_dir))
        if appdata:
            candidates.append(Path(appdata) / "TRAE SOLO CN")
        storage_json = next(
            (d / "User" / "globalStorage" / "storage.json" for d in candidates
             if (d / "User" / "globalStorage" / "storage.json").exists()),
            None,
        )
        if storage_json:
            return {"available": True, "reason": f"检测到 Trae Work 登录数据（{storage_json.parent}）"}
        return {"available": False, "reason": "未检测到 Trae Work 客户端登录数据（storage.json 缺失）；可在 dashboard 手动填写 trae_work_token"}

    # 兜底：无法判断时视为可用（不阻止用户手动尝试）
    return {"available": True, "reason": "无法判断依赖，允许尝试"}


# ─── 8081 Anthropic 协议端口（对内透传 8082，模型列表隔离） ───

_ANTHROPIC_PORT = int(os.environ.get("ANTHROPIC_PORT", "8081"))


def _anthropic_port_models() -> List[dict]:
    """8081 Anthropic 端口模型列表——动态来自 targets.json 顶层 models[]。

    与 dashboard「模型定义」编辑视图同一数据源（_MODELS_CFG["models"]）：
    监控视图展示什么，编辑视图就改什么，杜绝"展示但不可用"的歧义。
    取代硬编码常量（曾含 claude-opus-4-20250514 等不可用死名单——那些模型
    名无法被 _resolve_model_alias 命中，只会在客户端模型列表里误导用户）。

    返回 [{id, display_name, aliases, target}]：id=name 主模型名，aliases 为其
    别名（均可被 _resolve_model_alias 命中），target 为下游端口+真实模型。
    """
    out: List[dict] = []
    for m in _MODELS_CFG.get("models", []):
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m["name"])
        aliases = [str(a) for a in (m.get("aliases") or []) if isinstance(a, str)]
        tgt = m.get("target")
        out.append({
            "id": name,
            "display_name": _humanize_model_name(name),
            "aliases": aliases,
            "target": {"port": tgt.get("port"), "model": tgt.get("model")} if isinstance(tgt, dict) else {},
        })
    return out

_ANTHROPIC_STATS: Dict[str, int] = {"totalRequests": 0, "passthroughOk": 0, "passthroughError": 0, "startedAt": datetime.now().isoformat()}


# ─── 8082 OpenAI 协议端口（asyncio TCP，纯透传 + 多 provider 路由）───

_OPENAI_PORT = int(os.environ.get("OPENAI_PORT", "8082"))
_OPENAI_STATS: Dict[str, int] = {"totalRequests": 0, "passthroughOk": 0, "passthroughError": 0, "startedAt": datetime.now().isoformat()}


def _choose_openai_upstream(body_json: dict):
    """根据 PREFERRED_PROVIDER 选择上游 URL 和鉴权 headers"""
    provider = PREFERRED_PROVIDER
    if provider == "copilot":
        return (
            f"https://{COPILOT_GHE_HOST}/chat/completions",
            {
                "Authorization": f"Bearer {COPILOT_GHE_TOKEN}",
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "Content-Type": "application/json",
            },
        )
    elif provider in ("qclaw",):
        return (
            f"{QCLAW_BASE_URL}/chat/completions",
            {
                "Authorization": f"Bearer {QCLAW_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "OpenAI/JS 6.39.1",
            },
        )
    elif provider == "openai":
        base = OPENAI_BASE_URL or "https://api.openai.com/v1"
        return (
            f"{base.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        )
    else:
        # 兜底走 copilot
        return (
            f"https://{COPILOT_GHE_HOST}/chat/completions",
            {
                "Authorization": f"Bearer {COPILOT_GHE_TOKEN}",
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "Content-Type": "application/json",
            },
        )


async def _handle_openai_proxy_request(reader, writer):
    """8082 OpenAI 端口：固定为 copilot target（经统一透传引擎）。
    保留为兼容入口：若 8082 未在 targets.json 中配置，则此函数兜底转发。
    """
    copilot_target = next((t for t in _TARGETS if t["listenPort"] == _OPENAI_PORT), None)
    if copilot_target:
        await _handle_target_request(reader, writer, copilot_target)
        return
    # 兜底：无 target 配置时保持旧行为（透传 + 基本自检）
    try:
        method, path, raw_path, headers, body = await _parse_http_request(reader)
        if method is None:
            return
        if path == "/__proxy_info__":
            payload = json.dumps({
                "label": "claude-code-openai", "listenPort": _OPENAI_PORT,
                "targetHost": "unconfigured", "targetPort": 443, "targetProtocol": "https",
                "models": [], "retryAfterSeconds": 0, "errorPatterns": [],
                "startedAt": _OPENAI_STATS["startedAt"],
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return
        await _write_error_response(writer, 503, "8082 target not configured in targets.json")
    except Exception:
        _OPENAI_STATS["passthroughError"] += 1
        try:
            await _write_error_response(writer, 503, "OpenAI proxy error")
        except Exception:
            pass


async def _openai_server(host="0.0.0.0", port=8082):
    srv = await asyncio.start_server(_handle_openai_proxy_request, host=host, port=port)
    print(f"🔀 [claude-code-openai] 0.0.0.0:{port} -> {PREFERRED_PROVIDER} upstream (multi-provider routing)")
    return srv


async def _handle_anthropic_proxy_request(reader, writer):
    """8081 Anthropic 专用端口：/v1/messages 翻译成 OpenAI 格式后内部请求 8082，其余透传"""
    try:
        req_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not req_line:
            writer.close(); return
        parts = req_line.decode("utf-8", errors="replace").strip().split(" ", 2)
        method = parts[0] if len(parts) > 0 else "GET"
        raw_path = parts[1] if len(parts) > 1 else "/"

        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str: break
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        content_len = int(headers.get("content-length", 0))
        body = b""
        if content_len > 0:
            body = await asyncio.wait_for(reader.read(content_len), timeout=30)

        parsed = urlparse(raw_path)
        path = parsed.path

        # ── 自检端点 ──
        if path == "/__proxy_info__":
            import json as _json
            payload = _json.dumps({
                "label": "claude-code-anthropic", "listenPort": _ANTHROPIC_PORT,
                "targetHost": "127.0.0.1", "targetPort": 8082, "targetProtocol": "http",
                "models": [m["id"] for m in _anthropic_port_models()],
                "retryAfterSeconds": 0, "errorPatterns": [],
                "startedAt": _ANTHROPIC_STATS["startedAt"],
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        if path == "/__proxy_stats__":
            import json as _json
            payload = _json.dumps({"label": "claude-code-anthropic", "listenPort": _ANTHROPIC_PORT, **_ANTHROPIC_STATS})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        # ── /v1/models 拦截：只返回 Anthropic 模型（动态来自 targets.json models[]）──
        if path == "/v1/models" and method == "GET":
            import json as _json
            filtered = _anthropic_port_models()
            for m in filtered:
                m.setdefault("object", "model")
                m.setdefault("type", "model")
                m.setdefault("created", 1700000000)
                m.setdefault("owned_by", "anthropic")
            payload = _json.dumps({"data": filtered, "object": "list", "has_more": False})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        _ANTHROPIC_STATS["totalRequests"] += 1

        # ── /v1/messages：Anthropic→OpenAI 翻译，内部请求 8082 ──
        if path == "/v1/messages" and method == "POST":
            from anthropic_convert import convert_anthropic_request_to_openai, convert_openai_response_to_anthropic
            try:
                anthropic_body = json.loads(body.decode("utf-8"))
            except Exception:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n\r\n{\"error\":\"invalid json\"}")
                await writer.drain(); writer.close(); return

            original_model = anthropic_body.get("model", "unknown")
            is_stream = anthropic_body.get("stream", False)
            openai_body = convert_anthropic_request_to_openai(anthropic_body)

            # 8081 转发目标映射：models[] 命中则用配置的端口+模型（支持聚合 agg:xxx → 8080），未命中回退 modelDefaults.defaultPort
            mapped = _cfg._resolve_model_alias(_MODELS_CFG, original_model)
            if mapped:
                fwd_port = int(mapped["port"])
                openai_body["model"] = mapped["model"]
            else:
                fwd_port = int(_MODELS_CFG["modelDefaults"].get("defaultPort", 8082))

            openai_payload = json.dumps(openai_body).encode("utf-8")

            fwd_headers = {
                "content-type": "application/json",
                "host": f"127.0.0.1:{fwd_port}",
                "content-length": str(len(openai_payload)),
            }
            if headers.get("authorization"):
                fwd_headers["authorization"] = headers["authorization"]
            if headers.get("x-api-key"):
                fwd_headers["x-api-key"] = headers["x-api-key"]

            upstream_url = f"http://127.0.0.1:{fwd_port}/v1/chat/completions"

            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                req = client.build_request("POST", upstream_url, headers=fwd_headers, content=openai_payload)
                resp = await client.send(req, stream=is_stream)

                if is_stream:
                    _ANTHROPIC_STATS["passthroughOk"] += 1
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                    async for chunk in resp.aiter_bytes():
                        writer.write(chunk)
                        await writer.drain()
                    writer.write(b"data: [DONE]\n\n")
                    await writer.drain()
                    writer.close(); return

                body_bytes = await resp.aread()
                try:
                    openai_resp = json.loads(body_bytes.decode("utf-8"))
                except Exception:
                    _ANTHROPIC_STATS["passthroughError"] += 1
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain(); writer.close(); return

                if resp.status_code >= 400:
                    _ANTHROPIC_STATS["passthroughOk"] += 1
                    content_len = len(body_bytes)
                    writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'Error'}\r\nContent-Type: application/json\r\nContent-Length: {content_len}\r\n\r\n".encode())
                    writer.write(body_bytes)
                    await writer.drain(); writer.close(); return

                anthropic_resp = convert_openai_response_to_anthropic(openai_resp, original_model)
                resp_payload = json.dumps(anthropic_resp).encode("utf-8")
                _ANTHROPIC_STATS["passthroughOk"] += 1
                _bump_model_stats("anthropic", original_model, "ok")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_payload)}\r\n\r\n".encode())
                writer.write(resp_payload)
                await writer.drain()
                writer.close(); return
            return

        # ── 其余请求：透传到默认转发端口 ──
        fwd_port = int(_MODELS_CFG["modelDefaults"].get("defaultPort", 8082))
        upstream_url = f"http://127.0.0.1:{fwd_port}{raw_path}"
        fwd_headers = {k: v for k, v in headers.items() if k not in ("host", "connection")}
        fwd_headers["host"] = f"127.0.0.1:{fwd_port}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
            req = client.build_request(method, upstream_url, headers=fwd_headers, content=body if body else None)
            resp = await client.send(req, stream=True)

            content_type = resp.headers.get("content-type", "")
            is_stream = "text/event-stream" in content_type
            if is_stream:
                writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n".encode())
                for k, v in resp.headers.items():
                    if k.lower() not in _PROXY_STRIP_RESP_HEADERS:
                        writer.write(f"{k}: {v}\r\n".encode())
                writer.write(b"\r\n")
                async for chunk in resp.aiter_bytes():
                    writer.write(chunk)
                    await writer.drain()
                _ANTHROPIC_STATS["passthroughOk"] += 1
                writer.close(); return

            body_bytes = await resp.aread()
            _ANTHROPIC_STATS["passthroughOk"] += 1
            resp_headers = "".join(f"{k}: {v}\r\n" for k, v in resp.headers.items() if k.lower() not in _PROXY_STRIP_RESP_HEADERS)
            writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n{resp_headers}Content-Length: {len(body_bytes)}\r\n\r\n".encode())
            writer.write(body_bytes)
            await writer.drain()
    except Exception:
        _ANTHROPIC_STATS["passthroughError"] += 1
        try: writer.close()
        except: pass


async def _anthropic_server(host="0.0.0.0", port=8081):
    srv = await asyncio.start_server(_handle_anthropic_proxy_request, host=host, port=port)
    print(f"🔀 [claude-code-anthropic] 0.0.0.0:{port} -> http://127.0.0.1:{int(_MODELS_CFG['modelDefaults'].get('defaultPort', 8082))} (Anthropic protocol, /v1/models isolated)")
    return srv


async def _handle_aggregate_request(reader, writer, target, method, path, raw_path, headers, body):
    """聚合网关（8080）请求分发：解析虚拟模型 → AggregatorEngine 路由 → 转发到池成员真实端口。

    仅做路由/熔断编排，不解析任何 secretRef/apikeyEnv（聚合层不持有凭据，
    转发目标是本地其他 target 端口，鉴权由那些端口自身处理）。
    聚合层不透传客户端凭据（authorization/x-api-key）——凭据统一由各下游
    端口从 secrets.json 解析注入。
    """
    import aggregator as _agg

    label = target["label"]
    global _AGGREGATOR_ENGINE
    if _AGGREGATOR_ENGINE is None:
        try:
            _AGGREGATOR_ENGINE = _agg.AggregatorEngine.from_target(target)
        except Exception as e:
            logger.exception(f"[{label}] AggregatorEngine 初始化失败")
            await _write_error_response(writer, 500, f"聚合网关初始化失败: {e}")
            return
    engine = _AGGREGATOR_ENGINE

    try:
        body_json = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        body_json = {}

    virtual_model = body_json.get("model") if isinstance(body_json, dict) else None
    known_models = engine.list_virtual_models()
    if not virtual_model or virtual_model not in known_models:
        err_payload = json.dumps({
            "error": {
                "type": "invalid_request_error",
                "message": f"未知或缺失的虚拟模型 '{virtual_model}'，已配置模型: {known_models}",
            }
        })
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(err_payload.encode()), err_payload.encode()))
        await writer.drain(); writer.close(); return

    session_id = (
        headers.get("x-session-id")
        or headers.get("x-conversation-id")
        or (body_json.get("conversation_id") if isinstance(body_json, dict) else None)
        or (body_json.get("session_id") if isinstance(body_json, dict) else None)
        or (body_json.get("user") if isinstance(body_json, dict) else None)
    )

    async def send_fn(member, info):
        member_body = dict(body_json)
        member_body["model"] = member.model
        member_body_bytes = json.dumps(member_body, ensure_ascii=False).encode("utf-8")

        # 聚合层不透传客户端凭据（authorization / x-api-key）：
        # 转发目标是本地 target 端口，凭据由各下游端口自己从 secrets.json 解析注入
        # （crack 注入 secretRef、free/paid 客户端未带 key 时用 secrets.json 兜底）。
        # 客户端连 8080 时带的 key（如 dummy）对聚合层无意义，透传只会覆盖下游
        # 的真实凭据导致 401。
        fwd_headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("host", "connection", "content-length", "transfer-encoding",
                                            "authorization", "x-api-key")}
        fwd_headers["host"] = f"127.0.0.1:{member.port}"

        client = await get_http_client()
        req = client.build_request(method, f"http://127.0.0.1:{member.port}{raw_path}", headers=fwd_headers, content=member_body_bytes)
        resp = await client.send(req, stream=True)
        return resp

    stats = _TARGET_STATS.setdefault(label, {
        "totalRequests": 0, "translated429": 0,
        "passthroughOk": 0, "passthroughError": 0,
        "startedAt": datetime.now().isoformat(),
    })
    stats["totalRequests"] += 1

    try:
        member, resp = await engine.route_request(virtual_model, session_id, send_fn)
    except _agg.AllPoolsExhausted as e:
        stats["passthroughError"] += 1
        await _write_error_response(writer, 503, f"聚合网关 '{virtual_model}' 池已耗尽: {e}")
        return
    except Exception as e:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] 聚合路由异常")
        await _write_error_response(writer, 502, f"聚合网关路由失败: {e}")
        return

    content_type = resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type
    if not is_stream:
        body_text = (await resp.aread()).decode("utf-8", errors="replace")
        if engine.quota_error(body_text):
            engine.trip(member.port, "quota_error")
        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n{resp_headers}Content-Length: {len(body_text.encode())}\r\n\r\n".encode())
        writer.write(body_text.encode("utf-8"))
        await writer.drain()
        writer.close()
        stats["passthroughOk"] += 1
        return

    await _write_response(writer, resp, stats=stats)


async def _aggregator_prober():
    """每 5s 检查聚合网关的熔断端口是否到期，到期则发探测请求判定恢复。"""
    while True:
        await asyncio.sleep(5)
        engine = _AGGREGATOR_ENGINE
        if engine is None:
            continue
        try:
            due_ports = engine.probe_due_ports()
            for port in due_ports:
                ok = False
                try:
                    client = await get_http_client()
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        json={"model": "probe", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                        headers={"Content-Type": "application/json"},
                        timeout=httpx.Timeout(5.0),
                    )
                    ok = resp.status_code < 500
                except Exception:
                    ok = False
                engine.record_probe_result(port, ok)
        except Exception:
            logger.exception("aggregator prober error")


async def _handle_target_request(reader, writer, target):
    """统一透传引擎：处理单个 target 端口的全部请求。
    与原 _handle_vendor_request 兼容，新增 handler 分发 / 鉴权注入 / 401 缺 token。
    """
    label = target["label"]
    stats = _TARGET_STATS.setdefault(label, {
        "totalRequests": 0, "translated429": 0,
        "passthroughOk": 0, "passthroughError": 0,
        "startedAt": datetime.now().isoformat(),
    })
    try:
        # 可变状态对象：跟踪响应 headers 是否已写入下游（用于流式中途异常时避免二次写状态行）
        write_state = {"headers_sent": False}
        method, path, raw_path, headers, body = await _parse_http_request(reader)
        if method is None:
            return

        # ── 内建 JSON 端点 ──
        if path == "/__proxy_info__":
            payload = json.dumps({
                "label": label, "listenPort": target["listenPort"],
                "category": target.get("category", ""),
                "handler": target.get("handler", "passthrough"),
                "isFree": target.get("isFree", False),
                "targetHost": target["targetHost"], "targetPort": target.get("targetPort", 443),
                "targetProtocol": target.get("targetProtocol", "https"),
                "models": target.get("models", []),
                "retryAfterSeconds": _VENDOR_RETRY_AFTER,
                "errorPatterns": [p.pattern for p in _VENDOR_ERROR_PATTERNS],
                "startedAt": stats["startedAt"],
                "secretSet": bool(_cfg.resolve_secret(target, _SECRETS)),
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        if path == "/__proxy_stats__":
            payload = json.dumps({
                "label": label, "listenPort": target["listenPort"],
                **stats,
                "modelStats": _MODEL_STATS.get(label, {}),
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        # ── /dashboard：代理到 8081 FastAPI ──
        if path == "/dashboard" and method == "GET":
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as c:
                resp = await c.get("http://127.0.0.1:8081/dashboard")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n\r\n%s" % (len(resp.content), resp.content))
                await writer.drain()
            writer.close(); return

        # ── /api/*：代理到 8081 FastAPI（dashboard 管理接口）──
        # 用户可能通过任意 target 端口访问 /dashboard，页面内 JS 的 fetch('/api/...')
        # 是相对路径，会发到当前端口——必须透传回 8081，否则 404。
        # 注意：routePrefix 以 /api/ 开头的 target（如 openrouter 的 /api/v1），其上游
        # 路径 /api/v1/chat/completions 不能被误判为 dashboard API 劫持，需排除透传前缀。
        _route_prefix = target.get("routePrefix", "")
        if (path == "/api" or path.startswith("/api/")) and not (
            _route_prefix and path.startswith(_route_prefix)
        ):
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False) as c:
                fwd = {k: v for k, v in headers.items() if k.lower() not in ("host", "connection", "content-length")}
                fwd["host"] = "127.0.0.1:8081"
                req = c.build_request(method, f"http://127.0.0.1:8081{raw_path}", headers=fwd, content=body if body else None)
                resp = await c.send(req, stream=True)
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n".encode())
                    for k, v in resp.headers.items():
                        if k.lower() not in _PROXY_STRIP_RESP_HEADERS:
                            writer.write(f"{k}: {v}\r\n".encode())
                    writer.write(b"\r\n")
                    # 标记 headers 已写入（避免后续异常时二次写状态行）
                    write_state["headers_sent"] = True
                    async for chunk in resp.aiter_bytes():
                        writer.write(chunk)
                        await writer.drain()
                else:
                    resp_body = await resp.aread()
                    resp_headers = "".join(
                        f"{k}: {v}\r\n" for k, v in resp.headers.items()
                        if k.lower() not in _PROXY_STRIP_RESP_HEADERS
                    )
                    writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n{resp_headers}Content-Length: {len(resp_body)}\r\n\r\n".encode())
                    writer.write(resp_body)
                    await writer.drain()
            writer.close(); return

        stats["totalRequests"] += 1

        # ── qclaw /v1/models：上游（mmgrcalltoken.3g.qq.com）不提供 /models 接口，
        #    转发会得到 404，导致 opencode 等客户端拉不到模型列表。这里直接返回
        #    targets.json 中 qclaw target 声明的 enabled=true 模型（OpenAI 格式），
        #    作为 qclaw 的"官方模型列表"单一事实源（与 _handle_traework 的 models 处理一致）。
        #    放在 crack 401 检查之前：模型列表是元数据，缺 token 也应可列出。
        if target.get("label") == "qclaw" and path == "/v1/models" and method == "GET":
            _qclaw_models = [
                {"id": m["id"], "object": "model", "created": 1700000000, "owned_by": "qclaw"}
                for m in (target.get("models") or [])
                if isinstance(m, dict) and m.get("enabled", False)
            ]
            _qclaw_payload = json.dumps(
                {"data": _qclaw_models, "object": "list", "has_more": False}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                % (len(_qclaw_payload), _qclaw_payload)
            )
            await writer.drain()
            writer.close()
            return

        # ── crack 类缺 token → 401（不转发上游）──
        if target.get("category") == "crack" and not _cfg.resolve_secret(target, _SECRETS):
            err_payload = json.dumps({
                "error": {
                    "type": "missing_token",
                    "message": f"请到 dashboard (http://127.0.0.1:8081/dashboard) 填写 {target.get('secretRef', label)} token",
                }
            })
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(err_payload.encode()), err_payload.encode()))
            await writer.drain(); writer.close(); return

        # ── Gemini 原生协议代理（OpenAI 请求 ↔ generateContent）──
        if target.get("handler") == "gemini-native":
            await _handle_gemini_native(writer, target, method, path, headers, body, stats, label)
            return

        # ── Trae Work 协议代理（OpenAI 请求 → llm_utils_chat）──
        if target.get("handler") == "trae-work":
            await _handle_traework(writer, target, method, path, headers, body, stats, label)
            return

        # ── 聚合网关（虚拟模型 → 池成员路由 + 会话粘性 + 熔断）──
        if target.get("handler") == "aggregator":
            await _handle_aggregate_request(reader, writer, target, method, path, raw_path, headers, body)
            return

        # ── 上游转发（含路径重写 + handler body/header 处理）──
        body_bytes, body_json, cross_port_target = _handler_prepare_body(target, body)
        # 模型级统计：解析请求模型名（映射后真实模型）
        _req_model = None
        if body_json and isinstance(body_json, dict):
            _req_model = body_json.get("model")
        elif body:
            try:
                _req_model = json.loads(body).get("model")
            except Exception:
                pass

        # ── codebuddy 请求入站诊断日志（2026-08-05）──
        # 独立 codebuddy.log：记录请求 model/stream/body 摘要 + system prompt 前 200 字符，
        # 用于定位上游 content_filter 拦截的触发因素（透传模式下客户端只能看到 200 空 SSE）。
        if target.get("label") == "codebuddy" and _req_model:
            _sys_preview = ""
            if body_json and isinstance(body_json, dict):
                for _m in (body_json.get("messages") or []):
                    if isinstance(_m, dict) and _m.get("role") == "system":
                        _sc = _m.get("content")
                        if isinstance(_sc, str):
                            _sys_preview = _sc[:200]
                        elif isinstance(_sc, list):
                            _sys_preview = json.dumps(_sc, ensure_ascii=False)[:200]
                        break
            _cb_body_preview = body[:300].decode("utf-8", errors="replace") if body else ""
            codebuddy_logger.debug(
                f"[codebuddy] {method} {path} model={_req_model} stream={bool(body_json and body_json.get('stream'))} "
                f"sys[:200]={_sys_preview!r} body[:300]={_cb_body_preview!r}"
            )

        # ── Copilot /responses 桥接：responsesModels 名单内的模型走 /responses 协议 ──
        # 上游部分模型（gpt-5.6-terra 等）只支持 /responses 端点，不支持 /chat/completions。
        # 客户端统一用 OpenAI chat.completions 格式请求，网关负责双向格式转换：
        #   chat.completions body → Responses API body（发送前）
        #   Responses API 响应   → chat.completions 响应（接收后，流式/非流式）
        _use_responses = False
        if (
            target.get("handler") == "copilot"
            and raw_path in ("/v1/chat/completions", "/chat/completions")
            and _req_model
            and str(_req_model) in (target.get("responsesModels") or [])
            and body_json and isinstance(body_json, dict)
        ):
            _use_responses = True
            body_bytes = json.dumps(
                _copilot_chat_to_responses_body(body_json), ensure_ascii=False
            ).encode("utf-8")
            logger.info(f"[{label}] responses bridge: {_req_model} → /responses (chat→responses)")

        # ── 跨端口路由：请求模型命中 models[] 别名且 target.port 指向另一端口 → 整体改路由 ──
        # Todo 7 已在 _handler_prepare_body 里产出 cross_port_target（命中跨端口时只回信号，不改 body model）；
        # 这里统一消费信号：改写 body.model 为目标模型、重序列化 body_bytes，并把 upstream 切到目标端口。
        # 跨端口场景下透传原始客户端 headers（目标端口自己处理鉴权/重写），不注入本 target 凭据。
        if cross_port_target:
            _cross_port = int(cross_port_target["port"])
            if body_json and isinstance(body_json, dict):
                body_json["model"] = cross_port_target["model"]
                body_bytes = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
            upstream_url = f"http://127.0.0.1:{_cross_port}{raw_path}"
            fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "connection", "content-length", "transfer-encoding")}
            fwd_headers["host"] = f"127.0.0.1:{_cross_port}"
            logger.info(f"[{label}] cross-port route: {_req_model} → 127.0.0.1:{_cross_port}{raw_path} model={cross_port_target['model']}")
        else:
            transport = "https" if target.get("targetProtocol", "https") == "https" else "http"
            upstream_path = _rewrite_upstream_path(
                target.get("handler", "passthrough"),
                raw_path,
                target.get("routePrefix", ""),
            )
            if _use_responses:
                upstream_path = "/responses"
            upstream_url = f"{transport}://{target['targetHost']}:{target.get('targetPort', 443)}{upstream_path}"
            fwd_headers = _resolve_auth(headers, target=target)
            fwd_headers["host"] = target["targetHost"]
            fwd_headers = _handler_prepare_headers(target, fwd_headers, body_json)

        async with httpx.AsyncClient(timeout=_TARGET_HTTPX_TIMEOUT, trust_env=False) as client:
            req = client.build_request(method, upstream_url, headers=fwd_headers, content=body_bytes if body_bytes else None)
            resp = await client.send(req, stream=True)

            content_type = resp.headers.get("content-type", "")
            is_stream = "text/event-stream" in content_type

            if is_stream:
                if _use_responses:
                    # 上游 /responses SSE → chat.completions SSE
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n\r\n")
                    await writer.drain()
                    # 标记 headers 已写入（避免后续异常时二次写状态行）
                    write_state["headers_sent"] = True
                    await _write_copilot_responses_stream(writer, resp, str(_req_model), label)
                    if _req_model:
                        _bump_model_stats(label, _req_model, "ok")
                    writer.close()
                    return
                # normalizeSse 为真时强制开启 log_sse——规范化依赖行缓冲逐帧处理，
                # 二者共用同一条链路（配置驱动，不硬编码 label）。
                _normalize_sse = bool(target.get("normalizeSse"))
                status, _ = await _write_response(
                    writer, resp, stats=stats, write_state=write_state,
                    log_sse=(target.get("label") == "codebuddy" or _normalize_sse),
                    _label=label,
                    normalize_sse=_normalize_sse,
                    normalize_finish_reason=bool(target.get("normalizeFinishReason", True)),
                )
                if _req_model:
                    _bump_model_stats(label, _req_model, "ok" if (status or 0) < 400 else "err")
                if status and status >= 400:
                    logger.warning(f"[{label}] stream returned HTTP {status}")
                return

            # 非流式：先读 body，再判断是否要翻译 429
            resp_body = await resp.aread()
            body_text = resp_body.decode("utf-8", errors="replace")
            status = resp.status_code

            # ── 检测"上游 200 但 body 嵌错误码"的伪装成功响应 ──
            # 仅当上游原始状态码为 200 时才判定；非 200 时 body 里的 code 不覆盖真实状态码
            if status == 200:
                try:
                    parsed = json.loads(body_text)
                    if (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("code"), int)
                        and 400 <= parsed["code"] <= 599
                        and isinstance(parsed.get("message"), str)
                        and "choices" not in parsed
                        and "object" not in parsed
                    ):
                        effective_status = parsed["code"]
                        logger.warning(f"[{label}] upstream 200 with embedded error code {effective_status}: {parsed.get('message')[:200]}")
                        await _write_response_with_status_override(writer, resp, effective_status, stats=stats)
                        return
                except Exception:
                    # JSON 解析失败或不符合错误信封结构 → 走正常透传逻辑
                    pass

            if status >= 400:
                logger.warning(f"[{label}] HTTP {status}: {body_text[:300]}")

            # ── Copilot /responses 非流式响应转换回 chat.completions 格式 ──
            # 成功(2xx)时上游返回 Responses API 结构（output[]），需转回 chat 结构；
            # 错误(4xx/5xx)时上游错误本身就是 OpenAI error 格式，直接透传。
            if _use_responses and status < 400:
                try:
                    upstream_json = json.loads(body_text)
                    chat_body = _copilot_responses_to_chat_body(upstream_json, str(_req_model))
                    payload = json.dumps(chat_body, ensure_ascii=False).encode("utf-8")
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                    writer.write(payload)
                    await writer.drain()
                    writer.close()
                    if _req_model:
                        _bump_model_stats(label, _req_model, "ok")
                    return
                except Exception as e:
                    logger.warning(f"[{label}] responses→chat convert failed: {e}，透传上游原始响应")

            # ── codebuddy 上游只支持流式：非流式请求自动转流式聚合 ──
            # 上游（copilot.tencent.com）对非流式 chat 返回 11101 "Non-stream chat request
            # is currently not supported"。检测到该错误时，用 stream:true 重试并聚合 SSE。
            if (
                status == 400
                and '"code":11101' in body_text
                and target.get("label") == "codebuddy"
            ):
                # body_json 可能为 None（passthrough handler 不解析），这里自行解析
                try:
                    _agg_body = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
                except Exception:
                    _agg_body = None
                if _agg_body and not _agg_body.get("stream"):
                    aggregated = await _aggregate_codebuddy_stream(
                        target, upstream_url, fwd_headers, _agg_body, label
                    )
                    if aggregated is not None:
                        if _req_model:
                            _bump_model_stats(label, _req_model, "ok")
                        payload = json.dumps(aggregated, ensure_ascii=False).encode("utf-8")
                        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                        writer.write(payload)
                        await writer.drain()
                        writer.close(); return
                    # 聚合失败：继续走下方错误处理（透传上游 400）

            mapped = _map_upstream_error(body_text)
            if mapped is not None:
                target_status, err_type = mapped
                stats["translated429"] += 1
                if _req_model:
                    _bump_model_stats(label, _req_model, "translated429")
                logger.info(f"[{label}] translated HTTP {status} → {target_status} ({err_type})")
                err_payload = json.dumps({
                    "error": {
                        "type": err_type,
                        "message": "Upstream temporarily over capacity.",
                        "original_status": resp.status_code,
                    }
                })
                writer.write(
                    f"HTTP/1.1 {target_status} {'Too Many Requests' if target_status == 429 else 'Error'}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Retry-After: {_VENDOR_RETRY_AFTER}\r\n"
                    f"Content-Length: {len(err_payload.encode())}\r\n"
                    f"\r\n{err_payload}".encode()
                )
                await writer.drain()
                writer.close()
            else:
                if _req_model:
                    _bump_model_stats(label, _req_model, "ok" if resp.status_code < 400 else "err")
                await _write_response(writer, resp, stats=stats, write_state=write_state)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as exc:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] upstream connect failed")
        if write_state.get("headers_sent"):
            logger.warning(f"[{label}] stream aborted mid-transfer after headers sent, closing connection: {exc}")
            try:
                writer.close()
            except Exception:
                pass
        else:
            try:
                await _write_error_response(writer, 502, f"Upstream connection failed for {label}")
            except Exception:
                pass
    except httpx.ReadTimeout as exc:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] upstream read timeout")
        if write_state.get("headers_sent"):
            logger.warning(f"[{label}] stream aborted mid-transfer after headers sent, closing connection: {exc}")
            try:
                writer.close()
            except Exception:
                pass
        else:
            try:
                await _write_error_response(writer, 504, f"Upstream read timeout for {label}")
            except Exception:
                pass
    except httpx.RemoteProtocolError as exc:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] upstream protocol error")
        if write_state.get("headers_sent"):
            logger.warning(f"[{label}] stream aborted mid-transfer after headers sent, closing connection: {exc}")
            try:
                writer.close()
            except Exception:
                pass
        else:
            try:
                await _write_error_response(writer, 502, f"Upstream protocol error for {label}")
            except Exception:
                pass
    except (ConnectionResetError, RuntimeError):
        # 客户端提前断开（写阶段异常，如 RuntimeError: unable to perform operation on <TCPTransport closed=True>）
        # 此异常发生在写入阶段，headers 已提交或正在提交，二次写无意义 → 仅记录 + 关闭连接
        logger.warning(f"[{label}] client disconnected mid-transfer, closing connection", exc_info=True)
        try:
            writer.close()
        except Exception:
            pass
    except Exception:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] target proxy exception")
        if write_state.get("headers_sent"):
            logger.warning(f"[{label}] stream aborted mid-transfer after headers sent, closing connection")
            try:
                writer.close()
            except Exception:
                pass
        else:
            try:
                await _write_error_response(writer, 503, f"Proxy error for {label}")
            except Exception:
                pass


async def _vendor_server(host, port, target):
    server = await asyncio.start_server(
        lambda r, w: _handle_target_request(r, w, target),
        host=host, port=port,
    )
    print(f"🔀 [{target['label']}] 0.0.0.0:{port} -> {target.get('targetProtocol','https')}://{target['targetHost']}:{target.get('targetPort', 443)}")
    return server


# ─── Provider 策略（开闭原则：新增 provider 只需在此注册） ───

def _default_provider(req, litellm_req, _orig):
    """标准 OpenAI"""
    litellm_req["api_key"] = OPENAI_API_KEY
    if OPENAI_BASE_URL:
        litellm_req["api_base"] = OPENAI_BASE_URL
        logger.debug(f"OpenAI: base={OPENAI_BASE_URL}")
    else:
        logger.debug(f"OpenAI: default")
    return None  # 继续走 LiteLLM

def _qclaw_provider(req, litellm_req, orig):
    """QClaw 上游直连（OpenAI 兼容接口）"""
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

def _anthropic_provider(req, litellm_req, _orig):
    """Anthropic / 自定义 Anthropic API"""
    litellm_req["api_key"] = ANTHROPIC_API_KEY
    if ANTHROPIC_BASE_URL:
        litellm_req["api_base"] = ANTHROPIC_BASE_URL
        logger.debug(f"Anthropic: base={ANTHROPIC_BASE_URL}")
    else:
        logger.debug(f"Anthropic: default")
    return None  # 继续走 LiteLLM

def _gemini_provider(req, litellm_req, orig):
    """Gemini 原生 API — 走 LiteLLM（gemini/ 前缀），框架内置 thoughtSignature 处理"""
    litellm_req["api_key"] = os.environ.get("GEMINI_API_KEY", "")
    # 不修改 req.model，让 LiteLLM 的 gemini/ 前缀路由正常工作
    # 模型名在非流式响应中由 convert_litellm_to_anthropic 后的 original_model 还原
    logger.debug(f"☀️ Gemini via LiteLLM: → {litellm_req.get('model')}")
    return None  # LiteLLM 处理剩余流程

def _copilot_model_name(anthropic_model: str) -> str:
    """把 Anthropic 模型名映射到 Copilot 企业可用模型名"""
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

_PROVIDER_STRATEGIES = {
    "openai": _default_provider,
    "qclaw": _qclaw_provider,

    "anthropic": _anthropic_provider,
    "gemini": _gemini_provider,
    "gemini-openai": _gemini_provider,
    "copilot": _copilot_provider,
}

def _map_model_name(model: str) -> str:
    """把任意模型名按当前 PREFERRED_PROVIDER 映射到 LiteLLM 可用的带前缀名称。
    copilot provider 请用 _copilot_model_name() 代替。"""
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

# List of OpenAI models
OPENAI_MODELS = [
    "o3-mini",
    "o1",
    "o1-mini",
    "o1-pro",
    "gpt-4.5-preview",
    "gpt-4o",
    "gpt-4o-audio-preview",
    "chatgpt-4o-latest",
    "gpt-4o-mini",
    "gpt-4o-mini-audio-preview",
    "gpt-4.1",  # Added default big model
    "gpt-4.1-mini",  # Added default small model
]

# List of Gemini models
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]


# Helper function to clean schema for Gemini
def clean_gemini_schema(schema: Any) -> Any:
    """Recursively removes unsupported fields from a JSON schema for Gemini."""
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


# Models for Anthropic API requests
class ContentBlockText(BaseModel):
    type: Literal["text"]
    text: str


class ContentBlockThinking(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["thinking"]
    thinking: str
    signature: Optional[str] = None


class ContentBlockImage(BaseModel):
    type: Literal["image"]
    source: Dict[str, Any]


class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]


class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any], List[Any], Any]


class SystemContent(BaseModel):
    type: Literal["text"]
    text: str


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[
        str,
        List[
            Union[
                ContentBlockText,
                ContentBlockThinking,
                ContentBlockImage,
                ContentBlockToolUse,
                ContentBlockToolResult,
            ]
        ],
    ]


class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any] = {}
    type: Optional[str] = None
    max_uses: Optional[int] = None


class ThinkingConfig(BaseModel):
    model_config = {"extra": "allow"}
    enabled: bool = True
    type: Optional[str] = None
    budget_tokens: Optional[int] = None

    @model_validator(mode="after")
    def normalize_thinking_config(cls, m):
        # 客户端可能发 {"type": "adaptive"} 或 {"type": "enabled", "budget_tokens": N}
        # 统一转成 enabled bool
        if m.type == "adaptive":
            m.enabled = True
        elif m.type == "enabled":
            m.enabled = True
        elif m.type == "disabled":
            m.enabled = False
        return m


class MessagesRequest(BaseModel):
    model_config = {"extra": "allow"}  # Allow extra fields from Claude Code
    model: str
    max_tokens: int
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[ThinkingConfig] = None
    original_model: Optional[str] = None  # Will store the original model name
    cache_control: Optional[Dict[str, Any]] = None

    @field_validator("model")
    def validate_model_field(cls, v, info):  # Renamed to avoid conflict
        original_model = v
        new_model = v  # Default to original value

        logger.debug(
            f"📋 MODEL VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}'"
        )

        # Remove provider prefixes for easier matching
        clean_v = v
        if clean_v.startswith("anthropic/"):
            clean_v = clean_v[10:]
        elif clean_v.startswith("openai/"):
            clean_v = clean_v[7:]
        elif clean_v.startswith("gemini/"):
            clean_v = clean_v[7:]
        elif clean_v.startswith("qclaw/"):
            clean_v = clean_v[6:]

        # --- Mapping Logic --- START ---
        mapped = False
        if PREFERRED_PROVIDER == "anthropic":
            # 也走模型映射：sonnet/haiku → BIG/SMALL_MODEL
            if "haiku" in clean_v.lower():
                new_model = f"anthropic/{SMALL_MODEL}"
                mapped = True
            elif "sonnet" in clean_v.lower():
                new_model = f"anthropic/{MEDIUM_MODEL}"
                mapped = True
            else:
                new_model = f"anthropic/{clean_v}"
                mapped = True

        # Map Haiku to SMALL_MODEL based on provider preference
        elif "haiku" in clean_v.lower():
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{SMALL_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = SMALL_MODEL
            else:
                new_model = f"openai/{SMALL_MODEL}"
            mapped = True

        # Map Sonnet to MEDIUM_MODEL (3-tier: Opus>Sonnet>Haiku)
        elif "sonnet" in clean_v.lower():
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{MEDIUM_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = MEDIUM_MODEL
            else:
                new_model = f"openai/{MEDIUM_MODEL}"
            mapped = True

        # Map Opus to BIG_MODEL
        elif "opus" in clean_v.lower():
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{BIG_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = BIG_MODEL
            else:
                new_model = f"openai/{BIG_MODEL}"
            mapped = True

        # Add prefixes to non-mapped models if they match known lists
        elif not mapped:
            if clean_v in GEMINI_MODELS and not v.startswith("gemini/"):
                new_model = f"gemini/{clean_v}"
                mapped = True
            elif clean_v in OPENAI_MODELS and not v.startswith("openai/"):
                new_model = f"openai/{clean_v}"
                mapped = True
        # --- Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 MODEL MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
            # If no mapping occurred and no prefix exists, log warning or decide default
            if not v.startswith(("openai/", "gemini/", "anthropic/")):
                logger.warning(
                    f"⚠️ No prefix or mapping rule for model: '{original_model}'. Using as is."
                )
            new_model = v  # Ensure we return the original if no rule applied

        # Store the original model in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values["original_model"] = original_model

        return new_model


class TokenCountRequest(BaseModel):
    model: str
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    tools: Optional[List[Tool]] = None
    thinking: Optional[ThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
    original_model: Optional[str] = None  # Will store the original model name

    @field_validator("model")
    def validate_model_token_count(cls, v, info):  # Renamed to avoid conflict
        # Use the same logic as MessagesRequest validator
        # NOTE: Pydantic validators might not share state easily if not class methods
        # Re-implementing the logic here for clarity, could be refactored
        original_model = v
        new_model = v  # Default to original value

        logger.debug(
            f"📋 TOKEN COUNT VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}'"
        )

        # Remove provider prefixes for easier matching
        clean_v = v
        if clean_v.startswith("anthropic/"):
            clean_v = clean_v[10:]
        elif clean_v.startswith("openai/"):
            clean_v = clean_v[7:]
        elif clean_v.startswith("gemini/"):
            clean_v = clean_v[7:]
        elif clean_v.startswith("qclaw/"):
            clean_v = clean_v[6:]

        # --- Mapping Logic --- START ---
        mapped = False
        if PREFERRED_PROVIDER == "anthropic":
            if "haiku" in clean_v.lower():
                new_model = f"anthropic/{SMALL_MODEL}"
                mapped = True
            elif "sonnet" in clean_v.lower():
                new_model = f"anthropic/{BIG_MODEL}"
                mapped = True
            else:
                new_model = f"anthropic/{clean_v}"
                mapped = True

        # Map Haiku to SMALL_MODEL based on provider preference
        elif "haiku" in clean_v.lower():
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{SMALL_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = SMALL_MODEL
            else:
                new_model = f"openai/{SMALL_MODEL}"
            mapped = True

        # Map Opus to BIG_MODEL
        elif "opus" in clean_v.lower():
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{BIG_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = BIG_MODEL
            else:
                new_model = f"openai/{BIG_MODEL}"
            mapped = True

        # Default: map everything else (Sonnet, unknown) to MEDIUM_MODEL
        elif not mapped:
            if PREFERRED_PROVIDER in ("gemini", "gemini-openai"):
                new_model = f"gemini/{MEDIUM_MODEL}"
            elif PREFERRED_PROVIDER in ("qclaw", "copilot"):
                new_model = MEDIUM_MODEL
            else:
                new_model = f"openai/{MEDIUM_MODEL}"
            mapped = True
        # --- Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 TOKEN COUNT MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
            if not v.startswith(("openai/", "gemini/", "anthropic/")):
                logger.warning(
                    f"⚠️ No prefix or mapping rule for token count model: '{original_model}'. Using as is."
                )
            new_model = v  # Ensure we return the original if no rule applied

        # Store the original model in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values["original_model"] = original_model

        return new_model


class TokenCountResponse(BaseModel):
    input_tokens: int


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: List[Union[ContentBlockText, ContentBlockToolUse, ContentBlockThinking]]
    type: Literal["message"] = "message"
    stop_reason: Optional[
        Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]
    ] = None
    stop_sequence: Optional[str] = None
    usage: Usage


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    method = request.method
    path = request.url.path

    # ── 8081 自身统计：/v1/messages 请求数 + 模型级统计 ──
    if path == "/v1/messages" and method == "POST":
        req_model = "unknown"
        try:
            body = await request.body()
            req_model = json.loads(body.decode("utf-8")).get("model", "unknown") if body else "unknown"
        except Exception:
            pass
        _ANTHROPIC_STATS["totalRequests"] += 1

    response = await call_next(request)

    if path == "/v1/messages" and method == "POST":
        outcome = "ok" if response.status_code < 400 else "err"
        _ANTHROPIC_STATS["passthroughOk" if outcome == "ok" else "passthroughError"] += 1
        _bump_model_stats("anthropic", req_model, outcome)

    if response.status_code >= 400:
        elapsed = time.time() - start
        logger.warning(
            f"⬆️ {method} {path} HTTP {response.status_code} ⏱️{elapsed:.1f}s"
        )

    return response


# Not using validation function as we're using the environment API key


def parse_tool_result_content(content):
    """Helper function to properly parse and normalize tool result content."""
    if content is None:
        return "No content provided"

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        result = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                result += item.get("text", "") + "\n"
            elif isinstance(item, str):
                result += item + "\n"
            elif isinstance(item, dict):
                if "text" in item:
                    result += item.get("text", "") + "\n"
                else:
                    try:
                        result += json.dumps(item) + "\n"
                    except:
                        result += str(item) + "\n"
                try:
                    result += str(item) + "\n"
                except:
                    result += "Unparseable content\n"
        return result.strip()

    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "")
        try:
            return json.dumps(content)
        except:
            return str(content)

    # Fallback for any other type
    try:
        return str(content)
    except:
        return "Unparseable content"


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


def _sanitize_for_log(obj, max_str_len: int = 2000):
    """Recursively sanitize objects so they can be safely logged as JSON."""
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_log(v, max_str_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_log(v, max_str_len) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str) and len(obj) > max_str_len:
            return obj[:max_str_len] + "...(truncated)"
        return obj
    try:
        s = str(obj)
        return s[:max_str_len] + ("...(truncated)" if len(s) > max_str_len else "")
    except Exception:
        return "<unserializable>"


def _request_id_from_headers(request: Optional[Request] = None) -> str:
    if request is None:
        return f"req_{uuid.uuid4().hex[:12]}"
    rid = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    return rid or f"req_{uuid.uuid4().hex[:12]}"


def _log_exception(event: str, exc: Exception, context: Optional[Dict[str, Any]] = None):
    import traceback

    details = {
        "event": event,
        "provider": PREFERRED_PROVIDER,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exc(),
        "context": context or {},
    }

    # Capture common exception attributes (LiteLLM/httpx/etc.)
    for attr in ("status_code", "message", "llm_provider", "model", "response"):
        if hasattr(exc, attr):
            details[attr] = getattr(exc, attr)

    if hasattr(exc, "__dict__"):
        extra = {}
        for k, v in exc.__dict__.items():
            if k not in ("args", "__traceback__"):
                extra[k] = v
        if extra:
            details["exception_attrs"] = extra

    logger.error(
        f"ERROR_CONTEXT {json.dumps(_sanitize_for_log(details), ensure_ascii=False)}"
    )


def convert_anthropic_to_litellm(anthropic_request: MessagesRequest) -> Dict[str, Any]:
    """Convert Anthropic API request format to LiteLLM format (which follows OpenAI)."""
    # LiteLLM already handles Anthropic models when using the format model="anthropic/claude-3-opus-20240229"
    # So we just need to convert our Pydantic model to a dict in the expected format

    messages = []

    # Add system message if present
    if anthropic_request.system:
        # Handle different formats of system messages
        if isinstance(anthropic_request.system, str):
            # Simple string format
            messages.append({"role": "system", "content": anthropic_request.system})
        elif isinstance(anthropic_request.system, list):
            # List of content blocks
            system_text = ""
            for block in anthropic_request.system:
                if hasattr(block, "type") and block.type == "text":
                    system_text += block.text + "\n\n"
                elif isinstance(block, dict) and block.get("type") == "text":
                    system_text += block.get("text", "") + "\n\n"

            if system_text:
                messages.append({"role": "system", "content": system_text.strip()})

    # Add conversation messages
    for idx, msg in enumerate(anthropic_request.messages):
        content = msg.content
        if isinstance(content, str):
            messages.append({"role": msg.role, "content": content})
        else:
            # Special handling for tool_result in user messages
            # OpenAI format: each tool_result → {"role": "tool", "tool_call_id": "xxx", "content": "yyy"}
            if msg.role == "user" and any(
                block.type == "tool_result"
                for block in content
                if hasattr(block, "type")
            ):
                text_parts = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_parts.append(block.text)
                        elif block.type == "tool_result":
                            tool_id = (
                                block.tool_use_id
                                if hasattr(block, "tool_use_id")
                                else ""
                            )
                            result_content = ""
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    result_content = block.content
                                elif isinstance(block.content, list):
                                    for cb in block.content:
                                        if isinstance(cb, dict) and cb.get("type") == "text":
                                            result_content += cb.get("text", "") + "\n"
                                        elif hasattr(cb, "type") and cb.type == "text":
                                            result_content += cb.text + "\n"
                                        else:
                                            result_content += str(cb) + "\n"
                                else:
                                    result_content = str(block.content)
                            # OpenAI standard: tool role message
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "content": result_content.strip() or "Tool executed successfully",
                            })
                # Any remaining text goes as a separate user message
                if text_parts:
                    messages.append({"role": "user", "content": "\n".join(text_parts)})
            # Regular handling for other message types（与 tool_result 处理同级）
            elif msg.role == "assistant" and any(
                hasattr(b, "type") and b.type == "tool_use"
                for b in content
            ):
                # Assistant with tool_use — convert to OpenAI tool_calls format
                tool_calls = []
                text_content = ""
                sigs_for_provider = []  # LiteLLM Gemini 需要消息级别的 thought_signatures
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_content += block.text
                        elif block.type == "tool_use":
                            tc = {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": json.dumps(block.input) if block.input else "{}"
                                }
                            }
                            # 注入签名到 tool_call 本身（备用兼容）
                            sig = _thought_signatures.get(block.id)
                            if sig:
                                tc["function"]["provider_specific_fields"] = {"thought_signature": sig}
                                sigs_for_provider.append(sig)
                            tool_calls.append(tc)
                msg_entry = {"role": "assistant"}
                if text_content:
                    msg_entry["content"] = text_content
                else:
                    msg_entry["content"] = None
                if tool_calls:
                    msg_entry["tool_calls"] = tool_calls
                # LiteLLM Gemini handler 从消息级别 provider_specific_fields 读取签名
                if sigs_for_provider:
                    msg_entry["provider_specific_fields"] = {"thought_signatures": sigs_for_provider}
                messages.append(msg_entry)
            else:
                processed_content = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            processed_content.append(
                                {"type": "text", "text": block.text}
                            )
                        elif block.type == "image":
                            # Convert Anthropic image source → OpenAI image_url format
                            source = block.source if isinstance(block.source, dict) else {}
                            if source.get("type") == "base64":
                                img_url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                            else:
                                img_url = source.get("url", "")
                            processed_content.append(
                                {"type": "image_url", "image_url": {"url": img_url}}
                            )
                        elif block.type == "tool_use":
                            # Handle tool use blocks if needed
                            processed_content.append(
                                {
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                }
                            )
                    elif block.type == "tool_result":
                        # Handle different formats of tool result content
                        processed_content_block = {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id
                            if hasattr(block, "tool_use_id")
                            else "",
                        }

                        # Process the content field properly
                        if hasattr(block, "content"):
                            if isinstance(block.content, str):
                                # If it's a simple string, create a text block for it
                                processed_content_block["content"] = [
                                    {"type": "text", "text": block.content}
                                ]
                            elif isinstance(block.content, list):
                                # If it's already a list of blocks, keep it
                                processed_content_block["content"] = block.content
                            else:
                                # Default fallback
                                processed_content_block["content"] = [
                                    {"type": "text", "text": str(block.content)}
                                ]
                        else:
                            # Default empty content
                            processed_content_block["content"] = [
                                {"type": "text", "text": ""}
                            ]

                        processed_content.append(processed_content_block)

                messages.append({"role": msg.role, "content": processed_content})

    copilot_target_model = ""
    if PREFERRED_PROVIDER == "copilot":
        source_model = anthropic_request.original_model or anthropic_request.model
        copilot_target_model = _copilot_model_name(source_model)

    copilot_thinking_enabled = bool(
        PREFERRED_PROVIDER == "copilot"
        and anthropic_request.thinking
        and anthropic_request.thinking.enabled
    )

    # Cap max_tokens for OpenAI/Gemini/Copilot models
    # QClaw 链路不受此限制，会在 _qclaw_provider 中恢复原始值
    max_tokens = anthropic_request.max_tokens
    if PREFERRED_PROVIDER == "copilot":
        max_tokens = min(max_tokens, 64000)
        # Copilot + thinking 常见中途截断，给一个保底 completion budget
        if copilot_thinking_enabled:
            max_tokens = max(max_tokens, 8192)
        logger.debug(
            f"Capping max_tokens to 64000 for Copilot model (original value: {anthropic_request.max_tokens})"
        )
    elif anthropic_request.model.startswith("openai/") or anthropic_request.model.startswith("gemini/"):
        max_tokens = min(max_tokens, 16384)
        logger.debug(
            f"Capping max_tokens to 16384 for OpenAI/Gemini model (original value: {anthropic_request.max_tokens})"
        )

    # Create LiteLLM request dict
    litellm_request = {
        "model": anthropic_request.model,  # it understands "anthropic/claude-x" format
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "_original_max_tokens": anthropic_request.max_tokens,  # 保留原始值供 QClaw 使用
        "stream": anthropic_request.stream,
    }
    if anthropic_request.temperature is not None:
        litellm_request["temperature"] = anthropic_request.temperature

    # thinking 参数仅原生 Anthropic 支持，DeepSeek Anthropic 兼容接口不认
    # 保持为空，让模型自行决定推理深度
    if copilot_thinking_enabled:
        # Anthropic thinking -> OpenAI compatible reasoning effort
        litellm_request["reasoning"] = {"effort": "high"}
        logger.debug(
            f"Copilot thinking enabled: translated to reasoning.effort=high target_model={copilot_target_model or 'unknown'}"
        )

    # Add optional parameters if present
    if anthropic_request.stop_sequences:
        litellm_request["stop"] = anthropic_request.stop_sequences

    if anthropic_request.top_p:
        litellm_request["top_p"] = anthropic_request.top_p

    if anthropic_request.top_k and PREFERRED_PROVIDER in ("anthropic", "gemini", "gemini-openai"):
        litellm_request["top_k"] = anthropic_request.top_k

    if anthropic_request.cache_control:
        litellm_request["cache_control"] = anthropic_request.cache_control

    # Convert tools to OpenAI format
    if anthropic_request.tools:
        openai_tools = []
        is_gemini_model = anthropic_request.model.startswith("gemini/")

        for tool in anthropic_request.tools:
            # Convert to dict if it's a pydantic model
            if hasattr(tool, "dict"):
                tool_dict = tool.dict()
                # Ensure tool_dict is a dictionary, handle potential errors if 'tool' isn't dict-like
                try:
                    tool_dict = dict(tool) if not isinstance(tool, dict) else tool
                except (TypeError, ValueError):
                    logger.error(f"Could not convert tool to dict: {tool}")
                    continue  # Skip this tool if conversion fails

            # Clean the schema if targeting a Gemini model
            input_schema = tool_dict.get("input_schema", {})
            if is_gemini_model:
                logger.debug(
                    f"Cleaning schema for Gemini tool: {tool_dict.get('name')}"
                )
                input_schema = clean_gemini_schema(input_schema)

            # Create OpenAI-compatible function tool
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool_dict["name"],
                    "description": tool_dict.get("description", ""),
                    "parameters": input_schema,  # Use potentially cleaned schema
                },
            }
            openai_tools.append(openai_tool)

        litellm_request["tools"] = openai_tools

    # Convert tool_choice to OpenAI format if present
    if anthropic_request.tool_choice:
        if hasattr(anthropic_request.tool_choice, "dict"):
            tool_choice_dict = anthropic_request.tool_choice.dict()
        else:
            tool_choice_dict = anthropic_request.tool_choice

        # Handle Anthropic's tool_choice format
        choice_type = tool_choice_dict.get("type")
        if choice_type == "auto":
            litellm_request["tool_choice"] = "auto"
        elif choice_type == "any":
            litellm_request["tool_choice"] = "any"
        elif choice_type == "tool" and "name" in tool_choice_dict:
            litellm_request["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice_dict["name"]},
            }
        else:
            # Default to auto if we can't determine
            litellm_request["tool_choice"] = "auto"

    return litellm_request


def convert_litellm_to_anthropic(
    litellm_response: Union[Dict[str, Any], Any], original_request: MessagesRequest
) -> MessagesResponse:
    """Convert LiteLLM (OpenAI format) response to Anthropic API response format."""

    # Enhanced response extraction with better error handling
    try:
        # Get the clean model name to check capabilities
        clean_model = original_request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/") :]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/") :]

        # Check if this is a Claude model (which supports content blocks)
        is_claude_model = clean_model.startswith("claude-")

        # Handle ModelResponse object from LiteLLM
        if hasattr(litellm_response, "choices") and hasattr(litellm_response, "usage"):
            # Extract data from ModelResponse object directly
            choices = litellm_response.choices or []
            # 防护：Copilot 在 max_tokens 过小时可能返回 choices:[]
            if not choices:
                choices = []
                message = None
            else:
                message = choices[0].message if len(choices) > 0 else None
            content_text = (
                message.content if message and hasattr(message, "content") else ""
            )
            tool_calls = (
                message.tool_calls
                if message and hasattr(message, "tool_calls")
                else None
            )
            finish_reason = (
                choices[0].finish_reason if choices and len(choices) > 0 else "stop"
            )
            usage_info = litellm_response.usage
            response_id = getattr(litellm_response, "id", f"msg_{uuid.uuid4()}")
        else:
            # For backward compatibility - handle dict responses
            # If response is a dict, use it, otherwise try to convert to dict
            try:
                response_dict = (
                    litellm_response
                    if isinstance(litellm_response, dict)
                    else litellm_response.dict()
                )
            except AttributeError:
                # If .dict() fails, try to use model_dump or __dict__
                try:
                    response_dict = (
                        litellm_response.model_dump()
                        if hasattr(litellm_response, "model_dump")
                        else litellm_response.__dict__
                    )
                except AttributeError:
                    # Fallback - manually extract attributes
                    response_dict = {
                        "id": getattr(litellm_response, "id", f"msg_{uuid.uuid4()}"),
                        "choices": getattr(litellm_response, "choices", [{}]),
                        "usage": getattr(litellm_response, "usage", {}),
                    }

            # Extract the content from the response dict
            choices = response_dict.get("choices", [{}])
            message = (
                choices[0].get("message", {}) if choices and len(choices) > 0 else {}
            )
            content_text = message.get("content", "")
            tool_calls = message.get("tool_calls", None)
            finish_reason = (
                choices[0].get("finish_reason", "stop")
                if choices and len(choices) > 0
                else "stop"
            )
            usage_info = response_dict.get("usage", {})
            response_id = response_dict.get("id", f"msg_{uuid.uuid4()}")

        # Create content list for Anthropic format
        content = []

        # ── 处理 reasoning_content（DeepSeek 等模型的思考过程）──
        # DeepSeek 返回的 reasoning_content 是独立字段，需要转为 Anthropic 的 thinking block
        reasoning_text = None
        if hasattr(message, "model_extra") and isinstance(message.model_extra, dict):
            reasoning_text = message.model_extra.get("reasoning_content") or message.model_extra.get("reasoning_text")
        if reasoning_text is None:
            reasoning_text = getattr(message, "reasoning_content", None) or getattr(message, "reasoning_text", None)
        if reasoning_text is None and isinstance(message, dict):
            reasoning_text = message.get("reasoning_content") or message.get("reasoning_text")

        # 如果请求开启了 thinking，将 reasoning_content 放入 thinking block
        upstream_thinking = getattr(original_request, "thinking", None)
        if reasoning_text and upstream_thinking and getattr(upstream_thinking, "enabled", False):
            content.append({"type": "thinking", "thinking": reasoning_text})
        elif reasoning_text:
            # 请求未开启 thinking，但也不要丢弃——作为文本保留
            content.append({"type": "text", "text": f"<thinking>{reasoning_text}</thinking>"})

        # Add text content block if present (text might be None or empty for pure tool call responses)
        # 过滤掉已经被 reasoning 处理的重复内容
        if content_text is not None and content_text != "":
            # 如果 text 内容和 reasoning 完全相同（有些模型会重复），跳过
            if reasoning_text and content_text.strip() == reasoning_text.strip():
                pass
            else:
                content.append({"type": "text", "text": content_text})

        # Add tool calls if present (tool_use in Anthropic format)
        # For ALL models, not just Claude models - convert tool_calls to tool_use blocks
        if tool_calls:
            logger.debug(f"Processing tool calls: {tool_calls}")

            # Convert to list if it's not already
            if not isinstance(tool_calls, list):
                tool_calls = [tool_calls]

            for idx, tool_call in enumerate(tool_calls):
                logger.debug(f"Processing tool call {idx}: {tool_call}")

                # Extract function data based on whether it's a dict or object
                if isinstance(tool_call, dict):
                    function = tool_call.get("function", {})
                    tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                    name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                else:
                    function = getattr(tool_call, "function", None)
                    tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                    name = getattr(function, "name", "") if function else ""
                    arguments = (
                        getattr(function, "arguments", "{}") if function else "{}"
                    )

                # Convert string arguments to dict if needed
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        fixed_suffix = _close_json_fragment(arguments)
                        if fixed_suffix:
                            try:
                                arguments = json.loads(arguments + fixed_suffix)
                            except json.JSONDecodeError:
                                logger.warning(
                                    f"Failed to parse tool arguments as JSON: {arguments}"
                                )
                                arguments = {"raw": arguments}
                        else:
                            logger.warning(
                                f"Failed to parse tool arguments as JSON: {arguments}"
                            )
                            arguments = {"raw": arguments}

                # 提取 Gemini thought_signature（兼容两种来源）
                sig = None
                # 来源1：OpenAI 兼容端点 extra_content.google.thought_signature
                if isinstance(tool_call, dict):
                    extra = tool_call.get("extra_content", {})
                    if isinstance(extra, dict):
                        sig = extra.get("google", {}).get("thought_signature")
                else:
                    extra = getattr(tool_call, "extra_content", None)
                    if isinstance(extra, dict):
                        sig = extra.get("google", {}).get("thought_signature")
                # 来源2：LiteLLM Gemini handler 的 provider_specific_fields
                if not sig and function:
                    if isinstance(function, dict):
                        psf = function.get("provider_specific_fields", {})
                    else:
                        psf = getattr(function, "provider_specific_fields", {})
                    if isinstance(psf, dict):
                        sig = psf.get("thought_signature")
                # 来源3：消息级别的 provider_specific_fields.thought_signatures
                if not sig and hasattr(message, "provider_specific_fields"):
                    psf = getattr(message, "provider_specific_fields", {})
                    if isinstance(psf, dict):
                        sig_list = psf.get("thought_signatures", [])
                        if isinstance(sig_list, list) and idx < len(sig_list):
                            sig = sig_list[idx]
                if sig:
                    _thought_signatures[tool_id] = sig
                    logger.debug(f"💭 Saved thought_signature for tool {tool_id}")

                logger.debug(
                    f"Adding tool_use block: id={tool_id}, name={name}, input={arguments}"
                )

                content.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": arguments,
                    }
                )

        # Get usage information - extract values safely from object or dict
        if isinstance(usage_info, dict):
            prompt_tokens = usage_info.get("prompt_tokens", 0) or 0
            completion_tokens = usage_info.get("completion_tokens", 0) or 0
        else:
            prompt_tokens = getattr(usage_info, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage_info, "completion_tokens", 0) or 0

        # QClaw 网关不返回 usage → 用 tiktoken 本地估算
        if prompt_tokens == 0 or completion_tokens == 0:
            try:
                if prompt_tokens == 0:
                    prompt_tokens = _estimate_messages_tokens(
                        getattr(original_request, "messages", []) or [],
                        original_request.model,
                        getattr(original_request, "system", None),
                        getattr(original_request, "tools", None),
                    )
                if completion_tokens == 0:
                    out_text = _extract_text_from_content(content)
                    completion_tokens = _estimate_text_tokens(out_text, original_request.model)
            except Exception as _e:
                logger.debug(f"tiktoken estimate failed in convert_litellm_to_anthropic: {_e}")

        # Map OpenAI finish_reason to Anthropic stop_reason
        stop_reason = None
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"  # Default

        # Make sure content is never empty
        if not content:
            content.append({"type": "text", "text": ""})

        # Create Anthropic-style response
        anthropic_response = MessagesResponse(
            id=response_id,
            model=original_request.model,
            role="assistant",
            content=content,
            stop_reason=stop_reason,
            stop_sequence=None,
            usage=Usage(input_tokens=prompt_tokens, output_tokens=completion_tokens),
        )

        return anthropic_response

    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        error_message = (
            f"Error converting response: {str(e)}\n\nFull traceback:\n{error_traceback}"
        )
        logger.error(error_message)

        # In case of any error, create a fallback response
        return MessagesResponse(
            id=f"msg_{uuid.uuid4()}",
            model=original_request.model,
            role="assistant",
            content=[
                {
                    "type": "text",
                    "text": f"Error converting response: {str(e)}. Please check server logs.",
                }
            ],
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0),
        )


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI 兼容端点：/v1/chat/completions  → 统一走 LiteLLM
# ══════════════════════════════════════════════════════════════════════════════

async def _litellm_oai_stream(response_generator):
    """把 LiteLLM 流式输出转成 OpenAI SSE 格式（bytes）"""
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


@app.post("/v1/chat/completions")
async def openai_chat_completions(raw_request: Request):
    """OpenAI 兼容端点

    透传模式（qclaw / openai / copilot / gemini-openai）：直接转发请求体给后端，不做模型映射或协议转换。
    翻译模式（anthropic / gemini）：通过 LiteLLM 进行格式翻译和模型映射。
    """
    request_id = _request_id_from_headers(raw_request)
    req_model = "unknown"
    mapped_model = "unknown"
    body = {}
    try:
        body = await raw_request.json()
        default_model = COPILOT_MEDIUM_MODEL if PREFERRED_PROVIDER == "copilot" else MEDIUM_MODEL
        req_model = body.get("model", default_model)
        is_stream = body.get("stream", False)

        # ── 透传模式：qclaw / openai / copilot / gemini-openai ──
        _PASSTHROUGH_PROVIDERS = ("qclaw", "openai", "copilot", "gemini-openai")
        if PREFERRED_PROVIDER in _PASSTHROUGH_PROVIDERS:
            passthrough_model = req_model
            # 去掉 provider 前缀（如果有）
            for pfx in ("qclaw/", "openai/", "copilot/", "gemini/"):
                if passthrough_model.startswith(pfx):
                    passthrough_model = passthrough_model[len(pfx):]
                    break

            # ── provider 特殊处理 ──
            headers = {"Content-Type": "application/json"}
            url = ""

            if PREFERRED_PROVIDER in ("qclaw",):
                body["model"] = passthrough_model
                # QClaw 网关要求必须有 system message
                msgs = body.get("messages", [])
                if not any(m.get("role") == "system" for m in msgs):
                    msgs.insert(0, {"role": "system", "content": "You are Claude, a helpful AI assistant."})
                    body["messages"] = msgs
                # 清理非标准字段，避免客户端透传的 Anthropic 专属参数导致上游 400
                body = _clean_qclaw_body(body)
                logger.debug(f"🐙 QClaw passthrough: body keys={list(body.keys())} model={body.get('model')}")
                headers["Authorization"] = f"Bearer {QCLAW_API_KEY}"
                headers["User-Agent"] = "OpenAI/JS 6.39.1"  # 上游拒绝 python-httpx 默认 UA
                _qclaw_base = QCLAW_BASE_URL
                url = f"{_qclaw_base}/chat/completions"

            elif PREFERRED_PROVIDER == "openai":
                body["model"] = passthrough_model
                headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
                base = OPENAI_BASE_URL or "https://api.openai.com/v1"
                url = f"{base}/chat/completions"

            elif PREFERRED_PROVIDER == "copilot":
                # copilot 模型映射：haiku/sonnet/opus → COPILOT_*_MODEL
                body["model"] = _copilot_model_name(passthrough_model)
                headers["Authorization"] = f"Bearer {COPILOT_GHE_TOKEN}"
                headers["Copilot-Integration-Id"] = COPILOT_INTEGRATION_ID
                # Copilot 不接受空/None 消息 content
                for msg in body.get("messages", []):
                    c = msg.get("content")
                    if c is None or (isinstance(c, str) and not c.strip()):
                        msg["content"] = "."
                # Copilot 不接受没有 tools 时的 tool_choice
                if body.get("tool_choice") and not body.get("tools"):
                    body.pop("tool_choice")
                url = f"{COPILOT_API_BASE}/chat/completions"

            elif PREFERRED_PROVIDER == "gemini-openai":
                body["model"] = passthrough_model
                headers["Authorization"] = f"Bearer {os.environ.get('GEMINI_API_KEY', '')}"
                gemini_base = os.environ.get(
                    "GEMINI_BASE_URL",
                    "https://generativelanguage.googleapis.com/v1beta/openai",
                )
                url = f"{gemini_base}/chat/completions"

            log_request_beautifully(
                "POST", "/v1/chat/completions", req_model, body["model"],
                len(body.get("messages", [])), len(body.get("tools") or []), 200
            )

            if is_stream:
                async def passthrough_stream():
                    last_err = None
                    for attempt in range(3):
                        if attempt > 0:
                            await asyncio.sleep(0.5)
                        client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False)
                        try:
                            async with client.stream("POST", url, json=body, headers=headers) as resp:
                                if resp.status_code >= 400:
                                    # 读 body 判断是否为可识别的限流错误（数据驱动，见 _VENDOR_ERROR_MAPS）
                                    if resp.status_code == 429 or resp.status_code >= 500:
                                        err_text = await resp.aread()
                                        mapped = _map_upstream_error(err_text.decode("utf-8", "replace"))
                                        if mapped is not None:
                                            # 限流类：不重试，直接发 SSE error 事件让客户端（opencode 等）感知并重试，
                                            # 避免对稳定限流盲目重试 3 次浪费时间。
                                            _st, _et = mapped
                                            ev = {"error": {"type": _et, "message": err_text.decode("utf-8", "replace")[:500]}}
                                            yield json.dumps(ev).encode()
                                            yield b"data: [DONE]\n\n"
                                            return
                                        if resp.status_code >= 500:
                                            last_err = f"upstream {resp.status_code}"
                                            continue
                                        # 429 但非限流特征（罕见）：透传原始响应
                                        yield err_text
                                        yield b"data: [DONE]\n\n"
                                        return
                                    # 其他 4xx（400/401/403 等）：直接透传原始响应流
                                    async for chunk in resp.aiter_bytes():
                                        yield chunk
                                    return
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                                return
                        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                            last_err = str(e)
                            continue
                        finally:
                            await client.aclose()
                    # 所有重试失败
                    yield json.dumps({"error": {"type": "proxy_error", "message": f"{PREFERRED_PROVIDER} upstream unavailable after retries: {last_err}"}}).encode()
                return StreamingResponse(passthrough_stream(), media_type="text/event-stream")
            else:
                last_err = None
                for attempt in range(3):
                    if attempt > 0:
                        await asyncio.sleep(0.5)
                    try:
                        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                            resp = await client.post(url, json=body, headers=headers)
                            if resp.status_code >= 500 and attempt < 2:
                                last_err = f"upstream {resp.status_code}"
                                continue
                            # QClaw 网关不返回 usage → 用 tiktoken 本地估算后注入
                            try:
                                resp_data = resp.json()
                            except Exception:
                                return JSONResponse(content={"error": "invalid upstream JSON"}, status_code=502)
                            if isinstance(resp_data, dict) and not resp_data.get("usage"):
                                try:
                                    est_in = _estimate_messages_tokens(
                                        body.get("messages", []) or [],
                                        body.get("model", req_model),
                                    )
                                    # 抽出响应文本用于估算 output
                                    _choices = resp_data.get("choices") or [{}]
                                    _msg = _choices[0].get("message", {}) if _choices else {}
                                    _out_text = _msg.get("content", "") or ""
                                    if _msg.get("reasoning_content"):
                                        _out_text += _msg.get("reasoning_content")
                                    for _tc in _msg.get("tool_calls", []) or []:
                                        try:
                                            _out_text += json.dumps(_tc.get("function", {}).get("arguments", ""), ensure_ascii=False)
                                        except Exception:
                                            pass
                                    est_out = _estimate_text_tokens(_out_text, body.get("model", req_model))
                                    resp_data["usage"] = {
                                        "prompt_tokens": est_in,
                                        "completion_tokens": est_out,
                                        "total_tokens": est_in + est_out,
                                    }
                                except Exception as _e:
                                    logger.debug(f"tiktoken passthrough estimate failed: {_e}")
                            return JSONResponse(content=resp_data, status_code=resp.status_code)
                    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                        last_err = str(e)
                        continue
                return JSONResponse(
                    content={"error": {"type": "proxy_error", "message": f"{PREFERRED_PROVIDER} upstream unavailable after retries: {last_err}"}},
                    status_code=502,
                )

        # ── 翻译模式：anthropic / gemini — 走 LiteLLM ──
        mapped_model = _map_model_name(req_model)
        litellm_req = dict(body)
        litellm_req["model"] = mapped_model

        # 应用 provider 策略（注入 api_key / api_base / extra_headers 等）
        class _R:
            model = req_model
        strategy = _PROVIDER_STRATEGIES.get(PREFERRED_PROVIDER, _default_provider)
        strategy(_R(), litellm_req, req_model)

        log_request_beautifully(
            "POST", "/v1/chat/completions", req_model, litellm_req["model"],
            len(litellm_req.get("messages", [])), len(litellm_req.get("tools") or []), 200
        )

        async def _do_litellm_call():
            if is_stream:
                litellm_req["stream"] = True
                gen = await litellm.acompletion(**litellm_req)
                return StreamingResponse(_litellm_oai_stream(gen), media_type="text/event-stream")
            else:
                litellm_req.pop("stream", None)
                resp = await litellm.acompletion(**litellm_req)
                return JSONResponse(content=resp.model_dump())

        return await _do_litellm_call()

    except HTTPException:
        raise
    except Exception as e:
        # 上游错误：绕过 litellm，httpx 直连 QClaw 上游重试
        if _is_auth_expired_error(e) and PREFERRED_PROVIDER == "qclaw":
            try:
                logger.info("🔄 /v1/chat/completions error → fallback httpx 直连...")
                await asyncio.sleep(0.5)
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0), trust_env=False) as client:
                    resp = await client.post(
                        f"{QCLAW_BASE_URL}/chat/completions",
                        json=_clean_qclaw_body(litellm_req),
                        headers={
                            "Authorization": f"Bearer {QCLAW_API_KEY}",
                            "Content-Type": "application/json",
                            "User-Agent": "OpenAI/JS 6.39.1",
                        },
                    )
                    if resp.status_code != 200:
                        raise HTTPException(status_code=502, detail=f"QClaw fallback failed: {resp.status_code}")
                    return JSONResponse(content=resp.json())
            except Exception as retry_e:
                _log_exception("openai_chat_completions_fallback_failed", retry_e, {"original_error": str(e)})
                raise HTTPException(status_code=getattr(retry_e, "status_code", 502),
                                     detail=f"QClaw fallback failed: {str(retry_e)}")

        _log_exception(
            "openai_chat_completions_failed",
            e,
            {
                "request_id": request_id,
                "path": str(raw_request.url.path),
                "request_model": req_model,
                "mapped_model": mapped_model,
                "stream": body.get("stream"),
                "message_count": len(body.get("messages") or []),
                "tool_count": len(body.get("tools") or []),
                "has_reasoning": bool(body.get("reasoning")),
                "has_thinking": bool(body.get("thinking")),
                "max_tokens": body.get("max_tokens"),
                "max_completion_tokens": body.get("max_completion_tokens"),
            },
        )
        # 限流/资源耗尽（如 qclaw: ResourceExhausted / Worker local total request limit reached）
        # 转成 HTTP 429 + Retry-After，让下游客户端（opencode 等）自动重试，而非 500 UnknownError。
        if _is_rate_limit_error(e):
            raise HTTPException(
                status_code=429,
                headers={"Retry-After": str(_VENDOR_RETRY_AFTER)},
                detail=str(e),
            )
        raise HTTPException(status_code=500, detail=str(e))


async def handle_streaming(
    response_generator,
    original_request: MessagesRequest,
    original_model_name: str = "",
    request_id: str = "",
):
    """Handle streaming responses from LiteLLM and convert to Anthropic format."""
    try:
        # Send message_start event
        message_id = f"msg_{uuid.uuid4().hex[:24]}"  # Format similar to Anthropic's IDs

        # QClaw 网关不返回 usage，message_start 阶段提前用 tiktoken 估算 input_tokens
        try:
            est_input_tokens = _estimate_messages_tokens(
                getattr(original_request, "messages", []) or [],
                original_model_name or original_request.model,
                getattr(original_request, "system", None),
                getattr(original_request, "tools", None),
            )
        except Exception as _e:
            logger.debug(f"tiktoken input estimate (stream) failed: {_e}")
            est_input_tokens = 0

        message_data = {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": original_model_name or original_request.original_model or original_request.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": est_input_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        }
        yield f"event: message_start\ndata: {json.dumps(message_data)}\n\n"

        # 根据请求是否开启 thinking 决定初始 content block 类型
        upstream_thinking = getattr(original_request, "thinking", None)
        thinking_enabled = upstream_thinking and getattr(upstream_thinking, "enabled", False)
        if thinking_enabled:
            # 先开 thinking block (index 0)
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}})}\n\n"
            thinking_block_started = True
            thinking_block_closed = False
        else:
            # 先开 text block (index 0)
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
            thinking_block_started = False
            thinking_block_closed = True  # 没开 thinking block，标记为已关闭

        # Send a ping to keep the connection alive (Anthropic does this)
        yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"

        tool_index = None
        current_tool_call = None
        tool_content = ""
        accumulated_text = ""  # Track accumulated text content
        text_sent = False  # Track if we've sent any text content
        text_block_closed = False  # Track if text block is closed
        thinking_block_started = False  # Track if thinking content block has been started
        accumulated_reasoning = ""  # Track accumulated reasoning content
        text_block_index = 0  # Track current text block index (0 if no thinking, 1 if after thinking)
        input_tokens = est_input_tokens  # 已用 tiktoken 估算（上游 QClaw 不返回）
        output_tokens = 0
        has_sent_stop_reason = False
        last_tool_index = 0
        openai_to_anthropic_tool_index: Dict[int, int] = {}
        tool_json_buffers: Dict[int, str] = {}

        # Process each chunk
        async for chunk in response_generator:
            try:
                # Check if this is the end of the response with usage data
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    if hasattr(chunk.usage, "prompt_tokens"):
                        input_tokens = chunk.usage.prompt_tokens
                    if hasattr(chunk.usage, "completion_tokens"):
                        output_tokens = chunk.usage.completion_tokens

                # Handle text content
                if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                    choice = chunk.choices[0]

                    # Get the delta from the choice
                    if hasattr(choice, "delta"):
                        delta = choice.delta
                    else:
                        # If no delta, try to get message
                        delta = getattr(choice, "message", {})

                    # Check for finish_reason to know when we're done
                    finish_reason = getattr(choice, "finish_reason", None)

                    # Process reasoning content first (avoid Agent context loss)
                    reasoning_text = None
                    if hasattr(delta, "model_extra") and isinstance(delta.model_extra, dict):
                        reasoning_text = (
                            delta.model_extra.get("reasoning_content")
                            or delta.model_extra.get("reasoning_text")
                        )
                    if reasoning_text is None:
                        reasoning_text = getattr(delta, "reasoning_content", None)
                    if reasoning_text is None:
                        reasoning_text = getattr(delta, "reasoning_text", None)
                    if isinstance(delta, dict) and reasoning_text is None:
                        reasoning_text = delta.get("reasoning_content") or delta.get(
                            "reasoning_text"
                        )
                    if reasoning_text and tool_index is None:
                        # 检查请求是否开启了 thinking
                        if thinking_enabled:
                            # thinking block 已在外面初始化，直接发 delta
                            if not thinking_block_closed:
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': reasoning_text}})}\n\n"
                                accumulated_reasoning += reasoning_text
                        else:
                            # 请求未开启 thinking，作为 text 保留但加上标签
                            wrapped_reasoning = f"<thinking>{reasoning_text}</thinking>"
                            accumulated_text += wrapped_reasoning
                            if not text_block_closed:
                                text_sent = True
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': text_block_index, 'delta': {'type': 'text_delta', 'text': wrapped_reasoning}})}\n\n"

                    # Process text content
                    delta_content = None
                    if hasattr(delta, "content"):
                        delta_content = delta.content
                    elif isinstance(delta, dict) and "content" in delta:
                        delta_content = delta["content"]

                    if delta_content is not None and delta_content != "":
                        accumulated_text += delta_content
                        if tool_index is None:
                            if thinking_enabled and not thinking_block_closed:
                                # thinking block 还开着，先关闭它再开 text block
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                                thinking_block_closed = True
                                text_block_index = 1
                                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                            elif thinking_enabled and thinking_block_closed:
                                # thinking block 已关闭，确保 text block index = 1 且已开
                                if text_block_index == 0:
                                    text_block_index = 1
                                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                            else:
                                # 没开启 thinking，用 index 0 的 text block（已在外面初始化）
                                text_block_index = 0

                            text_sent = True
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': text_block_index, 'delta': {'type': 'text_delta', 'text': delta_content}})}\n\n"

                    # Process tool calls
                    delta_tool_calls = None

                    # Handle different formats of tool calls
                    if hasattr(delta, "tool_calls"):
                        delta_tool_calls = delta.tool_calls
                    elif isinstance(delta, dict) and "tool_calls" in delta:
                        delta_tool_calls = delta["tool_calls"]

                    # Process tool calls if any
                    if delta_tool_calls:
                        # First tool call we've seen - need to handle text properly
                        if tool_index is None:
                            # 先关闭 thinking block（如果还开着）
                            if thinking_enabled and not thinking_block_closed:
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                                thinking_block_closed = True
                                if text_block_index == 0:
                                    text_block_index = 1
                            # 关闭 text block（仅当它确实被打开过时）
                            if text_sent and not text_block_closed:
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_block_index})}\n\n"
                            elif (
                                accumulated_text
                                and not text_sent
                                and not text_block_closed
                            ):
                                # 发送积累的 text 再关闭
                                text_sent = True
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': text_block_index, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_block_index})}\n\n"
                            # 如果 text block 从未打开过（无 text 内容），不需要关闭
                            text_block_closed = True  # 无论如何标记为已关闭

                        # Convert to list if it's not already
                        if not isinstance(delta_tool_calls, list):
                            delta_tool_calls = [delta_tool_calls]

                        for tool_call in delta_tool_calls:
                            # Get the index of this tool call (for multiple tools)
                            current_index = None
                            if isinstance(tool_call, dict) and "index" in tool_call:
                                current_index = tool_call["index"]
                            elif hasattr(tool_call, "index"):
                                current_index = tool_call.index
                            else:
                                current_index = 0

                            # Check if this is a new tool or a continuation
                            if current_index not in openai_to_anthropic_tool_index:
                                tool_index = current_index
                                last_tool_index += 1
                                openai_to_anthropic_tool_index[current_index] = (
                                    last_tool_index
                                )
                                tool_json_buffers[last_tool_index] = ""

                                # Extract function info
                                if isinstance(tool_call, dict):
                                    function = tool_call.get("function", {})
                                    name = (
                                        function.get("name", "")
                                        if isinstance(function, dict)
                                        else ""
                                    )
                                    tool_id = tool_call.get(
                                        "id", f"toolu_{uuid.uuid4().hex[:24]}"
                                    )
                                else:
                                    function = getattr(tool_call, "function", None)
                                    name = (
                                        getattr(function, "name", "")
                                        if function
                                        else ""
                                    )
                                    tool_id = getattr(
                                        tool_call,
                                        "id",
                                        f"toolu_{uuid.uuid4().hex[:24]}",
                                    )

                                anthropic_tool_index = openai_to_anthropic_tool_index[
                                    current_index
                                ]
                                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anthropic_tool_index, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {}}})}\n\n"
                                current_tool_call = tool_call
                                tool_content = ""
                            else:
                                anthropic_tool_index = openai_to_anthropic_tool_index[
                                    current_index
                                ]

                            # Extract function arguments
                            arguments = None
                            if isinstance(tool_call, dict) and "function" in tool_call:
                                function = tool_call.get("function", {})
                                arguments = (
                                    function.get("arguments", "")
                                    if isinstance(function, dict)
                                    else ""
                                )
                            elif hasattr(tool_call, "function"):
                                function = getattr(tool_call, "function", None)
                                arguments = (
                                    getattr(function, "arguments", "")
                                    if function
                                    else ""
                                )

                            # If we have arguments, send them as a delta
                            if arguments:
                                # Try to detect if arguments are valid JSON or just a fragment
                                try:
                                    # If it's already a dict, use it
                                    if isinstance(arguments, dict):
                                        args_json = json.dumps(arguments)
                                    else:
                                        # Otherwise, try to parse it
                                        json.loads(arguments)
                                        args_json = arguments
                                except (json.JSONDecodeError, TypeError):
                                    # If it's a fragment, treat it as a string
                                    args_json = arguments

                                # Add to accumulated tool content
                                tool_content += (
                                    args_json if isinstance(args_json, str) else ""
                                )
                                if isinstance(args_json, str):
                                    tool_json_buffers[anthropic_tool_index] = (
                                        tool_json_buffers.get(anthropic_tool_index, "")
                                        + args_json
                                    )

                                # Send the update
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anthropic_tool_index, 'delta': {'type': 'input_json_delta', 'partial_json': args_json}})}\n\n"

                    # Process finish_reason - end the streaming response
                    if finish_reason and not has_sent_stop_reason:
                        has_sent_stop_reason = True

                        # Close any open tool call blocks
                        if tool_index is not None:
                            for i in range(1, last_tool_index + 1):
                                fix_suffix = _close_json_fragment(
                                    tool_json_buffers.get(i, "")
                                )
                                if fix_suffix:
                                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': fix_suffix}})}\n\n"
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

                        # If we accumulated text but never sent or closed text block, do it now
                        if not text_block_closed and text_sent:
                            # text block 被打开过，关闭它
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_block_index})}\n\n"
                            text_block_closed = True
                        elif not text_block_closed and accumulated_text and not text_sent:
                            # 有积累的 text 但没发过，先发再关
                            text_sent = True
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': text_block_index, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_block_index})}\n\n"
                            text_block_closed = True

                        # 如果 thinking block 还没关闭，关闭它
                        if thinking_enabled and not thinking_block_closed:
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            thinking_block_closed = True

                        # Map OpenAI finish_reason to Anthropic stop_reason
                        stop_reason = "end_turn"
                        if finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif finish_reason == "tool_calls":
                            stop_reason = "tool_use"
                        elif finish_reason == "stop":
                            stop_reason = "end_turn"

                        # Send message_delta with stop reason and usage
                        # 上游 QClaw 不返回 usage → 用 tiktoken 估算 output_tokens
                        if output_tokens == 0:
                            try:
                                _est_text = (
                                    accumulated_text
                                    + (accumulated_reasoning if thinking_enabled else "")
                                    + "".join(tool_json_buffers.values())
                                )
                                output_tokens = _estimate_text_tokens(
                                    _est_text,
                                    original_model_name or original_request.model,
                                )
                                logger.debug(
                                    f"STREAM EARLY-EXIT ESTIMATE: acc_text_len={len(accumulated_text)} "
                                    f"acc_reasoning_len={len(accumulated_reasoning)} "
                                    f"thinking_enabled={thinking_enabled} "
                                    f"est_output_tokens={output_tokens}"
                                )
                            except Exception as _e:
                                logger.debug(f"tiktoken output estimate (stream early-exit) failed: {_e}")
                        usage = {"output_tokens": output_tokens}

                        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n"

                        # Send message_stop event
                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

                        # Send final [DONE] marker to match Anthropic's behavior
                        yield "data: [DONE]\n\n"
                        return
            except Exception as e:
                # Log error but continue processing other chunks
                _log_exception(
                    "anthropic_stream_chunk_failed",
                    e,
                    {
                        "request_id": request_id or "unknown",
                        "model": original_model_name
                        or original_request.original_model
                        or original_request.model,
                        "stream_phase": "chunk_processing",
                        "has_tool_calls": tool_index is not None,
                    },
                )
                continue

        # If we didn't get a finish reason, close any open blocks
        if not has_sent_stop_reason:
            # Close any open tool call blocks
            if tool_index is not None:
                for i in range(1, last_tool_index + 1):
                    fix_suffix = _close_json_fragment(tool_json_buffers.get(i, ""))
                    if fix_suffix:
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': fix_suffix}})}\n\n"
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

            # Close the text content block
            # 如果 thinking 开启，先关 thinking block (index 0)，再关 text block (text_block_index)
            if thinking_enabled:
                if not thinking_block_closed:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                    thinking_block_closed = True
                # 关闭 text block（仅当被打开过）
                if text_sent and not text_block_closed:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_block_index})}\n\n"
                    text_block_closed = True
            else:
                if text_sent and not text_block_closed:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                    text_block_closed = True
                elif not text_sent and not text_block_closed:
                    # text block 从未被打开，不需要关闭
                    pass

            # Send final message_delta with usage
            # 上游 QClaw 不返回 usage → 用 tiktoken 估算 output_tokens
            if output_tokens == 0:
                try:
                    output_accumulated_text = (
                        accumulated_text
                        + (accumulated_reasoning if thinking_enabled else "")
                        + "".join(tool_json_buffers.values())
                    )
                    output_tokens = _estimate_text_tokens(
                        output_accumulated_text,
                        original_model_name or original_request.model,
                    )
                    logger.debug(
                        f"STREAM FINAL ESTIMATE: acc_text_len={len(accumulated_text)} "
                        f"acc_reasoning_len={len(accumulated_reasoning)} "
                        f"thinking_enabled={thinking_enabled} "
                        f"est_output_tokens={output_tokens}"
                    )
                except Exception as _e:
                    logger.debug(f"tiktoken output estimate (stream final) failed: {_e}")
            usage = {"output_tokens": output_tokens}

            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': usage})}\n\n"

            # Send message_stop event
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

            # Send final [DONE] marker to match Anthropic's behavior
            yield "data: [DONE]\n\n"

    except Exception as e:
        _log_exception(
            "anthropic_stream_failed",
            e,
            {
                "request_id": request_id or "unknown",
                "model": original_model_name
                or original_request.original_model
                or original_request.model,
                "stream_phase": "outer_handler",
                "output_tokens": output_tokens if "output_tokens" in locals() else 0,
            },
        )

        # Send error message_delta
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'error', 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n"

        # Send message_stop event
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        # Send final [DONE] marker
        yield "data: [DONE]\n\n"


async def handle_qclaw_streaming(qclaw_response, display_model: str):
    """QClaw OpenAI SSE 流 → Anthropic SSE 流 格式转换"""
    import uuid as _uuid
    msg_id = f"msg_{_uuid.uuid4().hex[:24]}"

    # message_start — 使用原始模型名让 Claude Code 识别
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': display_model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 0}}})}\n\n".encode()

    # content_block_start (text)
    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n".encode()

    accumulated = ""
    buffer = b""
    async for chunk in qclaw_response.aiter_bytes():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            decoded = line.decode("utf-8", errors="replace")
            if decoded.startswith("data: [DONE]"):
                break
            if not decoded.startswith("data: "):
                continue
            try:
                data = json.loads(decoded[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                finish = data.get("choices", [{}])[0].get("finish_reason")

                if content:
                    accumulated += content
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': content}})}\n\n".encode()

                if finish:
                    break
            except:
                continue

    # content_block_stop
    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n".encode()

    # message_delta — 用 tiktoken 估算 output_tokens（之前是 len(split()) 太粗）
    stop_reason = "end_turn"
    usage = {"output_tokens": _estimate_text_tokens(accumulated, display_model)}
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n".encode()

    # message_stop
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n".encode()


@app.post("/v1/messages")
async def create_message(request: MessagesRequest, raw_request: Request):
    request_id = _request_id_from_headers(raw_request)
    original_model = request.model if hasattr(request, "model") else "unknown"
    litellm_request = {}
    try:
        # print the body here
        body = await raw_request.body()

        # Parse the raw body as JSON since it's bytes
        body_json = json.loads(body.decode("utf-8"))
        original_model = body_json.get("model", "unknown")

        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]

        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/") :]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/") :]

        # Dump 上游原始请求关键字段，方便排查
        upstream_thinking = body_json.get("thinking", {})
        upstream_max_tokens = body_json.get("max_tokens", "N/A")
        logger.debug(
            f"📊 UPSTREAM REQUEST: model={original_model} stream={body_json.get('stream')} max_tokens={upstream_max_tokens} thinking={upstream_thinking}"
        )

        # ── Copilot /v1/messages 透传：直接转发 Anthropic 格式到下游 /v1/messages ──
        # 绕过 convert_anthropic_to_litellm() → LiteLLM → /chat/completions 的双层翻译，
        # Anthropic 原生格式零转换，thinking/tool_use/stream 等复杂结构不会丢失。
        # ── 统一模型定义解析：命中 models[] → Anthropic→OpenAI 翻译后转发到本地端口 ──
        # 未命中则继续走下方原有 copilot 透传 / LiteLLM 路径。
        mapped = _cfg._resolve_model_alias(_MODELS_CFG, original_model)
        if mapped:
            from anthropic_convert import convert_anthropic_request_to_openai, convert_openai_response_to_anthropic
            _fwd_port = int(mapped["port"])
            openai_body = convert_anthropic_request_to_openai(body_json)
            openai_body["model"] = mapped["model"]
            openai_payload = json.dumps(openai_body).encode("utf-8")
            _fwd_headers = {
                "content-type": "application/json",
                "host": f"127.0.0.1:{_fwd_port}",
                "content-length": str(len(openai_payload)),
            }
            if raw_request.headers.get("authorization"):
                _fwd_headers["authorization"] = raw_request.headers["authorization"]
            if raw_request.headers.get("x-api-key"):
                _fwd_headers["x-api-key"] = raw_request.headers["x-api-key"]
            _is_stream = bool(body_json.get("stream", False))
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                _req = client.build_request("POST", f"http://127.0.0.1:{_fwd_port}/v1/chat/completions", headers=_fwd_headers, content=openai_payload)
                _resp = await client.send(_req, stream=_is_stream)
                if _is_stream:
                    async def _models_stream():
                        try:
                            async for chunk in _resp.aiter_bytes():
                                yield chunk
                        finally:
                            await _resp.aclose()
                        yield b"data: [DONE]\n\n"
                    return StreamingResponse(_models_stream(), media_type="text/event-stream")
                _body_bytes = await _resp.aread()
                try:
                    _openai_resp = json.loads(_body_bytes.decode("utf-8"))
                except Exception:
                    return JSONResponse(content={"error": {"type": "proxy_error", "message": "upstream invalid response"}}, status_code=502)
                if _resp.status_code >= 400:
                    return JSONResponse(content=_openai_resp, status_code=_resp.status_code)
                _anthropic_resp = convert_openai_response_to_anthropic(_openai_resp, original_model)
                return JSONResponse(content=_anthropic_resp, status_code=_resp.status_code)

        if PREFERRED_PROVIDER == "copilot":
            target_model = _copilot_model_name(original_model)
            copilot_msgs_body = dict(body_json)
            copilot_msgs_body["model"] = target_model
            # 清理 Copilot 不接受的空/None content
            for msg in copilot_msgs_body.get("messages", []):
                c = msg.get("content")
                if c is None or (isinstance(c, str) and not c.strip()):
                    msg["content"] = "."
            # Copilot 不接受没有 tools 时的 tool_choice
            if copilot_msgs_body.get("tool_choice") and not copilot_msgs_body.get("tools"):
                copilot_msgs_body.pop("tool_choice")

            copilot_msgs_url = f"{COPILOT_API_BASE}/v1/messages"
            copilot_headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {COPILOT_GHE_TOKEN}",
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "anthropic-version": raw_request.headers.get("anthropic-version", "2023-06-01"),
            }
            log_request_beautifully(
                "POST", raw_request.url.path, original_model, target_model,
                len(copilot_msgs_body.get("messages", [])),
                len(copilot_msgs_body.get("tools") or []), 200
            )

            if copilot_msgs_body.get("stream"):
                async def copilot_messages_stream():
                    last_err = None
                    for attempt in range(3):
                        if attempt > 0:
                            await asyncio.sleep(0.5)
                        client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False)
                        try:
                            async with client.stream("POST", copilot_msgs_url, json=copilot_msgs_body, headers=copilot_headers) as resp:
                                if resp.status_code >= 500:
                                    await resp.aread()
                                    last_err = f"upstream {resp.status_code}"
                                    continue
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                                return
                        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                            last_err = str(e)
                            continue
                        finally:
                            await client.aclose()
                    yield json.dumps({"error": {"type": "proxy_error", "message": f"copilot /v1/messages upstream unavailable: {last_err}"}}).encode()
                return StreamingResponse(copilot_messages_stream(), media_type="text/event-stream")
            else:
                last_err = None
                for attempt in range(3):
                    if attempt > 0:
                        await asyncio.sleep(0.5)
                    try:
                        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                            resp = await client.post(copilot_msgs_url, json=copilot_msgs_body, headers=copilot_headers)
                            if resp.status_code >= 500 and attempt < 2:
                                last_err = f"upstream {resp.status_code}"
                                continue
                            resp_data = resp.json()
                            # 还原 Claude Code 原始模型名
                            if isinstance(resp_data, dict) and "model" in resp_data:
                                resp_data["model"] = original_model
                            return JSONResponse(content=resp_data, status_code=resp.status_code)
                    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                        last_err = str(e)
                        continue
                raise HTTPException(status_code=502, detail=f"copilot /v1/messages upstream unavailable: {last_err}")

        # Convert Anthropic request to LiteLLM format
        litellm_request = convert_anthropic_to_litellm(request)

        # Apply provider strategy（各策略自行设置 api_key/api_base/headers）
        provider_strategy = _PROVIDER_STRATEGIES.get(PREFERRED_PROVIDER, _default_provider)
        result = provider_strategy(request, litellm_request, original_model)
        if result is not None:
            return result

        # For OpenAI models - modify request format to work with limitations
        if "openai" in litellm_request["model"] and "messages" in litellm_request:
            logger.debug(f"Processing OpenAI model request: {litellm_request['model']}")

            # For OpenAI models, we need to convert content blocks to simple strings
            # and handle other requirements
            for i, msg in enumerate(litellm_request["messages"]):
                # Special case - handle message content directly when it's a list of tool_result
                # This is a specific case we're seeing in the error
                if "content" in msg and isinstance(msg["content"], list):
                    is_only_tool_result = True
                    for block in msg["content"]:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "tool_result"
                        ):
                            is_only_tool_result = False
                            break

                    if is_only_tool_result and len(msg["content"]) > 0:
                        logger.warning(
                            f"Found message with only tool_result content - special handling required"
                        )
                        # Extract the content from all tool_result blocks
                        all_text = ""
                        for block in msg["content"]:
                            all_text += "Tool Result:\n"
                            result_content = block.get("content", [])

                            # Handle different formats of content
                            if isinstance(result_content, list):
                                for item in result_content:
                                    if (
                                        isinstance(item, dict)
                                        and item.get("type") == "text"
                                    ):
                                        all_text += item.get("text", "") + "\n"
                                    elif isinstance(item, dict):
                                        # Fall back to string representation of any dict
                                        try:
                                            item_text = item.get(
                                                "text", json.dumps(item)
                                            )
                                            all_text += item_text + "\n"
                                        except:
                                            all_text += str(item) + "\n"
                            elif isinstance(result_content, str):
                                all_text += result_content + "\n"
                            else:
                                try:
                                    all_text += json.dumps(result_content) + "\n"
                                except:
                                    all_text += str(result_content) + "\n"

                        # Replace the list with extracted text
                        litellm_request["messages"][i]["content"] = (
                            all_text.strip() or "..."
                        )
                        logger.warning(
                            f"Converted tool_result to plain text: {all_text.strip()[:200]}..."
                        )
                        continue  # Skip normal processing for this message

                # 1. Handle content field - normal case
                if "content" in msg:
                    # Check if content is a list (content blocks)
                    if isinstance(msg["content"], list):
                        # Convert complex content blocks to simple string
                        text_content = ""
                        for block in msg["content"]:
                            if isinstance(block, dict):
                                # Handle different content block types
                                if block.get("type") == "text":
                                    text_content += block.get("text", "") + "\n"

                                # Handle tool_result content blocks - extract nested text
                                elif block.get("type") == "tool_result":
                                    tool_id = block.get("tool_use_id", "unknown")
                                    text_content += f"[Tool Result ID: {tool_id}]\n"

                                    # Extract text from the tool_result content
                                    result_content = block.get("content", [])
                                    if isinstance(result_content, list):
                                        for item in result_content:
                                            if (
                                                isinstance(item, dict)
                                                and item.get("type") == "text"
                                            ):
                                                text_content += (
                                                    item.get("text", "") + "\n"
                                                )
                                            elif isinstance(item, dict):
                                                # Handle any dict by trying to extract text or convert to JSON
                                                if "text" in item:
                                                    text_content += (
                                                        item.get("text", "") + "\n"
                                                    )
                                                else:
                                                    try:
                                                        text_content += (
                                                            json.dumps(item) + "\n"
                                                        )
                                                    except:
                                                        text_content += str(item) + "\n"
                                    elif isinstance(result_content, dict):
                                        # Handle dictionary content
                                        if result_content.get("type") == "text":
                                            text_content += (
                                                result_content.get("text", "") + "\n"
                                            )
                                        else:
                                            try:
                                                text_content += (
                                                    json.dumps(result_content) + "\n"
                                                )
                                            except:
                                                text_content += (
                                                    str(result_content) + "\n"
                                                )
                                    elif isinstance(result_content, str):
                                        text_content += result_content + "\n"
                                    else:
                                        try:
                                            text_content += (
                                                json.dumps(result_content) + "\n"
                                            )
                                        except:
                                            text_content += str(result_content) + "\n"

                                # Handle tool_use content blocks
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_id = block.get("id", "unknown")
                                    tool_input = json.dumps(block.get("input", {}))
                                    text_content += f"[Tool: {tool_name} (ID: {tool_id})]\nInput: {tool_input}\n\n"

                                # Handle image content blocks
                                elif block.get("type") == "image":
                                    text_content += "[Image content - not displayed in text format]\n"

                        # Make sure content is never empty for OpenAI models
                        if not text_content.strip():
                            text_content = "..."

                        litellm_request["messages"][i]["content"] = text_content.strip()
                    # Also check for None or empty string content
                    elif msg["content"] is None:
                        litellm_request["messages"][i]["content"] = (
                            "..."  # Empty content not allowed
                        )

                # 2. Remove any fields OpenAI doesn't support in messages
                for key in list(msg.keys()):
                    if key not in [
                        "role",
                        "content",
                        "name",
                        "tool_call_id",
                        "tool_calls",
                    ]:
                        logger.warning(
                            f"Removing unsupported field from message: {key}"
                        )
                        del msg[key]

            # 3. Final validation - check for any remaining invalid values and dump full message details
            for i, msg in enumerate(litellm_request["messages"]):
                # Log the message format for debugging
                logger.debug(
                    f"Message {i} format check - role: {msg.get('role')}, content type: {type(msg.get('content'))}"
                )

                # If content is still a list or None, replace with placeholder
                if isinstance(msg.get("content"), list):
                    logger.warning(
                        f"CRITICAL: Message {i} still has list content after processing: {json.dumps(msg.get('content'))}"
                    )
                    # Last resort - stringify the entire content as JSON
                    litellm_request["messages"][i]["content"] = (
                        f"Content as JSON: {json.dumps(msg.get('content'))}"
                    )
                elif msg.get("content") is None:
                    logger.warning(
                        f"Message {i} has None content - replacing with placeholder"
                    )
                    litellm_request["messages"][i]["content"] = (
                        "..."  # Fallback placeholder
                    )

        # Only log basic info about the request, not the full details
        logger.debug(
            f"Request for model: {litellm_request.get('model')}, stream: {litellm_request.get('stream', False)}"
        )

        # Handle streaming mode
        if request.stream:
            # Use LiteLLM for streaming
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                litellm_request.get("model"),
                len(litellm_request["messages"]),
                num_tools,
                200,  # Assuming success at this point
            )
            # Ensure we use the async version for streaming
            response_generator = await litellm.acompletion(**litellm_request)

            return StreamingResponse(
                handle_streaming(response_generator, request, original_model, request_id),
                media_type="text/event-stream",
            )
        else:
            # Use LiteLLM for regular completion
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                litellm_request.get("model"),
                len(litellm_request["messages"]),
                num_tools,
                200,  # Assuming success at this point
            )
            start_time = time.time()
            litellm_response = litellm.completion(**litellm_request)
            logger.debug(
                f"✅ RESPONSE RECEIVED: Model={litellm_request.get('model')}, Time={time.time() - start_time:.2f}s"
            )

            # Convert LiteLLM response to Anthropic format
            anthropic_response = convert_litellm_to_anthropic(litellm_response, request)

            # 还原 Claude Code 原始模型名
            if original_model and hasattr(anthropic_response, "model"):
                anthropic_response.model = original_model

            return anthropic_response

    except Exception as e:
        _log_exception(
            "anthropic_messages_failed",
            e,
            {
                "request_id": request_id,
                "path": str(raw_request.url.path),
                "original_model": original_model,
                "mapped_model": litellm_request.get("model"),
                "stream": bool(getattr(request, "stream", False)),
                "message_count": len(getattr(request, "messages", []) or []),
                "tool_count": len(getattr(request, "tools", []) or []),
                "has_thinking": bool(getattr(request, "thinking", None)),
                "max_tokens": getattr(request, "max_tokens", None),
                "max_completion_tokens": litellm_request.get("max_completion_tokens"),
            },
        )

        # 检测 QClaw 网关 upstream auth 过期 (9002)
        # litellm 内部缓存的状态重置不彻底，绕过 litellm 直接用 httpx 重试
        if _is_auth_expired_error(e) and PREFERRED_PROVIDER == "qclaw":
            await _reset_litellm_clients()
            logger.info("🔄 Retrying via httpx passthrough (bypass litellm)...")
            try:
                return await _passthrough_to_qclaw(litellm_request, request, original_model, request_id)
            except Exception as retry_e:
                _log_exception("anthropic_messages_fallback_failed", retry_e, {"original_error": str(e)})
                raise HTTPException(status_code=getattr(retry_e, "status_code", 502),
                                     detail=f"QClaw fallback failed: {str(retry_e)}")

        # Format error for response
        error_message = f"Error: {str(e)}"

        # Return detailed error
        status_code = getattr(e, "status_code", 500)
        raise HTTPException(status_code=status_code, detail=error_message)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: TokenCountRequest, raw_request: Request):
    try:
        # Log the incoming token count request
        original_model = request.original_model or request.model

        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]

        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/") :]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/") :]

        # Convert the messages to a format LiteLLM can understand
        converted_request = convert_anthropic_to_litellm(
            MessagesRequest(
                model=request.model,
                max_tokens=100,  # Arbitrary value not used for token counting
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                thinking=request.thinking,
            )
        )

        # Use LiteLLM's token_counter function
        try:
            # Import token_counter function
            from litellm import token_counter

            # Log the request beautifully
            num_tools = len(request.tools) if request.tools else 0

            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                converted_request.get("model"),
                len(converted_request["messages"]),
                num_tools,
                200,  # Assuming success at this point
            )

            # Prepare token counter arguments
            token_counter_args = {
                "model": converted_request["model"],
                "messages": converted_request["messages"],
            }

            # Add custom base URL for OpenAI models if configured
            if request.model.startswith("openai/") and OPENAI_BASE_URL:
                token_counter_args["api_base"] = OPENAI_BASE_URL

            # Count tokens
            token_count = token_counter(**token_counter_args)

            # Return Anthropic-style response
            return TokenCountResponse(input_tokens=token_count)

        except ImportError:
            logger.error("Could not import token_counter from litellm")
            # Fallback：用 tiktoken 本地估算（之前硬编码 1000 误差太大）
            try:
                est = _estimate_messages_tokens(
                    converted_request.get("messages", []) or [],
                    converted_request.get("model", request.model),
                    system=getattr(request, "system", None),
                    tools=getattr(request, "tools", None),
                )
                return TokenCountResponse(input_tokens=est)
            except Exception:
                return TokenCountResponse(input_tokens=1000)  # 终极兜底

    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        logger.error(f"Error counting tokens: {str(e)}\n{error_traceback}")
        raise HTTPException(status_code=500, detail=f"Error counting tokens: {str(e)}")


# ── 下游模型列表缓存（避免每次 /v1/models 都请求上游）──
_DOWNSTREAM_MODELS_CACHE: Optional[List[dict]] = None
_DOWNSTREAM_MODELS_CACHE_TIME: float = 0
_MODELS_CACHE_TTL: float = 300.0  # 5 分钟


async def _fetch_downstream_models() -> List[dict]:
    """从下游网关拉取模型列表，按下游 endpoint 区分 provider 拉取方式。

    - copilot / openai / qclaw：直连下游 /models（OpenAI 格式）
    - 返回统一格式的 model dict 列表
    """
    global _DOWNSTREAM_MODELS_CACHE, _DOWNSTREAM_MODELS_CACHE_TIME
    now = time.time()
    if _DOWNSTREAM_MODELS_CACHE and (now - _DOWNSTREAM_MODELS_CACHE_TIME) < _MODELS_CACHE_TTL:
        return _DOWNSTREAM_MODELS_CACHE

    downstream = []
    try:
        if PREFERRED_PROVIDER == "copilot":
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), trust_env=False) as client:
                resp = await client.get(
                    f"{COPILOT_API_BASE}/models",
                    headers={
                        "Authorization": f"Bearer {COPILOT_GHE_TOKEN}",
                        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        caps = m.get("capabilities", {}) or {}
                        family = caps.get("family", m.get("id", ""))
                        limits = caps.get("limits", {}) or {}
                        supports = caps.get("supports", {}) or {}
                        endpoints = m.get("supported_endpoints", [])
                        downstream.append({
                            "id": m["id"],
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": m.get("vendor", "copilot"),
                            "display_name": m.get("name", m["id"]),
                            "description": m.get("id", ""),
                            # 扩展字段 — 透传给了解下游能力的客户端
                            "context_window": limits.get("max_context_window_tokens"),
                            "max_output_tokens": limits.get("max_output_tokens"),
                            "supports_tools": supports.get("tool_calls", False),
                            "supports_vision": supports.get("vision", False),
                            "supports_streaming": supports.get("streaming", False),
                            "supports_thinking": supports.get("adaptive_thinking", False),
                            "supports_reasoning_effort": supports.get("reasoning_effort", []),
                            "supported_endpoints": endpoints,
                            "model_family": family,
                            "tokenizer": caps.get("tokenizer"),
                            "preview": m.get("preview", False),
                        })
        elif PREFERRED_PROVIDER in ("openai",):
            # OpenAI 上游
            base = OPENAI_BASE_URL or "https://api.openai.com/v1"
            url = base.rstrip("/") + "/models"
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), trust_env=False) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"})
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        downstream.append({
                            "id": m["id"],
                            "object": "model",
                            "created": m.get("created", 1700000000),
                            "owned_by": m.get("owned_by", "openai"),
                            "display_name": m.get("id", ""),
                        })

        if downstream:
            logger.debug(f"Fetched {len(downstream)} downstream models from {PREFERRED_PROVIDER}")
            _DOWNSTREAM_MODELS_CACHE = downstream
            _DOWNSTREAM_MODELS_CACHE_TIME = now
    except Exception as e:
        logger.warning(f"Failed to fetch downstream models: {e}")
        if _DOWNSTREAM_MODELS_CACHE:
            return _DOWNSTREAM_MODELS_CACHE  # 用过期缓存兜底

    return downstream


def _build_models_list(include_aliases: bool = True) -> List[dict]:
    """构建模型列表，同时兼容 OpenAI 和 Anthropic 两套规范。

    - copilot/openai provider：从下游 /models 拉取 + Claude Code 别名
    - 其他 provider：硬编码列表 + 别名
    - include_aliases=True：加 Anthropic 别名（8081 Anthropic 端口用）
    - include_aliases=False：只返回真实下游模型（8082 OpenAI 端口用）

    OpenAI 客户端读 object/owned_by，Anthropic 客户端读 type/display_name，
    两套字段都塞进去，各取所需。
    """
    models: List[dict] = []

    # ── 翻译链路别名（仅 8081 Anthropic 端口需要）──
    # 动态来自 targets.json 顶层 models[]（name + aliases 均可被 _resolve_model_alias 命中），
    # 与 dashboard「模型定义」编辑视图同源；不再硬编码（曾含 claude-*-4-20250514 死名单）。
    if include_aliases:
        for _m in _MODELS_CFG.get("models", []):
            if not isinstance(_m, dict) or not _m.get("name"):
                continue
            _names = [str(_m["name"])] + [str(a) for a in (_m.get("aliases") or []) if isinstance(a, str)]
            for _mid in _names:
                models.append({
                    "id": _mid,
                    "object": "model",
                    "type": "model",
                    "created": 1700000000,
                    "owned_by": "anthropic",
                    "display_name": _humanize_model_name(_m["name"]),
                })

    # ── 能用下游 /models 的 provider：直接用缓存的列表（异步预拉取在 startup 完成）──
    _downstream = _DOWNSTREAM_MODELS_CACHE or []
    if _downstream:
        for dm in _downstream:
            entry = dict(dm)
            entry.setdefault("type", "model")
            models.append(entry)
        return models

    # ── 无下游缓存的 fallback（qclaw / gemini / anthropic 等）──
    _passthrough_models = []
    if PREFERRED_PROVIDER in ("qclaw",):
        _passthrough_models = [
            ("modelroute", "QClaw Model Route"),
            ("pool-deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("pool-deepseek-v4-flash", "DeepSeek V4 Flash"),
            ("pool-glm-5.2", "GLM 5.2"),
            ("pool-glm-5.1", "GLM 5.1"),
            ("pool-kimi-k2.7-code-highspeed", "Kimi K2.7 Code"),
            ("pool-kimi-k2.6", "Kimi K2.6"),
            ("pool-minimax-m3", "MiniMax M3"),
            ("pool-minimax-m2.7", "MiniMax M2.7"),
        ]
    elif PREFERRED_PROVIDER == "copilot":
        _passthrough_models = [
            (COPILOT_BIG_MODEL, "Copilot Big"),
            (COPILOT_MEDIUM_MODEL, "Copilot Medium"),
            (COPILOT_SMALL_MODEL, "Copilot Small"),
        ]

    for mid, display in _passthrough_models:
        models.append({
            "id": mid,
            "object": "model",
            "type": "model",
            "created": 1700000000,
            "owned_by": "qclaw" if mid.startswith("pool") or mid == "modelroute" else "copilot",
            "display_name": display,
        })

    return models


@app.get("/v1/models")
@app.get("/api/v1/models")
@app.get("/openai/v1/models")
async def list_models():
    """8081 Anthropic /v1/models — 动态来自 targets.json models[]（与 dashboard 编辑视图同源）"""
    models = [
        {**m, "object": "model", "type": "model", "created": 1700000000, "owned_by": "anthropic"}
        for m in _anthropic_port_models()
    ]
    return {"data": models, "object": "list", "has_more": False}


@app.get("/api/tags")
async def list_ollama_tags():
    """Ollama 风格的模型列表（部分工具如 openclaw 会探测 /api/tags）。"""
    models = []
    for m in _build_models_list():
        models.append({
            "name": m["id"],
            "model": m["id"],
            "modified_at": "2024-01-01T00:00:00Z",
            "size": 0,
            "details": {"family": "qclaw", "parameter_size": "unknown"},
        })
    return {"models": models}


@app.get("/")
async def root():
    return {"message": "Anthropic Proxy for LiteLLM"}


# ─── 统一管理面板（所有 LLM 相关服务一览）─────────────────────────────

DASHBOARD_STYLE = """
  /* ── 设计 Token（OpenRouter 风格：深色近黑 + 品牌青蓝渐变）── */
  :root {
    --bg-page: #0a0a0f;
    --bg-elev: #10101a;
    --bg-card: #13131d;
    --bg-card-hi: #171724;
    --bg-inset: #0d0d14;
    --border: rgba(148, 163, 184, 0.14);
    --border-strong: rgba(148, 163, 184, 0.28);
    --border-focus: rgba(34, 211, 238, 0.55);
    --brand-cyan: #22d3ee;
    --brand-blue: #3b82f6;
    --brand-grad: linear-gradient(135deg, #22d3ee 0%, #3b82f6 100%);
    --brand-glow: rgba(34, 211, 238, 0.35);
    --text-primary: #eceef4;
    --text-secondary: #9aa3b2;
    --text-tertiary: #6b7280;
    --success: #34d399;
    --warning: #fbbf24;
    --danger: #f87171;
    --radius-lg: 14px;
    --radius-md: 10px;
    --radius-sm: 7px;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", monospace;
  }

  /* ── 全局 ── */
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", ui-monospace, sans-serif; background-color: var(--bg-page); color: var(--text-primary); margin: 0; padding: 32px; min-height: 100vh; background-image: radial-gradient(1000px 500px at 85% -10%, rgba(59, 130, 246, 0.10), transparent 60%), radial-gradient(900px 460px at -10% 0%, rgba(34, 211, 238, 0.07), transparent 55%), radial-gradient(2px 2px at 20% 30%, rgba(148,163,184,0.10), transparent 100%), radial-gradient(2px 2px at 70% 60%, rgba(148,163,184,0.08), transparent 100%); background-attachment: fixed; }
  h1 { font-size: 21px; font-weight: 700; margin: 0 0 2px 0; letter-spacing: -0.3px; }
  h3 { font-size: 14px; font-weight: 600; margin: 0 0 10px 0; }
  .sub { color: var(--text-secondary); font-size: 13px; margin-bottom: 22px; }
  .sub .refresh-time { font-size: 12px; color: var(--text-tertiary); }
  code { color: var(--brand-cyan); }
  a { color: #7aa2ff; }

  /* ── 总览栏：KPI 统计卡（OpenRouter 大数字风格）── */
  .overview-bar { display: flex; gap: 20px; flex-wrap: wrap; align-items: stretch; background: linear-gradient(180deg, rgba(22,22,36,0.9) 0%, rgba(13,13,20,0.9) 100%); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px; margin-bottom: 26px; box-shadow: 0 10px 40px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.05); backdrop-filter: blur(4px); }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 14px; flex: 1 1 auto; min-width: 0; }
  .kpi-card { position: relative; display: flex; flex-direction: column; gap: 6px; background: linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.015) 100%), var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px 18px 16px; overflow: hidden; transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s; }
  .kpi-card::before { content: ""; position: absolute; top: 0; left: 12%; right: 12%; height: 1px; background: linear-gradient(90deg, transparent, rgba(34,211,238,0.7), transparent); }
  .kpi-card::after { content: ""; position: absolute; inset: 0; background: radial-gradient(120% 90% at 100% 0%, rgba(34,211,238,0.09), transparent 55%); pointer-events: none; }
  .kpi-card:hover { border-color: rgba(34,211,238,0.4); transform: translateY(-2px); box-shadow: 0 8px 28px rgba(34,211,238,0.10), 0 4px 16px rgba(0,0,0,0.35); }
  .kpi-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1.2px; color: var(--text-tertiary); }
  .kpi-value { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 32px; font-weight: 700; line-height: 1.05; color: var(--text-primary); letter-spacing: -0.5px; }
  .kpi-value small { font-size: 16px; font-weight: 600; color: var(--text-secondary); margin-left: 3px; }
  .kpi-value.accent { background: var(--brand-grad); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
  .kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
  .kpi-sub .kpi-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; vertical-align: 1px; }
  .ov-side { display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; gap: 12px; flex-shrink: 0; }
  .ov-dots { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
  .ov-actions { display: flex; gap: 10px; align-items: center; }
  .status-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
  .status-dot.green { background: var(--success); box-shadow: 0 0 8px rgba(52,211,153,0.6); }
  .status-dot.yellow { background: var(--warning); box-shadow: 0 0 8px rgba(251,191,36,0.5); }
  .status-dot.red { background: var(--danger); box-shadow: 0 0 8px rgba(248,113,113,0.5); }

  /* ── 卡片头启动状态灯（呼吸动画）── */
  .ct-lamp { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; transition: background 0.3s, box-shadow 0.3s; }
  .ct-lamp.on { background: var(--success); box-shadow: 0 0 10px rgba(52,211,153,0.7); animation: lampPulse 2.2s ease-in-out infinite; }
  .ct-lamp.off { background: var(--danger); box-shadow: 0 0 8px rgba(248,113,113,0.6); }
  .ct-lamp.idle { background: #9aa3b2; box-shadow: 0 0 6px rgba(154,163,178,0.4); }
  @keyframes lampPulse { 0%, 100% { box-shadow: 0 0 6px rgba(52,211,153,0.4); } 50% { box-shadow: 0 0 14px rgba(52,211,153,0.9); } }

  /* ── 区块 ── */
  .section { margin-bottom: 28px; }
  .section-title { font-size: 14px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1.4px; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid rgba(148,163,184,0.10); display: flex; align-items: center; gap: 8px; }
  .section-title::before { content: ""; width: 3px; height: 15px; border-radius: 3px; background: var(--brand-grad); flex-shrink: 0; box-shadow: 0 0 8px var(--brand-glow); }
  .section-title .sec-count { display: inline-flex; align-items: center; justify-content: center; min-width: 22px; height: 20px; font-size: 11px; font-weight: 700; color: var(--brand-cyan); background: rgba(34,211,238,0.10); border: 1px solid rgba(34,211,238,0.25); border-radius: 999px; padding: 0 8px; margin-left: 2px; vertical-align: middle; letter-spacing: 0; }

  /* ── 卡片纵向排列（单列，不做自适应 flow）── */
  .card-grid { display: flex; flex-direction: column; gap: 14px; }

  /* ── 卡片容器：深色渐变底 + 顶部高光线 + hover 品牌光晕 ── */
  .card { position: relative; background: linear-gradient(180deg, rgba(255,255,255,0.035) 0%, rgba(255,255,255,0.008) 40%, rgba(255,255,255,0) 100%), var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; transition: border-color 0.25s, transform 0.2s, box-shadow 0.25s; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 1px 4px rgba(0,0,0,0.3); }
  .card::before { content: ""; position: absolute; top: 0; left: 8%; right: 8%; height: 1px; background: linear-gradient(90deg, transparent, rgba(34,211,238,0.45), transparent); z-index: 1; pointer-events: none; }
  .card:hover { border-color: rgba(34,211,238,0.35); transform: translateY(-3px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 12px 40px rgba(34,211,238,0.10), 0 6px 20px rgba(0,0,0,0.4); }
  /* 端口强调条：左 3px 彩色 border */
  .card.accent-8082 { border-left: 3px solid #3b82f6; }
  .card.accent-8084 { border-left: 3px solid #a78bfa; }
  .card.accent-8090 { border-left: 3px solid #f59e0b; }
  .card.accent-8091 { border-left: 3px solid #34d399; }
  .card.accent-8092 { border-left: 3px solid #22d3ee; }
  .card.accent-8093 { border-left: 3px solid #c084fc; }
  .card.accent-8094 { border-left: 3px solid #fbbf24; }
  .card.accent-8083 { border-left: 3px solid #38bdf8; }
  .card.accent-8085 { border-left: 3px solid #f472b6; }
  .card.accent-8086 { border-left: 3px solid #34d399; }
  .card.accent-8080 { border-left: 3px solid #22d3ee; }

  /* ── 卡片头（可点击 toggle）── */
  .card-toggle { display: flex; align-items: center; gap: 10px; width: 100%; padding: 14px 22px; background: none; border: none; color: inherit; font: inherit; cursor: pointer; text-align: left; user-select: none; transition: background 0.2s; }
  .card-toggle:hover { background: rgba(255,255,255,0.03); }
  .card-toggle:focus-visible { outline: 2px solid var(--brand-cyan); outline-offset: -2px; }
  .card-toggle:active { transform: scale(0.98); }
  .card-toggle .ct-name { font-size: 16px; font-weight: 600; flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card-toggle .ct-port { font-size: 13px; color: var(--text-secondary); font-family: var(--font-mono); white-space: nowrap; margin-left: 2px; }
  .card-toggle .ct-summary { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; margin-left: auto; }
  .card-toggle .ct-arrow { font-size: 12px; color: var(--text-tertiary); transition: transform 0.25s ease; flex-shrink: 0; margin-left: 4px; }
  .card-toggle .ct-arrow.open { transform: rotate(180deg); }
  .card-toggle .badge-group { display: flex; gap: 6px; flex-wrap: wrap; flex-shrink: 0; }

  /* ── 详情区（手风琴体）── */
  .card-detail { max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.25s ease, opacity 0.2s ease, padding 0.25s ease; padding: 0 22px; }
  .card-detail.open { max-height: 4000px; opacity: 1; padding: 0 22px 18px 22px; }
  .card-detail > *:first-child { margin-top: 0; }
  /* 内嵌子容器：把 kv/统计/模型/凭据 分成独立视觉区块，避免展开后"杂货铺"感 */
  .card-detail > .kv, .card-detail > .card-desc, .card-detail > .stats-block, .card-detail > .model-section, .card-detail > .token-edit { background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); border: 1px solid rgba(148,163,184,0.10); border-radius: var(--radius-sm); }
  .card-detail > .kv { padding: 12px 14px; margin: 12px 0 0 0; }
  .card-detail > .card-desc { padding: 10px 14px; margin: 10px 0 0 0; }
  .card-detail > .stats-block { padding: 12px 14px; margin: 10px 0 0 0; }
  .card-detail > .model-section { padding: 12px 14px; margin: 10px 0 0 0; }
  .card-detail > .token-edit { padding: 12px 14px; margin: 10px 0 0 0; }

  /* ── badge（分类 + 状态）：低饱和半透明底 + 细边框，品牌青蓝为主，状态色仅语义用 ── */
  .badge { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 600; white-space: nowrap; letter-spacing: 0.02em; backdrop-filter: blur(2px); }
  .badge-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  /* 分类 badge：crack（品牌青蓝）/ free（语义绿）/ paid（语义橙）/ generic（中性灰） */
  .badge.b-crack { background: rgba(34,211,238,0.10); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.28); }
  .badge.b-crack .badge-dot { background: var(--brand-cyan); box-shadow: 0 0 6px rgba(34,211,238,0.7); }
  .badge.b-free { background: rgba(52,211,153,0.09); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.26); }
  .badge.b-free .badge-dot { background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); }
  .badge.b-paid { background: rgba(251,191,36,0.09); color: #fcd34d; border: 1px solid rgba(251,191,36,0.26); }
  .badge.b-paid .badge-dot { background: #f59e0b; box-shadow: 0 0 6px rgba(245,158,11,0.6); }
  .badge.b-generic { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-generic .badge-dot { background: #9aa3b2; }
  /* 状态 badge：细底 + 状态点 */
  .badge.b-st-green { background: rgba(52,211,153,0.09); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.26); }
  .badge.b-st-green::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); flex-shrink: 0; }
  .badge.b-st-blue { background: rgba(59,130,246,0.10); color: #93c5fd; border: 1px solid rgba(59,130,246,0.30); }
  .badge.b-st-blue::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--brand-blue); box-shadow: 0 0 6px rgba(59,130,246,0.6); flex-shrink: 0; }
  .badge.b-st-red { background: rgba(248,113,113,0.09); color: #fca5a5; border: 1px solid rgba(248,113,113,0.26); }
  .badge.b-st-red::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--danger); box-shadow: 0 0 6px rgba(248,113,113,0.6); flex-shrink: 0; }
  .badge.b-st-yellow { background: rgba(251,191,36,0.09); color: #fcd34d; border: 1px solid rgba(251,191,36,0.26); }
  .badge.b-st-yellow::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex-shrink: 0; }
  .badge.b-st-purple { background: rgba(192,132,252,0.09); color: #d8b4fe; border: 1px solid rgba(192,132,252,0.26); }
  .badge.b-st-purple::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #c084fc; flex-shrink: 0; }
  .badge.b-st-orange { background: rgba(251,146,60,0.09); color: #fdba74; border: 1px solid rgba(251,146,60,0.26); }
  .badge.b-st-orange::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #f59e0b; flex-shrink: 0; }
  .badge.b-st-gray { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-st-gray::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #9aa3b2; flex-shrink: 0; }
  /* 元数据标签：破解/非破解、免费/收费、稳定性 */
  .badge.b-meta-crack { background: rgba(34,211,238,0.08); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.24); }
  .badge.b-meta-normal { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-meta-free { background: rgba(52,211,153,0.08); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.24); }
  .badge.b-meta-paid { background: rgba(251,191,36,0.08); color: #fcd34d; border: 1px solid rgba(251,191,36,0.24); }
  .badge.b-meta-stable { background: rgba(52,211,153,0.08); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.24); }
  .badge.b-meta-stable::before { content: '●'; font-size: 8px; margin-right: 3px; color: var(--success); }
  .badge.b-meta-unstable { background: rgba(251,191,36,0.08); color: #fcd34d; border: 1px solid rgba(251,191,36,0.24); }
  .badge.b-meta-unstable::before { content: '◐'; font-size: 9px; margin-right: 3px; color: var(--warning); }
  .badge.b-meta-agg { background: rgba(34,211,238,0.08); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.24); }
  .badge.b-meta-agg::before { content: '◎'; font-size: 9px; margin-right: 3px; color: var(--brand-cyan); }
  .badge.b-meta-gemini { background: rgba(56,189,248,0.08); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.24); }
  .badge.b-meta-gemini::before { content: '◆'; font-size: 8px; margin-right: 3px; color: #38bdf8; }
  .badge.b-meta-oa { background: rgba(129,140,248,0.08); color: #a5b4fc; border: 1px solid rgba(129,140,248,0.24); }
  .badge.b-meta-oa::before { content: '◈'; font-size: 8px; margin-right: 3px; color: #818cf8; }

  /* ── kv 元信息 ── */
  .kv { display: grid; grid-template-columns: 130px 1fr; gap: 6px 16px; font-size: 13px; margin-bottom: 10px; }
  .kv div:nth-child(odd) { color: var(--text-secondary); }
  .card-desc { font-size: 12.5px; color: var(--text-secondary); margin-bottom: 8px; }

  /* ── 流量统计块 ── */
  .stats-block { display: flex; gap: 22px; flex-wrap: wrap; margin: 12px 0 10px 0; }
  .stat-item { display: flex; flex-direction: column; gap: 3px; }
  .stat-label { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; }
  .stat-value { font-size: 28px; font-weight: 700; color: var(--text-primary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; letter-spacing: -0.5px; line-height: 1.1; text-shadow: 0 0 20px rgba(34,211,238,0.25); }

  /* ── 进度条 ── */
  .rate-bar { display: flex; height: 9px; border-radius: 5px; overflow: hidden; background: rgba(148,163,184,0.08); border: 1px solid rgba(148,163,184,0.10); margin: 10px 0; box-shadow: inset 0 1px 2px rgba(0,0,0,0.3); }
  .rate-bar-seg { transition: width 0.6s ease; }
  .rate-bar-seg.ok { background: linear-gradient(90deg, #10b981, #34d399); box-shadow: 0 0 8px rgba(52,211,153,0.4); }
  .rate-bar-seg.tr429 { background: linear-gradient(90deg, #d97706, #fbbf24); box-shadow: 0 0 8px rgba(251,191,36,0.35); }
  .rate-bar-seg.err { background: linear-gradient(90deg, #dc2626, #f87171); box-shadow: 0 0 8px rgba(248,113,113,0.35); }
  .mini-stats { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12.5px; color: var(--text-secondary); margin-top: 6px; }
  .mini-stats b { color: var(--text-primary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

  /* ── 模型表格 ── */
  .model-count { display: inline-block; background: rgba(59,130,246,0.12); color: #93c5fd; font-size: 11px; padding: 2px 8px; border-radius: 999px; margin-left: 6px; font-weight: 600; border: 1px solid rgba(59,130,246,0.28); }
  .no-models { font-size: 12.5px; color: var(--text-tertiary); margin-top: 6px; font-style: italic; }
  .model-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12.5px; table-layout: fixed; }
  .model-table th { text-align: left; padding: 8px 12px; color: var(--text-tertiary); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-table th:nth-child(1) { width: 3%; text-align: center; }
  .model-table th:nth-child(2) { width: 18%; }
  .model-table th:nth-child(3) { width: 22%; }
  .model-table th:nth-child(4) { width: 10%; text-align: center; }
  .model-table th:nth-child(5) { width: 12%; text-align: center; }
  .model-table th:nth-child(6) { width: 10%; text-align: center; }
  .model-table th:nth-child(7) { width: 10%; text-align: center; }
  .model-table td { padding: 7px 12px; border-bottom: 1px solid rgba(148,163,184,0.10); overflow-wrap: anywhere; }
  .model-table td.num { color: var(--text-tertiary); font-family: var(--font-mono); text-align: center; }
  .model-table td.mid { font-family: var(--font-mono); overflow-wrap: anywhere; }
  .model-table td.name { color: #c9cedd; }
  .model-table td.mstat { text-align: center; }
  .model-table td.act { width: 36px; text-align: center; }
  .model-table tbody tr:nth-child(even) { background: rgba(255,255,255,0.02); }
  .model-table tbody tr:hover { background: rgba(34,211,238,0.05); }
  .model-table tbody tr:hover { background: rgba(34,211,238,0.06); }
  .mstat { text-align: center; padding: 4px 8px; }
  .mstat.err { color: #f87171; }
  .mstat.warn { color: #fbbf24; }

  /* ── 模型编辑操作行（编辑态切换 + 保存）── */
  .model-ops { display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
  .model-edit-toggle, .model-save-btn { border-radius: var(--radius-sm); padding: 5px 12px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; transition: background 0.2s, border-color 0.2s, transform 0.15s, box-shadow 0.2s; }
  .model-edit-toggle { background: transparent; color: var(--brand-cyan); border: 1px solid rgba(34,211,238,0.28); }
  .model-edit-toggle:hover { background: rgba(34,211,238,0.10); border-color: rgba(34,211,238,0.5); transform: translateY(-1px); }
  .model-edit-toggle:active, .model-save-btn:active { transform: scale(0.98); }
  .model-prune-btn { border-radius: var(--radius-sm); padding: 5px 12px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; transition: background 0.2s, transform 0.15s; background: transparent; color: #fca5a5; border: 1px solid rgba(248,113,113,0.30); }
  .model-prune-btn:hover { background: rgba(248,113,113,0.10); border-color: rgba(248,113,113,0.55); transform: translateY(-1px); }
  .model-prune-btn:disabled { opacity: 0.6; cursor: wait; }
  .model-save-btn { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 14px rgba(59,130,246,0.35); }
  .model-save-btn:hover { filter: brightness(1.1); box-shadow: 0 6px 20px rgba(34,211,238,0.4); transform: translateY(-1px); }

  /* ── 展示开关（iOS 风格滑动 switch）── */
  .switch { position: relative; display: inline-block; width: 44px; height: 26px; vertical-align: middle; cursor: pointer; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch-slider { position: absolute; inset: 0; background: linear-gradient(135deg, #3a4158, #2c3148); border-radius: 999px; transition: background 0.25s ease; box-shadow: inset 0 1px 3px rgba(0,0,0,0.35), 0 1px 0 rgba(255,255,255,0.04); }
  .switch-slider::before { content: ''; position: absolute; width: 20px; height: 20px; left: 3px; top: 3px; background: radial-gradient(circle at 35% 30%, #f5f7fb, #c7ccd8); border-radius: 50%; transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1), background 0.25s ease; box-shadow: 0 2px 5px rgba(0,0,0,0.4); }
  .switch input:checked + .switch-slider { background: var(--brand-grad); box-shadow: inset 0 1px 2px rgba(0,0,0,0.15), 0 0 12px rgba(34,211,238,0.30); }
  .switch input:checked + .switch-slider::before { transform: translateX(18px); background: radial-gradient(circle at 35% 30%, #ffffff, #d5f4fc); }
  .switch input:focus-visible + .switch-slider { outline: 2px solid var(--brand-cyan); outline-offset: 2px; }
  .switch input:disabled + .switch-slider { opacity: 0.5; cursor: not-allowed; }

  /* ── 模型编辑 modal ── */
  .modal-overlay { position: fixed; inset: 0; background: rgba(5, 6, 10, 0.8); backdrop-filter: blur(6px); display: none; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
  .modal-overlay.open { display: flex; }
  .modal { background: linear-gradient(180deg, #17172a 0%, #101019 100%); border: 1px solid var(--border-strong); border-radius: 16px; width: 100%; max-width: 640px; max-height: 82vh; display: flex; flex-direction: column; box-shadow: 0 24px 70px rgba(0,0,0,0.65), 0 0 0 1px rgba(34,211,238,0.05), inset 0 1px 0 rgba(255,255,255,0.06); animation: modalIn 0.22s ease; }
  @keyframes modalIn { from { opacity: 0; transform: translateY(14px) scale(0.98); } to { opacity: 1; transform: none; } }
  .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 16px 20px; border-bottom: 1px solid #23263a; }
  .modal-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .modal-close { background: none; border: none; color: #6b7280; font-size: 20px; cursor: pointer; line-height: 1; padding: 4px 8px; border-radius: 6px; transition: color 0.2s, background 0.2s; }
  .modal-close:hover { color: #e0e0e0; background: #23263a; }
  .modal-body { overflow-y: auto; padding: 12px 20px; flex: 1; min-height: 0; }
  .modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px; border-top: 1px solid #23263a; }
  /* modal 内模型行 */
  .mrow { display: flex; align-items: center; gap: 12px; padding: 10px 4px; border-bottom: 1px solid #1f2233; }
  .mrow:last-child { border-bottom: none; }
  .mrow.mrow-master { margin-bottom: 2px; padding: 12px 4px; border-bottom: 1px dashed #3b4060; }
  .mrow .mrow-info { flex: 1; min-width: 0; }
  .mrow .mrow-id { font-family: ui-monospace, monospace; font-size: 13px; color: #e0e0e0; overflow-wrap: anywhere; }
  .mrow .mrow-name { font-size: 12px; color: #8b8fa3; margin-top: 2px; }
  .modal-btn { border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: background 0.2s, border-color 0.2s, transform 0.15s; border: 1px solid rgba(148,163,184,0.28); background: transparent; color: var(--text-secondary); }
  .modal-btn:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; transform: translateY(-1px); }
  .modal-btn-primary { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 14px rgba(59,130,246,0.35); }
  .modal-btn-primary:hover { filter: brightness(1.1); box-shadow: 0 6px 20px rgba(34,211,238,0.4); }
  .modal-btn:active { transform: scale(0.98); }
  .modal-msg { font-size: 12.5px; color: #9ca3af; margin-right: auto; align-self: center; }
  .modal-msg.success { color: #4ade80; }
  .modal-msg.danger { color: #f87171; }
  .mrow-all-hint { font-size: 12px; color: #6b7280; margin: 4px 0 8px 0; }
  /* modal 内搜索框 */
  .model-search-wrap { margin-bottom: 10px; }
  .model-search { width: 100%; padding: 8px 12px; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 13px; transition: border-color 0.2s; }
  .model-search::placeholder { color: var(--text-tertiary); }
  .model-search:focus { outline: 2px solid rgba(34,211,238,0.35); border-color: var(--border-focus); }

  .model-msg { font-size: 12px; color: #9ca3af; margin-top: 4px; min-height: 18px; }
  .model-msg.ok { color: #4ade80; }
  .model-msg.err { color: #f87171; }

  /* ── 模型编辑：每行删除按钮 + 底部添加行 ── */
  .mrow-del { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: 15px; line-height: 1; padding: 4px 8px; border-radius: 6px; flex-shrink: 0; transition: color 0.2s, background 0.2s; }
  .mrow-del:hover { color: var(--danger); background: rgba(248,113,113,0.12); }
  .mrow-add { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .mrow-add-input { flex: 1; min-width: 0; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 7px 10px; font-size: 12.5px; font-family: var(--font-mono); transition: border-color 0.2s, box-shadow 0.2s; }
  .mrow-add-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .mrow-add-btn { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 7px 14px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; flex-shrink: 0; box-shadow: 0 3px 12px rgba(59,130,246,0.30); transition: filter 0.2s, transform 0.15s; }
  .mrow-add-btn:hover { filter: brightness(1.1); }
  .mrow-add-btn:active { transform: scale(0.98); }

  /* ── 聚合网关 / 转发配置编辑 modal ── */
  .modal-wide { max-width: 860px; }
  .agg-section { margin-bottom: 16px; }
  .agg-section-title { font-size: 11.5px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; margin-bottom: 8px; }
  .agg-hint { font-size: 11.5px; color: var(--text-tertiary); margin: 2px 0 10px; line-height: 1.5; }
  .agg-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
  .agg-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .agg-label { font-size: 11px; color: var(--text-secondary); }
  .agg-input { background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 7px 10px; font-size: 12.5px; font-family: var(--font-mono); transition: border-color 0.2s, box-shadow 0.2s; min-width: 0; }
  .agg-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .agg-vm { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px 14px; margin-bottom: 12px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); }
  /* 虚拟模型成员明细（折叠，默认收起——model-table 为主监控列表，明细避免重复） */
  .agg-vm-detail { border: 1px solid var(--border); border-radius: var(--radius-sm); margin-bottom: 8px; background: var(--bg-inset); }
  .agg-vm-detail summary { cursor: pointer; list-style: none; padding: 8px 12px; font-size: 12.5px; color: var(--text-primary); display: flex; align-items: center; gap: 8px; user-select: none; }
  .agg-vm-detail summary::-webkit-details-marker { display: none; }
  .agg-vm-detail summary .agg-vm-sum { color: var(--text-tertiary); font-size: 11.5px; }
  .agg-vm-detail summary .agg-arrow { margin-left: auto; color: var(--text-tertiary); font-size: 10px; transition: transform 0.2s; }
  .agg-vm-detail[open] summary .agg-arrow { transform: rotate(180deg); }
  .agg-vm-detail .agg-vm-body { padding: 2px 12px 10px; border-top: 1px dashed var(--border); }
  .agg-vm-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .agg-vm-id { flex: 1; min-width: 0; }
  .agg-pool { margin: 8px 0 2px 0; }
  .agg-pool-title { font-size: 11.5px; color: var(--text-tertiary); font-weight: 600; margin-bottom: 6px; }
  .agg-pool-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; }
  .agg-mem-port { width: 180px; flex-shrink: 0; }
  .agg-mem-model { flex: 1; min-width: 200px; }
  .agg-mem-weight { width: 84px; flex-shrink: 0; }
  .agg-add-row { margin: 6px 0 10px 0; }
  .agg-vm-retries { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .agg-vm-retries .agg-field { flex: 1; min-width: 140px; }

  /* ── 三个编辑 modal 共享：作用域提示条（明示"这里改什么、不改什么"）── */
  .mm-scope { display: flex; align-items: flex-start; gap: 8px; font-size: 11.5px; line-height: 1.55; color: var(--text-secondary); background: rgba(34,211,238,0.06); border: 1px solid rgba(34,211,238,0.20); border-left: 3px solid var(--brand-cyan); border-radius: var(--radius-sm); padding: 8px 12px; margin: 0 0 12px; }
  .mm-scope .mm-scope-icon { flex-shrink: 0; opacity: 0.85; }
  .mm-scope b { color: var(--text-primary); font-weight: 600; }
  .mm-scope .mm-scope-neg { color: var(--text-tertiary); }

  /* ── 悬空引用全局警示条（dashboard 顶部）── */
  .dangling-bar { display: none; background: rgba(251,191,36,0.08); border: 1px solid rgba(251,191,36,0.30); border-left: 3px solid var(--warning); border-radius: var(--radius-md); padding: 12px 16px; margin-bottom: 18px; }
  .dangling-bar.show { display: block; }
  .dangling-bar .dg-head { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--warning); margin-bottom: 6px; }
  .dangling-bar .dg-count { font-size: 11px; font-weight: 500; color: var(--text-tertiary); margin-left: auto; }
  .dangling-bar ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .dangling-bar li { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
  .dangling-bar li code { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-primary); background: rgba(148,163,184,0.12); border-radius: 4px; padding: 1px 5px; margin-right: 6px; }

  /* ── 卡片内联 token 编辑 ── */
  .token-edit { margin-top: 8px; padding-top: 8px; border-top: 1px dashed rgba(148,163,184,0.16); }
  .te-status { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .te-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .te-input { flex: 1; min-width: 120px; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 6px 8px; font-size: 13px; }
  .te-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .te-save, .te-recrack { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 6px 12px; cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: filter 0.2s, transform 0.15s, box-shadow 0.2s; box-shadow: 0 3px 12px rgba(59,130,246,0.30); }
  .te-recrack { background: transparent; color: #b6bdd0; border: 1px solid rgba(148,163,184,0.28); box-shadow: none; }
  .te-recrack:disabled { background: transparent; color: var(--text-tertiary); cursor: not-allowed; border-color: rgba(148,163,184,0.16); transform: none !important; box-shadow: none; }
  .te-recrack:disabled:hover { background: transparent; transform: none; }
  .te-save:hover { filter: brightness(1.1); box-shadow: 0 5px 18px rgba(34,211,238,0.4); transform: translateY(-1px); }
  .te-recrack:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; transform: translateY(-1px); }
  .te-save:active, .te-recrack:active { transform: scale(0.98); }

  /* ── 总览栏操作按钮 + 消息 ── */
  .ov-btn { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-size: 13px; font-weight: 600; transition: background 0.2s, border-color 0.2s, transform 0.15s, box-shadow 0.2s; }
  .ov-btn:hover { background: rgba(34,211,238,0.08); border-color: rgba(34,211,238,0.5); color: #fff; transform: translateY(-1px); }
  .ov-btn:active { transform: scale(0.98); }
  .ov-btn-primary { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 16px rgba(59,130,246,0.35); }
  .ov-btn-primary:hover { background: var(--brand-grad); filter: brightness(1.1); border: none; color: #fff; box-shadow: 0 6px 24px rgba(34,211,238,0.45); }
  .ov-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .ov-msg { font-size: 12.5px; color: var(--text-secondary); margin-left: 4px; flex-basis: 100%; text-align: right; }
  .ov-msg.success { color: var(--success); }
  .ov-msg.danger { color: var(--danger); }

  /* ── 响应式：窄屏 ≤ 768px ── */
  @media (max-width: 768px) {
    body { padding: 16px; }
    .card-toggle { padding: 14px 16px; }
    .card-toggle .ct-name { white-space: normal; overflow: visible; font-size: 14px; }
    .card-toggle .ct-port { font-size: 12px; }
    .card-toggle .ct-summary { display: none; }
    .card-detail { padding: 0 16px; }
    .card-detail.open { padding: 0 16px 14px 16px; }
    .overview-bar { gap: 14px; padding: 14px; flex-direction: column; }
    .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .kpi-value { font-size: 26px; }
    .ov-side { flex-direction: row; align-items: center; justify-content: space-between; width: 100%; }
    .kv { grid-template-columns: 1fr 2fr; }
    .stats-block { gap: 12px; }
    .model-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .te-row { flex-direction: column; align-items: stretch; }
    .te-save, .te-recrack { align-self: flex-start; }
    .model-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  }

  /* ── 动效降级 ── */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition: none !important; animation: none !important; }
  }

  /* ── 破解网关：额度/签到状态展示 ── */
  .crack-status { margin-top: 8px; padding: 8px 10px; background: var(--bg-inset); border: 1px solid rgba(148,163,184,0.12); border-radius: var(--radius-sm); font-size: 12px; line-height: 1.6; }
  .cs-loading { color: #8b93a7; }
  .cs-err { color: #f87171; }
  .cs-head { color: #9aa3b8; margin-bottom: 4px; font-weight: 600; }
  .cs-row { display: flex; justify-content: space-between; gap: 8px; color: #c9d1e3; }
  .cs-row .k { color: #8b93a7; }
  .cs-checkin-ok { color: #34d399; }
  .cs-checkin-no { color: #fbbf24; }
  .cs-never { color: #6b7280; font-style: italic; }
  .cs-quota { border-top: 1px dashed #262a3a; margin-top: 6px; padding-top: 6px; }
  .cs-qrow { display: flex; justify-content: space-between; gap: 8px; color: #b6bfd4; }
  .cs-qrow .qname { color: #8b93a7; }
  .cs-qrow .qexp { color: #6b7280; font-size: 11px; }

  /* ── 8080 聚合卡：虚拟模型/会话/熔断状态展示 ── */
  .agg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; flex-shrink: 0; }
  .agg-dot.ok { background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); }
  .agg-dot.warn { background: var(--warning); box-shadow: 0 0 6px rgba(251,191,36,0.5); }
  .agg-dot.bad { background: var(--danger); box-shadow: 0 0 6px rgba(248,113,113,0.5); }
  .agg-dot.dim { background: #4b5563; }
  .agg-vm { border-top: 1px dashed #262a3a; margin-top: 6px; padding-top: 6px; }
  .agg-vm-head { color: #7dd3fc; font-weight: 600; font-size: 12px; margin-bottom: 3px; }
  .agg-vm-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #b6bfd4; padding: 1px 0; font-size: 12px; }
  .agg-vm-row .m { color: #c9d1e3; font-family: var(--font-mono); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .agg-vm-row .s { color: #8b93a7; flex-shrink: 0; }
  .agg-brk { display: flex; align-items: center; gap: 6px; color: #c9d1e3; padding: 1px 0; font-size: 12px; }
  .agg-brk .m { font-family: var(--font-mono); font-size: 11px; color: #7dd3fc; }
  .agg-brk .reason { color: #8b93a7; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

  /* ── 凭据管理按钮 ── */
  .te-cred-btn { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 14px; cursor: pointer; font-size: 13px; font-weight: 600; transition: background 0.2s, border-color 0.2s, transform 0.15s; }
  .te-cred-btn:hover { border-color: rgba(34,211,238,0.5); color: var(--brand-cyan); background: rgba(34,211,238,0.08); transform: translateY(-1px); }

  /* ── 凭据管理弹窗（表单/JSON 双模式）── */
  .cred-modal { position: fixed; inset: 0; background: rgba(5,6,10,0.78); backdrop-filter: blur(5px); display: none; align-items: center; justify-content: center; z-index: 1000; }
  .cred-modal.open { display: flex; }
  .cred-box { background: linear-gradient(180deg, #17172a 0%, #101019 100%); border: 1px solid var(--border-strong); border-radius: 14px; padding: 18px 22px; width: 560px; max-width: 92vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 24px 70px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.05); }
  .cred-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .cred-head h3 { margin: 0; color: var(--text-primary); font-size: 16px; font-weight: 600; }
  .cred-close { background: none; border: none; color: var(--text-secondary); font-size: 20px; cursor: pointer; line-height: 1; }
  .cred-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
  .cred-tab { background: none; border: none; color: var(--text-secondary); padding: 8px 16px; cursor: pointer; border-bottom: 2px solid transparent; font-size: 13px; transition: color 0.2s; }
  .cred-tab.active { color: var(--brand-cyan); border-bottom-color: var(--brand-cyan); }
  .cred-field { margin-bottom: 12px; }
  .cred-field label { display: block; color: var(--text-primary); font-size: 13px; margin-bottom: 4px; }
  .cred-field input { width: 100%; background: var(--bg-inset); color: #d5dcea; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px; font-size: 13px; box-sizing: border-box; }
  .cred-field input:focus { outline: 2px solid rgba(34,211,238,0.30); border-color: var(--border-focus); }
  .cred-field .cred-hint { display: block; color: var(--text-tertiary); font-size: 11px; margin-top: 3px; }
  .cred-field .cred-field-err { display: block; color: var(--danger); font-size: 11px; min-height: 14px; }
  .cred-req { color: var(--danger); }
  .cred-readonly { color: var(--text-tertiary); font-size: 11px; margin-top: 8px; padding: 6px 8px; background: var(--bg-inset); border-radius: var(--radius-sm); }
  .cred-foot { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 14px; }
  .cred-msg { flex: 1; font-size: 12px; }
  .cred-msg.ok { color: #4ade80; }
  .cred-msg.err { color: #f87171; }
  .cred-msg.warn { color: #fbbf24; }
  .cred-cancel { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-weight: 600; transition: border-color 0.2s, background 0.2s; }
  .cred-cancel:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; }
  .cred-save { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-weight: 600; box-shadow: 0 4px 14px rgba(59,130,246,0.35); transition: filter 0.2s, transform 0.15s; }
  .cred-save:hover { filter: brightness(1.1); transform: translateY(-1px); }
  .cred-pane textarea { width: 100%; height: 150px; background: var(--bg-inset); color: #d5dcea; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px; font-family: monospace; font-size: 12px; box-sizing: border-box; resize: vertical; }

  /* ── 编辑器统一层级：模型 / 凭据复用聚合配置的字段语言 ── */
  .model-editor-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 12px; margin: 0 0 10px; background: linear-gradient(180deg, rgba(34,211,238,0.06), rgba(255,255,255,0.015)), var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .model-editor-summary .mrow-all-hint { margin: 0; color: var(--text-secondary); }
  .model-editor-list { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-inset); overflow: hidden; }
  .model-editor-list .mrow { padding: 10px 12px; border-color: var(--border); transition: background 0.2s, border-color 0.2s; }
  .model-editor-list .mrow:hover { background: rgba(34,211,238,0.055); }
  .model-editor-list .mrow.mrow-master { margin: 0; background: rgba(34,211,238,0.045); border-bottom-style: solid; border-bottom-color: rgba(34,211,238,0.22); }
  .model-editor-list .mrow-id { color: var(--text-primary); }
  .model-editor-list .mrow-name { color: var(--text-secondary); }
  .model-editor-add { padding-top: 2px; }
  .mm-row { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(150px, 1fr) 180px minmax(200px, 1.35fr) auto; gap: 8px; align-items: center; padding: 10px; margin-bottom: 8px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); transition: border-color 0.2s, background 0.2s; }
  .mm-row:hover { border-color: rgba(34,211,238,0.3); background: rgba(34,211,238,0.035); }
  .mm-row .agg-mem-port { width: 100%; }
  .mm-row .agg-mem-model { min-width: 0; }
  .mm-hint { font-size: 11.5px; line-height: 1.55; color: var(--text-tertiary); margin: 0 0 14px; padding: 9px 12px; border-left: 2px solid var(--brand-cyan); background: rgba(34,211,238,0.045); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
  .mm-del, .mm-add-btn { border-radius: var(--radius-sm); font-size: 12px; font-weight: 600; cursor: pointer; transition: background 0.2s, border-color 0.2s, transform 0.15s; }
  .mm-del { border: 1px solid transparent; background: transparent; color: var(--text-tertiary); padding: 6px 8px; }
  .mm-del:hover { color: var(--danger); border-color: rgba(248,113,113,0.3); background: rgba(248,113,113,0.1); }
  .mm-add-btn { border: 1px solid rgba(34,211,238,0.28); background: rgba(34,211,238,0.06); color: var(--brand-cyan); padding: 7px 12px; }
  .mm-add-btn:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.12); transform: translateY(-1px); }
  .cred-modal { padding: 20px; }
  .cred-modal .modal { max-width: 560px; max-height: 82vh; }
  .cred-modal .modal-body { padding-top: 14px; }
  .cred-tabs { gap: 0; margin: -2px 0 14px; border-bottom-color: var(--border); }
  .cred-tab { padding: 8px 14px; font-weight: 600; }
  .cred-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
  .cred-field label { display: flex; align-items: center; gap: 4px; color: var(--text-secondary); font-size: 11px; margin: 0; }
  .cred-field input, .cred-pane textarea { color: var(--text-primary); font-family: var(--font-mono); }
  .cred-field input { padding: 7px 10px; }
  .cred-field .cred-hint { margin: 0; line-height: 1.4; }
  .cred-field .cred-field-err { min-height: 0; }
  .cred-readonly { margin-top: 10px; border: 1px solid var(--border); }
  .cred-pane textarea:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  @media (max-width: 768px) {
    .modal-overlay { padding: 12px; }
    .modal-wide { max-width: 100%; }
    .mm-row { grid-template-columns: 1fr; }
    .mm-row .agg-mem-port, .mm-row .agg-mem-model { width: 100%; }
    .mm-row .mm-del { justify-self: end; }
  }
"""


def _html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_LAN_IP_CACHE: Optional[str] = None


def _get_lan_ip() -> str:
    """探测本机局域网 IP（dashboard 展示可粘贴 base_url 用）。

    优先取 UDP 出口探测（能连外网时最准），回退网卡枚举 / hostname。
    结果缓存，避免每次渲染都探测。
    """
    global _LAN_IP_CACHE
    if _LAN_IP_CACHE:
        return _LAN_IP_CACHE
    # 方法1：UDP 出口探测（不实际发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            _LAN_IP_CACHE = ip
            return ip
    except Exception:
        pass
    # 方法2：枚举网卡地址
    try:
        for ifname in ("eth0", "ens3", "enp0s3", "enp1s0", "wlan0"):
            try:
                import fcntl, struct as _st
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, _st.pack('256s', ifname[:15].encode()))[20:24])
                s.close()
                if addr and not addr.startswith("127."):
                    _LAN_IP_CACHE = addr
                    return addr
            except Exception:
                continue
    except Exception:
        pass
    # 方法3：hostname 解析
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            _LAN_IP_CACHE = ip
            return ip
    except Exception:
        pass
    _LAN_IP_CACHE = "127.0.0.1"
    return _LAN_IP_CACHE


_WORD_FIXES = {
    # Model families / brands
    'glm': 'GLM', 'deepseek': 'DeepSeek', 'minimax': 'MiniMax', 'kimi': 'Kimi',
    'hunyuan': 'Hunyuan', 'qwen': 'Qwen', 'nemotron': 'Nemotron', 'llama': 'Llama',
    'gpt': 'GPT', 'claude': 'Claude', 'mai': 'Mai', 'hy3': 'Hy3',
    # Descriptors / suffixes
    'codex': 'Codex', 'pro': 'Pro', 'flash': 'Flash', 'mini': 'Mini',
    'ultra': 'Ultra', 'super': 'Super', 'turbo': 'Turbo', 'coder': 'Coder',
    'thinking': 'Thinking', 'instruct': 'Instruct', 'chat': 'Chat',
    'modelroute': 'Model Route', 'default': 'Default',
    'image': 'Image', 'art': 'Art', 'text': 'Text', 'embedding': 'Embedding',
    'small': 'Small', 'large': 'Large', 'picker': 'Picker',
    'compaction': 'Compaction', 'trajectory': 'Trajectory',
    'maverick': 'Maverick', 'oss': 'OSS', 'sonnet': 'Sonnet',
    'haiku': 'Haiku', 'opus': 'Opus', 'night': 'Night',
    'volc': 'Volc', 'highspeed': 'HighSpeed',
}


def _humanize_model_name(mid):
    """模型 id → 人类可读名。

    规则：去 'pool-' / 'provider/' 前缀、':free' 尾缀转 '(free)'，
    '-'/'_' 转空格，已知品牌词做专名修正，版本号保持原样。
    """
    s = str(mid)
    # Strip provider/ prefix (e.g., nvidia/nemotron → nemotron)
    if '/' in s:
        s = s.split('/')[-1]
    # Strip pool- prefix (e.g., pool-deepseek-v4-pro → deepseek-v4-pro)
    if s.startswith('pool-'):
        s = s[5:]
    # Handle :free suffix
    free_note = ''
    if s.endswith(':free'):
        s = s[:-5]
        free_note = ' (free)'
    # Replace separators with space
    s = s.replace('-', ' ').replace('_', ' ')
    # Apply word fixes
    words = []
    for w in s.split():
        w_lower = w.lower()
        if w_lower in _WORD_FIXES:
            words.append(_WORD_FIXES[w_lower])
        elif w and w[0].isdigit():
            # Version-like: uppercase trailing letter-suffixes (e.g., "550b" → "550B", "a55b" → "A55B", "4v" → "4V")
            result = w.upper() if any(c.isalpha() for c in w[-3:]) else w
            words.append(result)
        else:
            words.append(w[0].upper() + w[1:] if w else w)
    return ' '.join(words) + free_note


def _format_uptime(started_at_str):
    """ISO 时间串 → 人类可读运行时长（如 '1天2小时' / '35分钟' / '12秒'）。"""
    if not started_at_str:
        return "—"
    try:
        started = datetime.fromisoformat(started_at_str)
        now = datetime.now()
        # Handle naive datetime (no tzinfo) — assume local time
        if started.tzinfo is not None and hasattr(started.tzinfo, 'utcoffset'):
            now = datetime.now(started.tzinfo)
        delta = now - started
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "—"
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if days > 0:
            return f"{days}天{hours}小时"
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        if minutes > 0:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"
    except Exception:
        return "—"


def _model_details_html(models, model_stats=None, label=None, edit_mode=False, can_prune=False, col_429="429"):
    """模型列表表格（正常态）+ 模型编辑 modal 内容（edit_mode）。

    支持 models 为字符串列表（默认启用）、dict 列表（含 id/display_name/enabled）。
    label: target label，用于编辑入口按钮的 data-label；为 None 时无编辑能力（如 8081 卡片）。
    edit_mode: True 时返回 modal 编辑界面 HTML：全部模型 + 每个模型的 iOS 风格滑动开关（无删除按钮）。
    can_prune: 该网关上游是否支持 /models（copilot 系支持；codebuddy/qclaw/trae-work 不支持，
               不显示"清理过期模型"按钮，避免点击报"上游不可达"）。
    """
    editable = label is not None
    # 规范化：统一为 [{id, display, enabled, aliases}]
    norm = []
    for m in models or []:
        if isinstance(m, dict):
            mid = m.get('id', '')
            display = m.get('display_name', '') or _humanize_model_name(mid)
            enabled = m.get('enabled', True)
            aliases = m.get('aliases') or []
        else:
            mid = str(m)
            display = _humanize_model_name(mid)
            enabled = True
            aliases = []
        if mid:
            norm.append({"id": mid, "display": display, "enabled": enabled,
                         "aliases": [str(a) for a in aliases]})

    visible = [n for n in norm if n.get("enabled", True)]

    # ── 编辑态（modal 内容）：全部模型 + 滑动开关 + 每行删除 + 底部添加行 ──
    if edit_mode:
        esc_label = _html_escape(label or "")
        enabled_count = sum(1 for n in norm if n.get("enabled", True))
        rows_html = ""
        if not norm:
            rows_html = '<div class="no-models">(暂无模型数据，在下方添加)</div>'
        for n in norm:
            checked = 'checked' if n.get("enabled", True) else ''
            rows_html += (
                f'<div class="mrow" data-model="{_html_escape(n["id"])}">'
                f'  <div class="mrow-info">'
                f'    <div class="mrow-id">{_html_escape(n["id"])}</div>'
                f'    <div class="mrow-name">{_html_escape(n["display"])}</div>'
                f'  </div>'
                f'  <label class="switch" title="展示此模型">'
                f'    <input type="checkbox" class="model-show" data-model="{_html_escape(n["id"])}" {checked}>'
                f'    <span class="switch-slider"></span>'
                f'  </label>'
                f'  <button class="mrow-del" onclick="removeModelRow(this)" title="删除此模型">×</button>'
                f'</div>'
            )
        # 底部添加模型行（自由输入新模型名，保存后进 models 列表）
        add_row = (
            '<div class="mrow-add">'
            '<input type="text" class="mrow-add-input" id="model-add-input" '
            'placeholder="输入新模型名，保存后加入列表…" aria-label="新模型名">'
            '<button class="mrow-add-btn" onclick="addModelRow()">+ 添加模型</button>'
            '</div>'
        )
        hint = f'<div class="mrow-all-hint">共 {len(norm)} 个模型，已开启 {enabled_count} 个</div>' if norm else ''
        # 总开关：全开/全关/部分开（indeterminate），联动所有子开关
        master = ""
        if norm:
            master = (
                '<div class="mrow mrow-master" id="model-master-row">'
                '  <div class="mrow-info">'
                '    <div class="mrow-id">全部模型</div>'
                '    <div class="mrow-name">总开关，一键全开 / 全关</div>'
                '  </div>'
                '  <label class="switch" title="全开/全关">'
                '    <input type="checkbox" class="model-master" '
                + ('checked' if enabled_count == len(norm) else '')
                + '>'
                '    <span class="switch-slider"></span>'
                '  </label>'
                '</div>'
            )
        # 搜索始终置顶，保证较短列表也有一致的编辑入口。
        search = (
            '<div class="model-search-wrap">'
            '<input type="text" class="model-search" placeholder="搜索模型…" '
            'oninput="filterModels(this)" aria-label="搜索模型">'
            '</div>'
        )
        # 作用域提示（docs §2.4.1）：明示本页只是该端口的透传白名单
        scope = (
            '<div class="mm-scope"><span class="mm-scope-icon">i</span><span>'
            f'<b>本页仅控制 {_html_escape(label or "")} 端口的透传白名单（开关=是否对外暴露）。</b>'
            ' <span class="mm-scope-neg">不影响其他端口，也不改变 8081 模型定义与 8080 聚合路由。</span>'
            '</span></div>'
        )
        return (
            f'{scope}{search}<div class="model-editor-summary">{master}{hint}</div>'
            f'<div class="model-editor-list">{rows_html}</div>'
            f'<div class="model-editor-add">{add_row}</div>'
            f'<div class="model-msg" data-label="{esc_label}"></div>'
        )

    # ── 正常态表格（只展示启用模型，无删除按钮）──
    if not visible:
        if not editable:
            return '<div class="no-models">(暂无模型数据)</div>'
        return (
            '<div class="no-models">(暂无展示中的模型，点击「编辑模型」开启)</div>'
            f'<div class="model-ops">'
            f'  <button class="model-edit-toggle" data-label="{_html_escape(label)}" onclick="openModelEditor(this)">✏️ 编辑模型</button>'
            f'</div>'
        )

    has_stats = model_stats is not None
    esc_label = _html_escape(label or "")
    # 别名列：数据源含 aliases 字段即渲染（8081 卡片 models[] 定义始终显示，与编辑视图一致；
    # 空别名显示 —。其他 target 卡片无 aliases 字段则不显示，避免无谓加宽）
    has_alias_col = any("aliases" in n for n in visible)

    rows = []
    for i, n in enumerate(visible, 1):
        mid = n["id"]
        row = (
            f'<tr data-model="{_html_escape(mid)}">'
            f'<td class="num">{i}</td>'
            f'<td class="mid"><code>{_html_escape(mid)}</code></td>'
            f'<td class="name">{_html_escape(n["display"])}</td>'
        )
        if has_alias_col:
            alias_txt = ", ".join(_html_escape(a) for a in n.get("aliases") or [])
            row += f'<td class="alias">{alias_txt or "—"}</td>'
        if has_stats:
            ms = model_stats.get(mid) if mid else None
            if ms:
                total = ms.get("requests", 0)
                ok = ms.get("ok", 0)
                err = ms.get("err", 0)
                tr429 = ms.get("translated429", 0)
                rate = round(ok / total * 100, 1) if total > 0 else 100.0
                row += (
                    f'<td class="mstat">{total}</td>'
                    f'<td class="mstat">{rate}%</td>'
                    f'<td class="mstat err">{err}</td>'
                    f'<td class="mstat warn">{tr429}</td>'
                )
            else:
                row += '<td class="mstat">—</td><td class="mstat">—</td><td class="mstat">—</td><td class="mstat">—</td>'
        row += '</tr>'
        rows.append(row)

    alias_th = '<th>别名</th>' if has_alias_col else ''
    header_extra = f'<th>请求</th><th>成功率</th><th>错误</th><th>{_html_escape(str(col_429))}</th>' if has_stats else ''
    table_html = (
        f'<table class="model-table">'
        f'<thead><tr><th>#</th><th>模型 ID</th><th>名称</th>{alias_th}{header_extra}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
    )

    if not editable:
        return table_html

    # 可编辑：正常态表格 + 编辑入口 + 消息区
    edit_toggle = (
        f'<button class="model-edit-toggle" data-label="{esc_label}" onclick="openModelEditor(this)">✏️ 编辑模型</button>'
    )
    prune_toggle = ""
    if can_prune:
        prune_toggle = (
            f'<button class="model-prune-btn" data-label="{esc_label}" onclick="pruneModels(this)" '
            f'title="对照上游最新模型列表，删除已下线的过期模型（同步配置与内存）">'
            f'🧹 清理过期模型</button>'
        )
    return (
        f'{table_html}'
        f'<div class="model-ops">'
        f'  {edit_toggle}'
        f'  {prune_toggle}'
        f'</div>'
        f'<div class="model-msg" data-label="{esc_label}"></div>'
    )


def _build_card_html(name, note, kind_badge, status_badge, status_badge_class,
                     kv_items, stats_detail=None, models=None, model_stats=None, description="",
                     accent_class="", raw_html="", label=None, port=None, meta_badges=None,
                     can_prune=False, col_429="429"):
    """统一卡片渲染（手风琴折叠）：透传目标和定制服务用同一套视觉风格。

    stats_detail: dict with total/ok/err/translated/success_rate/uptime
    accent_class: CSS class for port-specific accent (e.g., 'accent-8082')
    label: target label，传递给模型编辑组件；None 时不显示编辑按钮
    port: 端口号，显示在卡片头
    meta_badges: 额外的分类标签列表 [("文本", "样式类"), ...]，如 [("破解", "b-crack"), ("免费", "b-free")...]
    can_prune: 上游是否支持 /models 清理（copilot 系 true；codebuddy/qclaw/trae-work false 不显示清理按钮）
    """
    # ── 卡片头 badges（分类 badge 带图标点 + 渐变底；状态 badge 带状态点）──
    kind_badge_class = {"破解": "b-crack", "免费": "b-free", "收费": "b-paid"}.get(str(kind_badge), "b-generic")
    badges = f'<span class="badge {kind_badge_class}"><span class="badge-dot"></span>{_html_escape(str(kind_badge))}</span>'
    # 元数据标签：破解/非破解、免费/收费、稳定性
    for meta_text, meta_cls in (meta_badges or []):
        badges += f' <span class="badge {meta_cls}">{_html_escape(str(meta_text))}</span>'
    if status_badge:
        # status_badge_class 可能是 'purple'/'blue'/'green'/'red'/'orange'/'gray' 等 → 映射为 b-status-*
        st_class = {"blue": "b-st-blue", "green": "b-st-green", "red": "b-st-red",
                    "yellow": "b-st-yellow", "purple": "b-st-purple", "orange": "b-st-orange",
                    "gray": "b-st-gray"}.get(str(status_badge_class), "b-st-gray")
        badges += f' <span class="badge {st_class}">{_html_escape(str(status_badge))}</span>'

    # ── 卡片头摘要（请求数）──
    summary = ""
    if stats_detail and stats_detail.get('alive'):
        total = stats_detail.get('total', 0)
        summary = f'<span class="ct-summary">{total} 请求</span>'

    # ── 卡片头 HTML ──
    port_str = f'<span class="ct-port">:{port}</span>' if port else ''
    # 启动状态灯：绿=运行中/红=离线/黄=未监听，带呼吸动画
    lamp_cls = {"blue": "on", "green": "on", "purple": "on", "orange": "on", "red": "off", "gray": "idle", "yellow": "idle"}.get(str(status_badge_class), "idle")
    lamp = f'<span class="ct-lamp {lamp_cls}" title="{_html_escape(str(status_badge))}"></span>'
    header_html = (
        f'<div class="card-toggle" role="button" tabindex="0" aria-expanded="false">'
        f'  {lamp}'
        f'  <span class="ct-name">{_html_escape(name)}</span>{port_str}'
        f'  <span class="badge-group">{badges}</span>'
        f'{summary}'
        f'  <span class="ct-arrow">▼</span>'
        f'</div>'
    )

    # ── 详情区 kv ──
    kv = "".join(
        f"<div>{_html_escape(str(k))}</div><div><code>{_html_escape(str(v))}</code></div>"
        for k, v in kv_items
    )

    # ── 流量统计块（含进度条）──
    stats_html = ""
    if stats_detail and stats_detail.get('alive'):
        ok = stats_detail.get('ok', 0)
        err = stats_detail.get('err', 0)
        tr = stats_detail.get('translated', 0)
        total = stats_detail.get('total', 0)
        success_rate = stats_detail.get('success_rate', 0)
        uptime = stats_detail.get('uptime', '—')

        # 进度条
        bar_html = ""
        if total > 0:
            ok_pct = round(ok / total * 100, 1)
            tr_pct = round(tr / total * 100, 1)
            err_pct = round(err / total * 100, 1)
            bar_html = (
                f'<div class="rate-bar">'
                + (f'<div class="rate-bar-seg ok" style="width:{ok_pct}%" title="成功 {ok}"></div>' if ok_pct > 0 else '')
                + (f'<div class="rate-bar-seg tr429" style="width:{tr_pct}%" title="429 翻译 {tr}"></div>' if tr_pct > 0 else '')
                + (f'<div class="rate-bar-seg err" style="width:{err_pct}%" title="错误 {err}"></div>' if err_pct > 0 else '')
                + f'</div>'
            )

        stats_html = (
            f'<div class="stats-block">'
            f'<div class="stat-item"><span class="stat-label">总请求</span><span class="stat-value">{total}</span></div>'
            f'<div class="stat-item"><span class="stat-label">成功率</span><span class="stat-value">{success_rate}%</span></div>'
            f'<div class="stat-item"><span class="stat-label">运行时长</span><span class="stat-value">{uptime}</span></div>'
            f'</div>'
            f'{bar_html}'
            f'<div class="mini-stats">'
            f'  <div>正常透传 <b>{ok}</b></div>'
            f'  <div>翻译 429 <b>{tr}</b></div>'
            f'  <div>代理错误 <b>{err}</b></div>'
            f'</div>'
        )

    model_html = _model_details_html(models, model_stats, label, edit_mode=False, can_prune=can_prune, col_429=col_429) if models is not None else ""
    card_class = f'card {accent_class}'.strip()

    return f"""<div class="{card_class}" data-label="{_html_escape(label or '')}" data-port="{port or ''}">
  {header_html}
  <div class="card-detail">
  <div class="kv">{kv}</div>
  {f'<div class="card-desc">{_html_escape(description)}</div>' if description else ""}
  {stats_html}
  {f'<div class="model-section" data-label="{_html_escape(label or "")}">{model_html}</div>' if model_html else ""}
  {raw_html}
  </div>
</div>"""


# ══════════════════════════════════════════════════════════════════════════════
# Task 8: 管理 REST API（dashboard 配置管理）
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/targets")
async def api_targets():
    """返回全部 target 配置 + secrets 元信息（key 打码）+ 统计 + 破解环境检测。"""
    result = []
    for t in _TARGETS:
        secret = _cfg.resolve_secret(t, _SECRETS)
        item = {
            **t,
            "secretSet": bool(secret),
            "secretMasked": _cfg.mask_secret(secret),
            "stats": _TARGET_STATS.get(t["label"], {}),
        }
        if t.get("category") == "crack" and t.get("crackTool"):
            item["crackEnv"] = _crack_env_check(t)
        result.append(item)
    return {
        "anthropicForwardPort": _MODELS_CFG["modelDefaults"].get("defaultPort", 8082),
        "targets": result,
    }


@app.get("/api/models")
async def api_get_models():
    """返回全局模型定义（models[] + modelDefaults）。"""
    return {
        "models": _MODELS_CFG.get("models", []),
        "modelDefaults": _MODELS_CFG.get("modelDefaults", {"defaultPort": 8082}),
    }


class ModelsUpdate(BaseModel):
    models: Optional[List] = None
    modelDefaults: Optional[Dict] = None


@app.put("/api/models")
async def api_update_models(update: ModelsUpdate):
    """更新全局模型定义，写 targets.json 顶层 models[]/modelDefaults 并热重载。"""
    cfg = _cfg.load_targets()
    if update.models is not None:
        cfg["models"] = update.models
    if update.modelDefaults is not None:
        cfg["modelDefaults"] = update.modelDefaults
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True}


def _scan_dangling_refs() -> List[dict]:
    """扫描配置中的悬空引用（引用了不存在的端口 / 虚拟模型 / 模型名）。

    只读诊断，不修改任何配置。改名后引用方不联动是有意设计（见
    docs/config-capability-unification.md §5「明确不做的事」第 4 条：不做自动改名
    联动），本函数负责把"引用断了"这件事显式暴露到 dashboard 顶部警示条，
    而不是让用户在请求失败时才发现。

    检查两类引用：
      1. 顶层 models[].target → {port, model}：端口是否存在、该端口是否提供此模型
      2. aggregator.virtualModels[vm].defaultPool/fallbackPool[] → {port, model}：同上

    端口集合含所有 enabled target 的 listenPort；聚合网关（8080）的"模型"为其
    virtualModels 的 key（agg:xxx），故链式聚合引用也能正确校验。

    模型名校验采取保守策略：仅当该端口**显式配置了非空白名单**时才判定模型悬空
    （空 models[] 表示不限制透传，任何模型名都合法，不应误报）。

    返回 [{"path": ..., "msg": ...}]，path 形如 models[2].target 便于前端定位。
    """
    items: List[dict] = []
    # 端口 → 该端口可被请求的模型名集合（None 表示不限制，不做模型级校验）
    port_models: Dict[int, Optional[set]] = {}
    port_labels: Dict[int, str] = {}
    for t in _TARGETS:
        port_num = t.get("listenPort")
        if port_num is None:
            continue
        port_labels[port_num] = t.get("label") or t.get("name") or str(port_num)
        if t.get("handler") == "aggregator":
            port_models[port_num] = set((t.get("virtualModels") or {}).keys())
            continue
        names = set()
        for m in (t.get("models") or []):
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name")
                if mid:
                    names.add(mid)
            elif isinstance(m, str):
                names.add(m)
        # 空白名单 = 不限制透传，模型级校验跳过（None 而非空集合，避免全量误报）
        port_models[port_num] = names or None

    def _check(path: str, ref: dict, what: str) -> None:
        port = ref.get("port")
        model = ref.get("model")
        if port is None:
            return
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            items.append({"path": path, "msg": f"{what} 的端口 {port!r} 不是合法端口号"})
            return
        if port_i not in port_models:
            items.append({"path": path, "msg": f"{what} 指向端口 {port_i}，但该端口未在 targets.json 中定义（或已禁用）"})
            return
        known = port_models[port_i]
        if model and known is not None and model not in known:
            plabel = port_labels.get(port_i, str(port_i))
            items.append({"path": path, "msg": f"{what} 指向 {port_i}（{plabel}）的模型 {model}，但该端口未提供此模型"})

    for idx, m in enumerate(_MODELS_CFG.get("models", []) or []):
        if not isinstance(m, dict):
            continue
        ref = m.get("target")
        if isinstance(ref, dict):
            _check(f"models[{idx}].target", ref, f"模型定义 {m.get('name') or idx}")

    for t in _TARGETS:
        if t.get("handler") != "aggregator":
            continue
        for vmid, vm in (t.get("virtualModels") or {}).items():
            if not isinstance(vm, dict):
                continue
            for pool_key in ("defaultPool", "fallbackPool"):
                for i, mem in enumerate(vm.get(pool_key) or []):
                    if isinstance(mem, dict):
                        _check(f"virtualModels.{vmid}.{pool_key}[{i}]", mem, f"虚拟模型 {vmid} 的{'默认池' if pool_key == 'defaultPool' else '降级池'}成员")

    return items


@app.get("/api/config/dangling")
async def api_get_dangling():
    """只读：返回配置中的悬空引用列表，供 dashboard 顶部警示条展示。"""
    return {"items": _scan_dangling_refs()}


@app.get("/api/aggregate/config")
async def api_get_aggregate_config():
    """返回聚合网关（handler=aggregator）target 的可编辑配置。"""
    target = next((t for t in _TARGETS if t.get("handler") == "aggregator"), None)
    if target is None:
        return {"configured": False}
    # 聚合可用端口列表（用于前端下拉选择 + 联动模型过滤）
    available_ports = {}
    for t in _TARGETS:
        port_num = t.get("listenPort")
        if port_num is None:
            continue
        if t.get("handler") == "aggregator":
            # 聚合网关：模型 = virtualModels 的 key（agg:xxx 虚拟模型 id），
            # 供模型定义/聚合配置编辑里"选 8080 → 联动列出虚拟模型"使用。
            vm_models = list((t.get("virtualModels") or {}).keys())
            available_ports[str(port_num)] = {
                "label": t.get("name") or t.get("label") or str(port_num),
                "handler": "aggregator",
                "models": vm_models,
            }
            continue
        models = []
        for m in (t.get("models") or []):
            if isinstance(m, dict):
                if m.get("enabled", True):
                    models.append(m.get("id") or m.get("name", ""))
            elif isinstance(m, str):
                models.append(m)
        available_ports[str(port_num)] = {
            "label": t.get("label") or t.get("name") or str(port_num),
            "handler": t.get("handler"),
            "models": models,
        }
    return {
        "configured": True,
        "name": target.get("name") or target.get("label"),
        "virtualModels": target.get("virtualModels", {}),
        "poolDefaults": target.get("poolDefaults", {}),
        "quotaErrorPatterns": target.get("quotaErrorPatterns", []),
        "availablePorts": available_ports,
    }


class AggregateConfigUpdate(BaseModel):
    name: Optional[str] = None
    virtualModels: Optional[Dict[str, dict]] = None
    poolDefaults: Optional[Dict[str, object]] = None
    quotaErrorPatterns: Optional[List[str]] = None


@app.put("/api/aggregate/config")
async def api_update_aggregate_config(update: AggregateConfigUpdate):
    """更新聚合网关虚拟模型/池默认值/配额熔断特征，写 targets.json 并热重载（引擎自动 reload）。"""
    cfg = _cfg.load_targets()
    target = next((t for t in cfg["targets"] if t.get("handler") == "aggregator"), None)
    if target is None:
        raise HTTPException(status_code=404, detail="未配置聚合网关 target（handler=aggregator）")
    if update.name is not None:
        target["name"] = update.name
    if update.virtualModels is not None:
        target["virtualModels"] = update.virtualModels
    if update.poolDefaults is not None:
        target["poolDefaults"] = update.poolDefaults
    if update.quotaErrorPatterns is not None:
        target["quotaErrorPatterns"] = update.quotaErrorPatterns
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True}


class TargetUpdate(BaseModel):
    label: Optional[str] = None
    listenPort: Optional[int] = None
    category: Optional[str] = None
    handler: Optional[str] = None
    isFree: Optional[bool] = None
    enabled: Optional[bool] = None
    targetHost: Optional[str] = None
    targetPort: Optional[int] = None
    targetProtocol: Optional[str] = None
    routePrefix: Optional[str] = None
    models: Optional[List] = None
    crackTool: Optional[str] = None
    secretRef: Optional[str] = None
    apikeyEnv: Optional[str] = None


@app.put("/api/targets/{label}")
async def api_update_target(label: str, update: TargetUpdate):
    """更新 target 非私密字段，写 targets.json 并热重载。"""
    cfg = _cfg.load_targets()
    for t in cfg["targets"]:
        if t["label"] == label:
            payload = update.model_dump(exclude_none=True)
            payload.pop("label", None)
            # ── 防御：过滤总开关等 UI 辅助行（旧版 bug 会混入 id="全部模型"）──
            if "models" in payload and isinstance(payload["models"], list):
                payload["models"] = [
                    m for m in payload["models"]
                    if not (isinstance(m, dict) and m.get("id") == "全部模型")
                    and not (isinstance(m, str) and m == "全部模型")
                ]
            t.update(payload)
            break
    else:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True, "label": label}


@app.post("/api/targets/{label}/prune-models")
async def api_prune_models(label: str):
    """清理过期模型：拉取下游最新模型列表，删除 targets.json 中不在最新列表的模型。

    对照最新模型列表（_fetch_live_models 优先，失败则返回 422），把配置中
    「最新列表不存在」的模型从 targets.json 移除并热重载（含内存 _TARGETS）。
    返回删除的模型列表。
    """
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    live = await _fetch_live_models(target)
    if not live:
        raise HTTPException(status_code=422, detail="无法拉取下游最新模型列表（上游不可达），无法清理")
    live_set = set(live)
    cfg = _cfg.load_targets()
    cfg_target = next((t for t in cfg["targets"] if t["label"] == label), None)
    if cfg_target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    # 模型定义保护：遍历全局 models[] 中 target.port 落在本 target 的记录，
    # 其 target.model 若在上游不存在则修正为同族可用模型（agg: 开头的聚合虚拟
    # 模型跳过，非上游模型）。保护后的 target.model 集合用于 kept/removed 判定，
    # 避免把仍被模型定义引用的模型误删。不落盘 cfg 内的修正，仅用于本次判定。
    cfg_models = cfg.get("models", [])
    protected_set = set()
    for rec in cfg_models:
        if not (isinstance(rec, dict) and isinstance(rec.get("target"), dict)):
            continue
        if int(rec["target"].get("port", -1)) != cfg_target["listenPort"]:
            continue
        tm = rec["target"].get("model")
        if not tm or str(tm).startswith("agg:"):
            continue
        if tm in live_set:
            protected_set.add(tm)
        else:
            fallback = None
            if any("haiku" in mm for mm in live_set):
                fallback = next((mm for mm in live_set if "haiku" in mm), None)
            elif any("sonnet" in mm for mm in live_set):
                fallback = next((mm for mm in live_set if "sonnet" in mm), None)
            if fallback:
                protected_set.add(fallback)
    removed = []
    kept = []
    for m in cfg_target.get("models", []):
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if mid and mid in live_set:
            kept.append(m)
        elif mid and mid in protected_set:
            kept.append(m)
        else:
            removed.append(mid)
    if removed:
        cfg_target["models"] = kept
        errors = _cfg.validate_targets(cfg)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        _cfg.save_targets(cfg)
        await _reload_targets()
    return {"ok": True, "label": label, "removed": removed, "keptCount": len(kept)}


@app.get("/api/targets/{label}/mapping")
async def api_target_mapping(label: str):
    """返回下拉数据源候选：本 target 模型列表 + 聚合虚拟模型列表。"""
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    model_ids = []
    for m in target.get("models", []) or []:
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if mid:
            model_ids.append(mid)
    agg_models = []
    for t in _TARGETS:
        if t.get("handler") == "aggregator" and t.get("enabled", True):
            agg_models.extend(sorted((t.get("virtualModels") or {}).keys()))
    return {
        "label": label,
        "models": model_ids,
        "aggModels": agg_models,
    }


class SecretUpdate(BaseModel):
    value: str = ""


@app.put("/api/secrets/{label}")
async def api_update_secret(label: str, update: SecretUpdate):
    """更新 target 的私密 key/token，写 secrets.json 并热加载。"""
    cfg = _cfg.load_targets()
    target = next((t for t in cfg["targets"] if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    # 无 secretRef 的直连网关（free/paid）统一落到约定 key f"{label}_token"，
    # 与 config_store.resolve_secret 的读取约定一致（存得进也读得出）
    ref = _cfg.secret_key_for(target)
    if not ref:
        raise HTTPException(status_code=422, detail=f"target '{label}' 无法确定 secrets key")
    secrets = _cfg.load_secrets()
    if update.value:
        secrets[ref] = update.value
    else:
        secrets.pop(ref, None)
    _cfg.save_secrets(secrets)
    _refresh_secrets()
    return {"ok": True, "label": label, "secretRef": ref, "secretSet": bool(update.value)}


class SecretBulkUpdate(BaseModel):
    """批量导入私密数据（破解网关多字段：token/refreshToken/userId 等）。"""
    data: Dict[str, str] = Field(default_factory=dict)


@app.put("/api/secrets/{label}/bulk")
async def api_update_secret_bulk(label: str, update: SecretBulkUpdate):
    """批量导入破解网关凭据（dashboard 表单/JSON 双模式提交），按 schema 校验。

    校验规则（来自 crack_common.CREDENTIAL_SCHEMAS）：
      - 字段映射：原始名（token/refreshToken/...）→ secrets 名，或直接 secrets 名
      - pattern 校验：字段定义了正则则必须匹配，否则 422
      - 未知字段：报错（避免"保存了但没生效"的困惑）
      - 只读字段（readonlyFields）：忽略不写入
    """
    import re as _re
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    schema = crack_common.CREDENTIAL_SCHEMAS.get(label) if crack_common else None
    if schema is None:
        raise HTTPException(status_code=422, detail=f"网关 '{label}' 无凭据 schema")

    field_keys = {f["key"] for f in schema["fields"]}
    import_mapping = schema.get("jsonImportMapping", {})
    readonly = set(schema.get("readonlyFields", []))
    patterns = {f["key"]: f.get("pattern") for f in schema["fields"]}
    required_keys = {f["key"] for f in schema["fields"] if f.get("required")}

    secrets = _cfg.load_secrets()
    errors: list[str] = []
    count = 0
    for k, v in update.data.items():
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        # 字段映射：直接 secrets 名，或原始名 → secrets 名
        secret_key = k if k in field_keys else import_mapping.get(k)
        if secret_key is None:
            errors.append(f"未知字段 '{k}'（该网关 schema 无此字段）")
            continue
        if secret_key in readonly:
            continue  # 只读字段（查询结果）忽略
        pat = patterns.get(secret_key)
        if pat:
            try:
                if not _re.match(pat, v):
                    errors.append(f"字段 '{secret_key}' 格式不符")
                    continue
            except _re.error:
                pass  # pattern 非法则跳过校验
        secrets[secret_key] = v
        count += 1

    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    if count == 0:
        raise HTTPException(status_code=422, detail="未识别到有效字段（token/refreshToken 等）")
    _cfg.save_secrets(secrets)
    _refresh_secrets()
    # 判定主 token 是否已配置（优先必填字段第一个）
    main_key = next(iter(sorted(required_keys)), f"{label.replace('-', '_')}_token")
    return {"ok": True, "label": label, "imported": count, "secretSet": bool(secrets.get(main_key))}


@app.get("/api/aggregate/status")
async def api_aggregate_status():
    """聚合网关运行时状态：虚拟模型 per-member 统计、会话粘性命中率、熔断端口。不含任何密钥。"""
    engine = _AGGREGATOR_ENGINE
    if engine is None:
        return {"configured": False}
    return {"configured": True, **engine.get_stats()}


@app.get("/api/crack/{label}/status")
async def api_crack_status(label: str):
    """破解网关状态查询：额度明细（含过期时间）+ 签到状态 + token 有效期。

    由 crack_common.CRACK_STATUS_HANDLERS 按 label 分发（trae-work 已实现，
    codebuddy/qclaw 待接入）。
    """
    if crack_common is None:
        raise HTTPException(status_code=503, detail="crack_common 模块不可用")
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    return crack_common.get_crack_status(label, _SECRETS)


@app.get("/api/crack/{label}/schema")
async def api_crack_schema(label: str):
    """返回该网关的凭据 schema（供 dashboard 凭据弹窗动态渲染表单）。"""
    if crack_common is None:
        raise HTTPException(status_code=503, detail="crack_common 模块不可用")
    schema = crack_common.CREDENTIAL_SCHEMAS.get(label)
    if schema is None:
        raise HTTPException(status_code=404, detail=f"网关 '{label}' 无凭据 schema")
    return schema


@app.post("/api/targets/{label}/recrack")
async def api_recrack(label: str):
    """触发破解工具重新提取 token。"""
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    tool = target.get("crackTool")
    if not tool:
        raise HTTPException(status_code=422, detail=f"target '{label}' 无 crackTool")
    ok = _run_crack_tool(tool)
    if not ok:
        return {"ok": False, "label": label, "message": "破解工具执行失败，请查看日志或手工填写"}
    return {"ok": True, "label": label, "message": "破解工具执行成功"}


@app.post("/api/reload")
async def api_reload():
    changes = await _reload_targets()
    return {"ok": True, "changes": changes}


async def _fetch_live_models(target: dict):
    """从下游网关拉取真实模型列表（OpenAI 格式，data[].id）。

    编辑弹框用：与 copilot 一致，展示下游真实可用模型。
    返回模型 id 列表；拉取失败（无 key/超时/非 200）返回 None，调用方降级。
    gemini-native handler：走 Google 原生 /v1beta/models，解析 models[].name。
    """
    host = target.get("targetHost") or ""
    if not host:
        return None
    protocol = target.get("targetProtocol", "https")
    port = target.get("targetPort", 443)
    prefix = target.get("routePrefix", "")
    url = f"{protocol}://{host}:{port}{prefix}/models"
    headers = {}
    secret = _cfg.resolve_secret(target, _SECRETS)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    for k, v in (target.get("extraHeaders") or {}).items():
        headers[k] = v
    is_gemini_native = target.get("handler") == "gemini-native"
    if is_gemini_native:
        headers.pop("Authorization", None)
        if secret:
            headers["x-goog-api-key"] = secret
        url = f"{_GEMINI_NATIVE_BASE}/models"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), trust_env=False) as c:
            resp = await c.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            ids = []
            if is_gemini_native:
                for m in (data.get("models", []) or []):
                    nm = m.get("name", "") if isinstance(m, dict) else ""
                    if nm.startswith("models/"):
                        ids.append(nm[len("models/"):])
            else:
                items = data.get("data", []) if isinstance(data, dict) else []
                for m in items:
                    if isinstance(m, dict) and m.get("id"):
                        ids.append(m["id"])
                    elif isinstance(m, str):
                        ids.append(m)
            return ids or None
    except Exception as e:
        logger.debug(f"_fetch_live_models {url} failed: {e}")
        return None


@app.get("/api/targets/{label}/models", response_class=HTMLResponse)
async def api_target_models_html(label: str, edit: int = 0):
    """返回单个 target 的模型区 HTML（edit=1 时渲染编辑态：全部模型 + 展示开关）。

    供 dashboard 前端「编辑模型」切换时无整页刷新重渲染。
    edit=1 时优先从下游 /models 拉取真实模型列表（与 copilot 一致），
    拉取失败则降级为 targets.json 配置的 models。
    """
    from fastapi.responses import HTMLResponse as _HR
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    models = target.get("models", [])
    if edit:
        live = await _fetch_live_models(target)
        if live:
            # 合并：以下游为准，保留 targets.json 中已存在的 enabled 状态
            local = {}
            for m in models:
                if isinstance(m, dict):
                    local[m.get("id", "")] = m.get("enabled", True)
                else:
                    local[str(m)] = True
            merged, seen = [], set()
            for mid in live:
                merged.append({"id": mid, "enabled": local.get(mid, True)})
                seen.add(mid)
            for mid, en in local.items():
                if mid and mid not in seen:
                    merged.append({"id": mid, "enabled": en})
            models = merged
    stats = _TARGET_STATS.get(label, {})
    html = _model_details_html(
        models,
        model_stats=_MODEL_STATS.get(label, {}),
        label=label,
        edit_mode=bool(edit),
    )
    return _HR(html)


# ══════════════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """统一管理面板 — 展示本机所有 LLM 相关服务的架构与状态。"""

    # ── 并行拉取各 asyncio TCP 端口的状态 ──
    async def _fetch(port):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as c:
                info_r = await c.get(f"http://127.0.0.1:{port}/__proxy_info__")
                stats_r = await c.get(f"http://127.0.0.1:{port}/__proxy_stats__")
                info = info_r.json() if info_r.status_code == 200 else {}
                stats = stats_r.json() if stats_r.status_code == 200 else {}
                return {
                    "label": info.get("label", f"port-{port}"),
                    "listenPort": port, "upstream": f"{info.get('targetProtocol','https')}://{info.get('targetHost','?')}:{info.get('targetPort',443)}",
                    "models": info.get("models", []),
                    "total": stats.get("totalRequests", 0),
                    "ok": stats.get("passthroughOk", 0),
                    "translated": stats.get("translated429", 0),
                    "err": stats.get("passthroughError", 0),
                    "alive": info_r.status_code == 200,
                    "startedAt": stats.get("startedAt", ""),
                    "modelStats": stats.get("modelStats", {}),
                }
        except Exception:
            return {"label": f"port-{port}", "listenPort": port, "upstream": "?", "models": [], "total": 0, "ok": 0, "translated": 0, "err": 0, "alive": False, "startedAt": "", "modelStats": {}}

    _dash_ports = [t["listenPort"] for t in _TARGETS if t.get("enabled", True)]
    results = await asyncio.gather(*[_fetch(p) for p in _dash_ports]) if _dash_ports else []
    _result_map = {r["listenPort"]: r for r in results}

    def _make_stats_detail(r):
        """构建增强统计字典（成功率、时长、进度条数据）。"""
        if not r["alive"]:
            return None
        total = r["total"]
        ok = r["ok"]
        err = r["err"]
        tr = r.get("translated", 0)
        success_rate = round(ok / total * 100, 1) if total > 0 else 100.0
        uptime = _format_uptime(r.get("startedAt", ""))
        return {
            "total": total, "ok": ok, "err": err, "translated": tr,
            "success_rate": success_rate, "uptime": uptime, "alive": True,
        }

    # ── 总览数据 ──
    total_requests_all = sum(r["total"] for r in results if r["alive"])
    alive_ports = sum(1 for r in results if r["alive"])
    alive_rate = round(alive_ports / len(results) * 100) if results else 0
    _alive_color = "#34d399" if alive_rate == 100 else ("#fbbf24" if alive_rate >= 50 else "#f87171")

    # ── 局域网 IP（可粘贴 base_url 用）──
    _lan_ip = _get_lan_ip()

    # ── 分组：聚合网关(8081) / 破解网关(crack) / 直连网关(free/paid) ──
    agg_cards, crack_cards, direct_cards = [], [], []

    # ── 8080 流量聚合（AggregatorEngine：虚拟模型路由 + 会话粘性 + 熔断）──
    # 监控视角与其他卡片一致：卡头请求数摘要 + 展开区流量统计块 + 成员级 model-table 单表
    # （虚拟模型 + 成员明细整合为一张表：模型 ID 列=虚拟模型，名称列=成员，列与监控表一致 + 延迟）
    # 配置编辑走「✏️ 编辑配置」进入独立 modal；单表由前端 fetch /api/aggregate/status 每 10s 渲染
    _agg_engine = _AGGREGATOR_ENGINE
    _agg_configured = _agg_engine is not None
    _agg_stats_detail = None
    _agg_vm_list = []
    _agg_member_total = 0
    _agg_started_at = 0
    _agg_pool_cfg_json = "{}"
    if _agg_configured:
        # 虚拟模型列表与池成员数取自配置（总有值），统计取自引擎（无流量时计数为 0）
        _agg_cfg_target = next((t for t in _TARGETS if t.get("handler") == "aggregator"), None)
        _agg_cfg_vms = (_agg_cfg_target or {}).get("virtualModels", {})
        _agg_vm_list = list(_agg_cfg_vms.keys())
        _agg_member_total = sum(
            len(v.get("defaultPool") or []) + len(v.get("fallbackPool") or [])
            for v in _agg_cfg_vms.values()
        )
        _agg_full = _agg_engine.get_stats()
        _agg_vms = _agg_full.get("virtual_models", {})
        _agg_tot = _agg_ok = _agg_err = _agg_tr = 0
        for _vm_id in _agg_vm_list:
            _members = _agg_vms.get(_vm_id, {})
            for _m in _members.values():
                _agg_tot += _m.get("requests", 0)
                _agg_ok += _m.get("ok", 0)
                _agg_err += _m.get("err", 0)
                _agg_tr += _m.get("degraded", 0)
        _agg_started_at = _agg_full.get("started_at", 0)
        _agg_rate = round(_agg_ok / _agg_tot * 100, 1) if _agg_tot > 0 else 100.0
        # _format_uptime 接受 ISO 字符串（与 8082 等透传卡一致）；引擎返回 float timestamp，调用点转换
        _agg_started_iso = datetime.fromtimestamp(_agg_started_at).isoformat() if _agg_started_at else ""
        _agg_stats_detail = {
            "total": _agg_tot, "ok": _agg_ok, "err": _agg_err,
            "translated": _agg_tr, "success_rate": _agg_rate,
            "uptime": _format_uptime(_agg_started_iso), "alive": True,
        }
        # 池配置 JSON 注入前端（供 loadAggregateStatus 渲染池详情折叠）
        _agg_pool_cfg_json = json.dumps(_agg_cfg_vms, ensure_ascii=False)
    agg_cards.append(_build_card_html(
        name="流量聚合",
        note="虚拟模型聚合路由 · 会话粘性 · 熔断降级（OpenAI /v1 入口）",
        kind_badge="聚合网关",
        status_badge="运行中" if _agg_configured else "未配置",
        status_badge_class="green" if _agg_configured else "gray",
        kv_items=[
            ("base_url", f"http://{_lan_ip}:8080"),
            ("监听地址", "http://0.0.0.0:8080"),
            ("协议", "OpenAI /v1（虚拟模型 agg:xxx）"),
            ("路由策略", "权重/会话粘性 · 失败降级 · 熔断摘除"),
            ("虚拟模型", f"{len(_agg_vm_list)} 个"),
            ("池成员", f"{_agg_member_total} 个"),
        ],
        stats_detail=_agg_stats_detail,
        # 模型区整合：虚拟模型 + 成员明细统一由前端渲染单表（loadAggregateStatus），服务端不再输出 model-table
        models=None,
        model_stats=None,
        col_429="降级",
        description="虚拟模型 id（agg:xxx）→ 按权重与会话粘性路由到池内成员端口，故障端口自动熔断并从降级池逃生。",
        accent_class="accent-8080",
        label=None,
        port=8080,
        meta_badges=[("熔断降级", "b-meta-normal"), ("OpenAI 协议", "b-meta-oa")],
        raw_html=(
            '<div class="model-ops">'
            '  <button class="model-edit-toggle" onclick="openAggConfigEditor(this)" '
            '    title="编辑聚合网关虚拟模型 / 池默认值 / 重试策略">✏️ 编辑配置</button>'
            '</div>'
            '<div class="crack-status" id="agg-status" data-ref="aggregate">'
            '  <div class="cs-loading">状态加载中…</div>'
            '</div>'
            f'<script type="application/json" id="agg-pool-data">{_agg_pool_cfg_json}</script>'
        ),
    ))

    # ── 8081 Anthropic（FastAPI，本 App 自身）—— 转发网关 ──
    _8081_total = _ANTHROPIC_STATS.get("totalRequests", 0)
    _8081_ok = _ANTHROPIC_STATS.get("passthroughOk", 0)
    _8081_err = _ANTHROPIC_STATS.get("passthroughError", 0)
    _8081_rate = round(_8081_ok / _8081_total * 100, 1) if _8081_total > 0 else 100.0
    # 8081 卡片关联的 target：modelDefaults.defaultPort 对应端口（dashboard 映射按钮定位用）
    _forward_target = next((t for t in _TARGETS if t.get("listenPort") == _MODELS_CFG["modelDefaults"].get("defaultPort", 8082)), None)
    _forward_label = _forward_target["label"] if _forward_target else None
    _ap_models = _anthropic_port_models()
    agg_cards.append(_build_card_html(
        name="anthropic-compatible",
        note="FastAPI · Anthropic 协议入口 · /v1/messages 翻译为 OpenAI 后内部请求 8082",
        kind_badge="Protocol",
        status_badge="运行中",
        status_badge_class="purple",
        kv_items=[
            ("base_url", f"http://{_lan_ip}:8081"),
            ("监听地址", "http://0.0.0.0:8081"),
            ("内部回调", "http://127.0.0.1:8082/v1/chat/completions"),
            ("协议", "Anthropic /v1/messages → OpenAI 翻译"),
            ("模型数量", f"{len(_ap_models)} 个（models[] 定义）"),
            ("systemd 服务", "anthropic-compatible"),
        ],
        models=_ap_models,
        model_stats=_MODEL_STATS.get("anthropic", {}),
        stats_detail={
            "total": _8081_total, "ok": _8081_ok, "err": _8081_err,
            "translated": 0, "success_rate": _8081_rate,
            "uptime": _format_uptime(_ANTHROPIC_STATS.get("startedAt", "")), "alive": True,
        },
        description="接收 Anthropic 客户端请求，结构化解码后转换为 OpenAI 格式，内部转发到 8082（copilot 透传）。响应译回 Anthropic 格式。",
        label=None,
        port=8081,
        raw_html=(
            '<div class="model-ops">'
            '  <button class="model-edit-toggle" onclick="openModelsEditor(this)" '
            '    title="编辑模型定义（name/别名 → 下游端口+真实模型，可指向聚合虚拟模型 agg:xxx）">✏️ 模型定义</button>'
            '</div>'
        ),
        meta_badges=[("Forward Gateway", "b-meta-agg"), ("Anthropic", "b-meta-normal")],
    ))

    # ── 动态 target 卡片（targets.json 驱动）──
    for t in _TARGETS:
        port = t["listenPort"]
        r = _result_map.get(port)
        if r is None:
            try:
                r = await _fetch(port)
            except Exception:
                r = {"label": t["label"], "listenPort": port, "upstream": "?", "models": [], "total": 0, "ok": 0, "translated": 0, "err": 0, "alive": False, "startedAt": "", "modelStats": {}}
        category = t.get("category", "free")
        badge_map = {"crack": "破解", "free": "免费", "paid": "收费"}
        badge_class_map = {"crack": "blue", "free": "green", "paid": "orange"}
        # ── 模型标签分类：破解/非破解 · 免费/收费（破解默认免费，可被 isFree 覆盖）· 稳定性（破解/收费高，免费低）──
        is_crack = category == "crack"
        # 显式设置 isFree 时以配置为准（如企业版 Copilot isFree=false → 收费）；
        # 未设置时按 category 推断：paid=收费，其余免费
        is_free = t.get("isFree") if t.get("isFree") is not None else (category != "paid")
        is_stable = category in ("crack", "paid")  # 破解与收费服务稳定性高
        # 元数据标签：kind_badge 已显示"破解/免费/收费"，这里只保留稳定性 + 协议，避免语义重复
        meta_badges = [
            ("稳定性高" if is_stable else "稳定性低", "b-meta-stable" if is_stable else "b-meta-unstable"),
        ]
        # 协议标签：gemini-native 是 OpenAI↔Gemini 转换；其余 target（crack/透传/trae-work）客户端均走 OpenAI 协议
        if t.get("handler") == "gemini-native":
            meta_badges.append(("Gemini协议", "b-meta-gemini"))
        else:
            meta_badges.append(("OpenAI 协议", "b-meta-oa"))
        secret = _cfg.resolve_secret(t, _SECRETS)
        # 可粘贴 base_url：局域网 IP + 本机端口 + 后缀（客户端直接可用）
        # - crack 类：我们自己定义 base_url 规范，客户端统一 /v1，代理内部映射到下游
        # - gemini-native：客户端走 OpenAI 协议入口 /v1
        # - free/paid 透传：直接用上游 routePrefix（如 /api/v1）
        if t.get("category") == "crack" or t.get("handler") == "gemini-native":
            _base_suffix = "/v1"
        else:
            _base_suffix = t.get("routePrefix", "")
        _base_url = f"http://{_lan_ip}:{port}{_base_suffix}"
        kv = [
            ("base_url", _base_url),
            ("分类", badge_map.get(category, category)),
            ("handler", t.get("handler", "passthrough")),
            ("上游", f"{t.get('targetProtocol','https')}://{t['targetHost']}:{t.get('targetPort',443)}{t.get('routePrefix','')}"),
        ]
        if t.get("isFree") is not None:
            kv.append(("isFree", "是（免费）" if t["isFree"] else "否（收费）"))
        if t.get("enabled") is False:
            kv.append(("状态", "预留（未监听）"))

        # ── 卡片内联 token 编辑块 ──
        # 直连网关（free/paid）无 secretRef 时退回约定 key f"{label}_token"，
        # 与 config_store.secret_key_for / PUT /api/secrets/{label} 保持一致
        sec_ref = _cfg.secret_key_for(t)
        esc_label = t["label"].replace("'", "\\'")
        # 破解环境检测：不可用则置灰 + title 提示
        recrack_btn = ""
        if t.get('category') == 'crack' and t.get('crackTool'):
            env = _crack_env_check(t)
            if env.get("available"):
                recrack_btn = f'<button class="te-recrack" onclick="recrackCard(\'{esc_label}\', this)">重新破解</button>'
            else:
                recrack_btn = (
                    f'<button class="te-recrack" disabled title="{_html_escape(env.get("reason", "环境依赖缺失"))}">'
                    f'重新破解</button>'
                )
        token_status = "✅ 已配置 " + _cfg.mask_secret(secret) if secret else "⚠️ 缺失"
        input_placeholder = "已配置，输入新值覆盖" if secret else "填写 " + (sec_ref or "token")
        input_value = "******" if secret else ""
        # 破解网关扩展：凭据管理按钮 + 额度/签到状态展示容器
        crack_status_html = ""
        is_crack = t.get('category') == 'crack'
        if is_crack:
            crack_status_html = (
                f'<div class="crack-status" id="cs-{port}" data-label="{esc_label}" '
                f'data-ref="{sec_ref}">'
                f'  <div class="cs-loading">状态加载中…</div>'
                f'</div>'
            )
        if is_crack:
            # crack 类：统一凭据弹窗（schema 驱动），无内联 password 输入
            token_edit = (
                f'<div class="token-edit" id="te-{port}">'
                f'  <div class="te-status">凭据: {token_status}</div>'
                f'  <div class="te-row">'
                f'    <button class="te-cred-btn" onclick="openCredentialModal(\'{esc_label}\', this)">'
                f'      凭据管理</button>'
                f'    {recrack_btn}'
                f'  </div>'
                f'  {crack_status_html}'
                f'</div>'
            )
        else:
            # free/paid 类：保留单字段 token 编辑
            token_edit = (
                f'<div class="token-edit" id="te-{port}">'
                f'  <div class="te-status">token: {token_status}</div>'
                f'  <div class="te-row">'
                f'    <input type="password" class="te-input" data-label="{esc_label}" data-ref="{sec_ref}"'
                f'           placeholder="{input_placeholder}" value="{input_value}">'
                f'    <button class="te-save" onclick="saveCardToken(\'{esc_label}\', this)">保存</button>'
                f'    {recrack_btn}'
                f'  </div>'
                f'  {crack_status_html}'
                f'</div>'
            )

        card = _build_card_html(
            name=f"{t['label']}",
            note="统一透传引擎 · targets.json 驱动",
            kind_badge=badge_map.get(category, category),
            status_badge="运行中" if r["alive"] else ("未监听" if t.get("enabled") is False else "离线"),
            status_badge_class=badge_class_map.get(category, "gray") if r["alive"] else "red",
            kv_items=kv,
            models=t.get("models", []),
            model_stats=r.get("modelStats") if r.get("alive") else None,
            stats_detail=_make_stats_detail(r),
            description=f"category={category} · handler={t.get('handler','passthrough')} · isFree={t.get('isFree')}",
            accent_class=f"accent-{port}",
            raw_html=token_edit,
            label=t["label"],
            port=port,
            meta_badges=meta_badges,
            # 上游是否支持 /models：配置驱动（targets.json hasModels）+ handler 兜底（copilot 系天然支持）
            can_prune=(t.get("hasModels") is True or t.get("handler") == "copilot"),
        )
        if category == "crack":
            crack_cards.append(card)
        elif category == "aggregate":
            # 聚合网关卡片已手动构建（含状态区/编辑按钮），循环跳过避免重复
            pass
        else:
            direct_cards.append(card)

    def _render_group(title, cards_list):
        if not cards_list:
            return ""
        return (
            f'<div class="section"><div class="section-title">{title}'
            f'<span class="sec-count">{len(cards_list)}</span></div>'
            f'<div class="card-grid">{"".join(cards_list)}</div></div>'
        )

    cards_html = (
        _render_group("聚合网关", agg_cards)
        + _render_group("破解网关", crack_cards)
        + _render_group("直连网关", direct_cards)
    )
    all_models = _build_models_list()
    # 生成概览栏的状态点
    overview_dots = "".join(
        f'<span class="status-dot {"green" if r["alive"] else "red"}" title="端口 {r["listenPort"]}: {"在线" if r["alive"] else "离线"}"></span>'
        for r in results
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Gateway — 管理总览</title>
<style>{DASHBOARD_STYLE}</style>
</head>
<body>
  <h1>🔀 LLM Gateway — 管理总览</h1>
  <div class="sub">8081 Anthropic (FastAPI) → 8082 copilot (透传) → 上游 · 统一 targets.json 驱动 <span class="refresh-time">· 手动刷新 · {datetime.now().strftime("%H:%M:%S")}</span></div>
  <div class="overview-bar">
    <div class="kpi-grid">
      <div class="kpi-card">
        <span class="kpi-label">服务总数</span>
        <span class="kpi-value">{len(results)}</span>
        <span class="kpi-sub">enabled targets</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">累计请求</span>
        <span class="kpi-value">{total_requests_all}</span>
        <span class="kpi-sub">所有存活端口</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">存活端口</span>
        <span class="kpi-value">{alive_ports}<small>/{len(results)}</small></span>
        <span class="kpi-sub">在线 / 全部端口</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">在线率</span>
        <span class="kpi-value accent">{alive_rate}%</span>
        <span class="kpi-sub"><span class="kpi-dot" style="background:{_alive_color}; box-shadow:0 0 6px {_alive_color};"></span>运行健康度</span>
      </div>
    </div>
    <div class="ov-side">
      <div class="ov-dots">{overview_dots}</div>
      <div class="ov-actions">
        <button class="ov-btn" onclick="doReload()">♻️ 重载配置</button>
        <button class="ov-btn ov-btn-primary" onclick="location.reload()">🔄 刷新状态</button>
      </div>
    </div>
    <span id="ov-msg" class="ov-msg" role="status"></span>
  </div>
  <div class="dangling-bar" id="dangling-bar" role="status" aria-live="polite"></div>
  {cards_html}

  <!-- 模型编辑 modal -->
  <div class="modal-overlay" id="model-modal" role="dialog" aria-modal="true" aria-label="编辑模型展示">
    <div class="modal modal-wide">
      <div class="modal-head">
        <h3 id="model-modal-title">编辑模型</h3>
        <button class="modal-close" onclick="closeModelEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="model-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="model-modal-msg"></span>
        <button class="modal-btn" onclick="closeModelEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" id="model-modal-save" onclick="saveModelEditor(this)">保存</button>
      </div>
    </div>
  </div>

  <!-- 模型定义编辑 modal（全局 models[]：name/aliases/target port+model） -->
  <div class="modal-overlay" id="models-modal" role="dialog" aria-modal="true" aria-label="编辑模型定义">
    <div class="modal">
      <div class="modal-head">
        <h3 id="models-modal-title">模型定义</h3>
        <button class="modal-close" onclick="closeModelsEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="models-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="models-modal-msg"></span>
        <button class="modal-btn" onclick="closeModelsEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" onclick="saveModelsEditor(this)">保存</button>
      </div>
    </div>
  </div>

  <!-- 聚合网关配置编辑 modal -->
  <div class="modal-overlay" id="agg-modal" role="dialog" aria-modal="true" aria-label="编辑聚合网关配置">
    <div class="modal modal-wide">
      <div class="modal-head">
        <h3 id="agg-modal-title">聚合网关配置</h3>
        <button class="modal-close" onclick="closeAggConfigEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="agg-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="agg-modal-msg"></span>
        <button class="modal-btn" onclick="closeAggConfigEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" onclick="saveAggConfig(this)">保存</button>
      </div>
    </div>
  </div>


<script>
// ═══ 三个编辑 modal 共享基础设施（mm* 前缀，docs §2.2）═══
// 统一消息提示：kind ∈ ok | warn | err | info
function mmMsg(el, kind, text) {{
  if (!el) return;
  var K = {{ok: "success", warn: "danger", err: "danger", info: ""}};
  el.textContent = text || "";
  el.className = "modal-msg " + (K[kind] !== undefined ? K[kind] : "");
}}

// 统一行插入：永远插在 section 末尾的添加按钮行之前。
// anchorSel 必须是该 section 专属类名，避免嵌套同类按钮撞名（Bug 1 根因）。
function mmInsertRow(section, rowHtml, anchorSel) {{
  if (!section || !rowHtml) return null;
  var anchor = null;
  if (anchorSel) {{
    var cands = section.querySelectorAll(anchorSel);
    for (var i = 0; i < cands.length; i++) {{
      if (mmOwnsNode(section, cands[i], anchorSel)) {{ anchor = cands[i]; break; }}
    }}
  }}
  if (anchor) {{ anchor.insertAdjacentHTML("beforebegin", rowHtml); return anchor.previousElementSibling; }}
  section.insertAdjacentHTML("beforeend", rowHtml);
  return section.lastElementChild;
}}

// node 是否"属于"section 本层：node 与 section 之间不得夹着另一个同类锚点容器。
// 用于 agg-modal 这类嵌套结构（虚拟模型块内还有成员添加行）。
function mmOwnsNode(section, node, anchorSel) {{
  var p = node.parentNode;
  while (p && p !== section) {{
    if (p.matches && p.matches(".agg-vm, .agg-pool")) return false;
    p = p.parentNode;
  }}
  return p === section;
}}

// 作用域提示条：明示本 modal 改什么、不改什么（§2.4.1）。
function mmScope(doesText, notText) {{
  var h = '<div class="mm-scope"><span class="mm-scope-icon">i</span><span><b>';
  h += escHtml(doesText) + '</b>';
  if (notText) h += ' <span class="mm-scope-neg">' + escHtml(notText) + '</span>';
  h += '</span></div>';
  return h;
}}

// ── 悬空引用警示条（§2.4.4）：只读诊断，改名后引用断了要看得见 ──
async function loadDanglingBar() {{
  var bar = document.getElementById('dangling-bar');
  if (!bar) return;
  try {{
    var resp = await fetch('/api/config/dangling');
    if (!resp.ok) return;
    var r = await resp.json();
    var items = (r && r.items) || [];
    if (!items.length) {{ bar.classList.remove('show'); bar.innerHTML = ''; return; }}
    var h = '<div class="dg-head"><span>配置存在悬空引用</span>';
    h += '<span class="dg-count">' + items.length + ' 处</span></div><ul>';
    items.forEach(function(it) {{
      h += '<li><code>' + escHtml(it.path || '') + '</code>' + escHtml(it.msg || '') + '</li>';
    }});
    h += '</ul>';
    bar.innerHTML = h;
    bar.classList.add('show');
  }} catch (e) {{ /* 诊断性功能，失败静默不打扰主流程 */ }}
}}

// ── 保存后局部刷新（§2.4.3）：重拉 dashboard HTML，只替换目标卡片 DOM ──
// 不整页刷新：保留手风琴展开状态与滚动位置，用户能立刻看到"我改的生效了"。
async function refreshCardDom(port) {{
  try {{
    var resp = await fetch(location.pathname, {{headers: {{'Cache-Control': 'no-cache'}}}});
    if (!resp.ok) return false;
    var html = await resp.text();
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var sel = '.card[data-port="' + port + '"]';
    var fresh = doc.querySelector(sel);
    var cur = document.querySelector(sel);
    if (!fresh || !cur) return false;
    // 保留当前展开态：新 DOM 是服务端默认（收起）状态
    var wasOpen = !!cur.querySelector('.card-detail.open');
    cur.replaceWith(fresh);
    if (wasOpen) {{
      var d = fresh.querySelector('.card-detail');
      var a = fresh.querySelector('.ct-arrow');
      var t = fresh.querySelector('.card-toggle');
      if (d) d.classList.add('open');
      if (a) a.classList.add('open');
      if (t) t.setAttribute('aria-expanded', 'true');
    }}
    bindCardAccordion(fresh);
    return true;
  }} catch (e) {{ return false; }}
}}

// ── 手风琴交互（互斥，任一时刻只展开一个）──
// 具名函数而非 IIFE：局部刷新替换卡片 DOM 后要能重新绑定。
function bindCardAccordion(scope) {{
  var list = scope ? [scope] : Array.prototype.slice.call(document.querySelectorAll('.card'));
  list.forEach(function(card) {{
    var toggle = card.querySelector('.card-toggle');
    var detail = card.querySelector('.card-detail');
    if (!toggle || !detail || toggle._accBound) return;
    toggle._accBound = true;
    toggle.addEventListener('click', function() {{
      var isOpen = detail.classList.contains('open');
      document.querySelectorAll('.card').forEach(function(c) {{
        var d = c.querySelector('.card-detail');
        var a = c.querySelector('.ct-arrow');
        var t = c.querySelector('.card-toggle');
        if (d) d.classList.remove('open');
        if (a) a.classList.remove('open');
        if (t) t.setAttribute('aria-expanded', 'false');
      }});
      if (!isOpen) {{
        detail.classList.add('open');
        var arrow = toggle.querySelector('.ct-arrow');
        if (arrow) arrow.classList.add('open');
        toggle.setAttribute('aria-expanded', 'true');
      }}
    }});
  }});
}}
bindCardAccordion();

// ── 模型编辑 modal：打开（fetch 编辑态 HTML 填入 modal）──
async function openModelEditor(btn) {{
  var label = btn.dataset.label;
  var overlay = document.getElementById('model-modal');
  var body = document.getElementById('model-modal-body');
  var title = document.getElementById('model-modal-title');
  var msg = document.getElementById('model-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '编辑模型 — ' + label;
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label) + '/models?edit=1');
    var html = await resp.text();
    if (resp.ok) {{
      body.innerHTML = html;
      bindModelEvents();
    }} else {{
      body.innerHTML = '<div class="no-models">加载失败: ' + html + '</div>';
    }}
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function closeModelEditor() {{
  var overlay = document.getElementById('model-modal');
  if (overlay) overlay.classList.remove('open');
}}

// ── 清理过期模型：对照上游最新列表，删除已下线模型（配置 + 内存）──
async function pruneModels(btn) {{
  var label = btn.dataset.label;
  var msgEl = document.querySelector('.model-msg[data-label="' + label + '"]');
  btn.disabled = true;
  btn.textContent = '清理中...';
  var show = function(t, cls) {{
    if (msgEl) {{ msgEl.textContent = t; msgEl.className = 'model-msg ' + (cls || ''); }}
  }};
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label) + '/prune-models', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      if (r.removed && r.removed.length) {{
        show('✅ 已删除 ' + r.removed.length + ' 个过期模型: ' + r.removed.join(', '), 'ok');
        setTimeout(function() {{ location.reload(); }}, 1200);
      }} else {{
        show('✅ 无过期模型（全部与上游一致，共 ' + r.keptCount + ' 个）', 'ok');
        btn.disabled = false;
        btn.textContent = '🧹 清理过期模型';
      }}
    }} else {{
      show('❌ ' + (r.detail || JSON.stringify(r)), 'err');
      btn.disabled = false;
      btn.textContent = '🧹 清理过期模型';
    }}
  }} catch (e) {{
    show('❌ 请求异常: ' + e, 'err');
    btn.disabled = false;
    btn.textContent = '🧹 清理过期模型';
  }}
}}

// 点击遮罩关闭
(function() {{
  var overlay = document.getElementById('model-modal');
  if (overlay) {{
    overlay.addEventListener('click', function(e) {{
      if (e.target === overlay) overlay.classList.remove('open');
    }});
  }}
}})();

// ── 保存模型展示设置（读取 modal 内所有开关 → PUT models）──
async function saveModelEditor(btn) {{
  var overlay = document.getElementById('model-modal');
  var body = document.getElementById('model-modal-body');
  var msg = document.getElementById('model-modal-msg');
  if (!overlay || !body) return;
  var label = document.getElementById('model-modal-title').textContent.replace('编辑模型 — ', '');
  var rows = body.querySelectorAll('.mrow');
  var models = [];
  rows.forEach(function(row) {{
    var idEl = row.querySelector('.mrow-id');
    var sw = row.querySelector('.model-show');
    if (!idEl || !sw) return;  // 跳过总开关行（无子开关 .model-show）
    var mid = idEl.textContent;
    models.push({{id: mid, enabled: sw.checked}});
  }});
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label), {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{models: models}}),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      var _onN = models.filter(function(m) {{ return m.enabled; }}).length;
      mmMsg(msg, 'ok', '✅ 已保存 ' + models.length + ' 个模型（开启 ' + _onN + ' 个）→ ' + label + ' 卡片已更新');
      var _port = (document.querySelector('.card[data-label="' + label + '"]') || {{}}).dataset;
      if (_port && _port.port) await refreshCardDom(_port.port);
      setTimeout(function() {{
        closeModelEditor();
        btn.disabled = false; btn.textContent = '保存';
      }}, 1200);
    }} else {{
      mmMsg(msg, 'err', '❌ 保存失败: ' + JSON.stringify(r.detail || r));
      btn.disabled = false; btn.textContent = '保存';
    }}
  }} catch (e) {{
    mmMsg(msg, 'err', '❌ 保存异常: ' + e);
    btn.disabled = false; btn.textContent = '保存';
  }}
}}

// ── 总开关：全开/全关/部分开（indeterminate），联动所有子开关 ──
function syncMasterState() {{
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var master = body.querySelector('.model-master');
  if (!master) return;
  var subs = Array.prototype.slice.call(body.querySelectorAll('.mrow .model-show'));
  if (subs.length === 0) return;
  var on = subs.filter(function(s) {{ return s.checked; }}).length;
  if (on === 0) {{
    master.checked = false; master.indeterminate = false;
  }} else if (on === subs.length) {{
    master.checked = true; master.indeterminate = false;
  }} else {{
    master.checked = false; master.indeterminate = true;
  }}
}}

// ── 绑定模型编辑事件（modal 内开关绑定 + 总开关联动）──
function bindModelEvents() {{
  document.querySelectorAll('.model-show').forEach(function(sw) {{
    if (sw._bound) return;
    sw._bound = true;
    sw.addEventListener('change', syncMasterState);
  }});
  var master = document.querySelector('#model-modal-body .model-master');
  if (master && !master._bound) {{
    master._bound = true;
    master.addEventListener('change', function() {{
      var checked = master.checked;
      document.querySelectorAll('#model-modal-body .mrow .model-show').forEach(function(sw) {{
        sw.checked = checked;
      }});
      syncMasterState();
    }});
  }}
  syncMasterState();
}}

// ── modal 内模型搜索过滤（隐藏不匹配行）──
function filterModels(input) {{
  var q = (input.value || '').toLowerCase().trim();
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var visible = 0;
  body.querySelectorAll('.mrow').forEach(function(row) {{
    var text = (row.textContent || '').toLowerCase();
    var match = !q || text.indexOf(q) >= 0;
    row.style.display = match ? '' : 'none';
    if (match) visible++;
  }});
  // 无匹配时提示
  var empty = body.querySelector('.no-models');
  if (q && visible === 0) {{
    if (!empty) {{
      empty = document.createElement('div');
      empty.className = 'no-models';
      body.appendChild(empty);
    }}
    empty.textContent = '无匹配模型: ' + input.value;
  }} else if (empty) {{
    empty.remove();
  }}
}}

// ── modal 内模型行：删除（×）──
function removeModelRow(btn) {{
  var row = btn.closest('.mrow');
  if (row) row.remove();
  syncMasterState();
}}

// ── modal 内模型行：底部添加（自由输入新模型名）──
function addModelRow() {{
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var input = document.getElementById('model-add-input');
  var mid = (input && input.value || '').trim();
  if (!mid) {{
    var msgEl = body.querySelector('.model-msg');
    if (msgEl) {{ msgEl.textContent = '⚠️ 请输入模型名'; msgEl.className = 'model-msg err'; }}
    return;
  }}
  var dup = false;
  body.querySelectorAll('.mrow .mrow-id').forEach(function(idEl) {{
    if (idEl.textContent === mid) dup = true;
  }});
  if (dup) {{
    var msgEl = body.querySelector('.model-msg');
    if (msgEl) {{ msgEl.textContent = '⚠️ 模型已存在: ' + mid; msgEl.className = 'model-msg err'; }}
    return;
  }}
  var html = '<div class="mrow" data-model="' + escHtml(mid) + '">' +
    '<div class="mrow-info">' +
    '  <div class="mrow-id">' + escHtml(mid) + '</div>' +
    '  <div class="mrow-name">' + escHtml(mid) + '</div>' +
    '</div>' +
    '<label class="switch" title="展示此模型">' +
    '  <input type="checkbox" class="model-show" data-model="' + escHtml(mid) + '" checked>' +
    '  <span class="switch-slider"></span>' +
    '</label>' +
    '<button class="mrow-del" onclick="removeModelRow(this)" title="删除此模型">×</button>' +
    '</div>';
  // 列表容器优先：.mrow-add 在 .model-editor-add 内，与行列表不同层
  var list = body.querySelector('.model-editor-list') || body;
  mmInsertRow(list, html, '.mrow-add');
  var nm = body.querySelector('.no-models');
  if (nm) nm.remove();
  if (input) input.value = '';
  bindModelEvents();
}}

// ── 模型定义编辑 modal（全局 models[]：name/aliases/target port+model）──
async function openModelsEditor(btn) {{
  var overlay = document.getElementById('models-modal');
  var body = document.getElementById('models-modal-body');
  var title = document.getElementById('models-modal-title');
  var msg = document.getElementById('models-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '模型定义 — 8081 转发/别名配置';
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var results = await Promise.all([fetch('/api/models'), fetch('/api/aggregate/config')]);
    var resp = results[0];
    var portsResp = results[1];
    var r = await resp.json();
    var ports = await portsResp.json();
    if (!resp.ok) {{
      body.innerHTML = '<div class="no-models">加载失败: ' + (r.detail || JSON.stringify(r)) + '</div>';
      return;
    }}
    _aggAvailablePorts = ports.availablePorts || {{}};
    body.innerHTML = buildModelsEditorHtml(r);
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function buildModelsEditorHtml(r) {{
  var models = r.models || [];
  var html = mmScope('本页定义 8081 的模型别名 → 下游端口+真实模型，保存后立即出现在 8081 卡片。',
    '不影响各 target 端口自身的透传白名单，也不改变 8080 聚合路由。');
  html += '<div class="mm-hint">模型定义：name 为主模型名（请求可直接用它），aliases 为额外别名（逗号分隔），target 指定最终下游端口与真实模型（可填聚合虚拟模型 agg:xxx）。未匹配任何定义的模型名将走 modelDefaults.defaultPort 原样透传。</div>';
  html += '<div class="agg-section"><div class="agg-section-title">默认转发端口</div><div class="agg-fields">' +
    '<label class="agg-field">' +
    '  <span class="agg-label">modelDefaults.defaultPort（未命中定义的兜底端口）</span>' +
    '  <input type="number" class="agg-input md-default-port" value="' + escHtml(String((r.modelDefaults || {{}}).defaultPort)) + '" aria-label="默认转发端口">' +
    '</label>' +
    '</div></div>';
  html += '<div class="agg-section"><div class="agg-section-title">模型定义列表</div>';
  if (models.length === 0) {{
    html += modelsRowHtml('', '', '', '');
  }} else {{
    models.forEach(function(m) {{
      var aliases = (m.aliases || []).join(', ');
      var t = m.target || {{}};
      html += modelsRowHtml(m.name, aliases, t.port, t.model);
    }});
  }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addModelsRow()">+ 添加模型</button></div></div>';
  return html;
}}

function modelsRowHtml(name, aliases, port, model) {{
  var n = (name === undefined || name === null) ? '' : escHtml(String(name));
  var a = (aliases === undefined || aliases === null) ? '' : escHtml(String(aliases));
  var p = (port === undefined || port === null) ? '' : escHtml(String(port));
  var m = (model === undefined || model === null) ? '' : escHtml(String(model));
  return '<div class="mm-row">' +
    '<input type="text" class="agg-input md-name" value="' + n + '" placeholder="模型名（如 sonnet）" aria-label="模型名">' +
    '<input type="text" class="agg-input md-aliases" value="' + a + '" placeholder="别名，逗号分隔" aria-label="别名">' +
    aggPortSelectHtml(p) +
    aggModelSelectHtml(p, m) +
    '<button class="mm-del" onclick="removeModelsRow(this)" title="删除此行">×</button>' +
    '</div>';
}}

function addModelsRow() {{
  var body = document.getElementById('models-modal-body');
  if (!body) return;
  mmInsertRow(body, modelsRowHtml('', '', '', ''), '.agg-add-row');
}}

function removeModelsRow(btn) {{
  var row = btn.closest('.mm-row');
  if (row) row.remove();
}}

function closeModelsEditor() {{
  var overlay = document.getElementById('models-modal');
  if (overlay) overlay.classList.remove('open');
}}

async function saveModelsEditor(btn) {{
  var body = document.getElementById('models-modal-body');
  var msg = document.getElementById('models-modal-msg');
  if (!body || !msg) return;
  var defaultPortEl = body.querySelector('.md-default-port');
  var defaultPort = defaultPortEl ? defaultPortEl.value.trim() : '';
  if (defaultPort === '' || isNaN(Number(defaultPort)) || Number(defaultPort) < 0 || Number(defaultPort) % 1 !== 0) {{
    mmMsg(msg, 'err', '⚠️ defaultPort 必须为非负整数');
    return;
  }}
  var models = [];
  var bad = false;
  body.querySelectorAll('.mm-row').forEach(function(row) {{
    if (bad) return;
    var nEl = row.querySelector('.md-name');
    var aEl = row.querySelector('.md-aliases');
    var pEl = row.querySelector('.agg-mem-port');
    var mEl = row.querySelector('.agg-mem-model');
    var n = (nEl ? nEl.value : '').trim();
    var a = (aEl ? aEl.value : '').trim();
    var p = (pEl ? pEl.value : '').trim();
    var m = (mEl ? mEl.value : '').trim();
    if (!n && !a && !p && !m) return;
    if (!n) {{ mmMsg(msg, 'err', '⚠️ 模型名不能为空'); bad = true; return; }}
    if (p === '' || isNaN(Number(p)) || Number(p) < 0 || Number(p) % 1 !== 0) {{
      mmMsg(msg, 'err', '⚠️ 模型 ' + n + ' 的下游端口必须为非负整数'); bad = true; return;
    }}
    if (!m) {{ mmMsg(msg, 'err', '⚠️ 模型 ' + n + ' 的真实模型不能为空'); bad = true; return; }}
    var aliases = a ? a.split(',').map(function(x) {{ return x.trim(); }}).filter(function(x) {{ return x; }}) : [];
    models.push({{name: n, aliases: aliases, target: {{port: Number(p), model: m}}}});
  }});
  if (bad) return;
  var payload = {{models: models, modelDefaults: {{defaultPort: Number(defaultPort)}}}};
  var resp = await fetch('/api/models', {{
    method: 'PUT',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});
  var r = await resp.json();
  if (!resp.ok) {{
    mmMsg(msg, 'err', '⚠️ 保存失败: ' + (r.detail || JSON.stringify(r)));
    return;
  }}
  // 生效位置提示（docs §2.4.2）：显示实际保存条目数 + 改动出现在哪
  mmMsg(msg, 'ok', '✅ 已保存 ' + models.length + ' 个模型定义 → 已在 8081 卡片显示');
  // 局部刷新 8081 卡片（§2.4.3）：不整页刷新，用户立刻能对上数字
  await refreshCardDom(8081);
  loadDanglingBar();
  setTimeout(function() {{ closeModelsEditor(); }}, 1200);
}}

// ── 聚合网关（8080）配置编辑 modal ──
function aggNumField(key, labelText, val, placeholder) {{
  var v = (val === undefined || val === null) ? '' : escHtml(String(val));
  return '<label class="agg-field">' +
    '<span class="agg-label">' + labelText + '</span>' +
    '<input type="number" class="agg-input agg-pd-num" data-key="' + key + '" value="' + v + '" placeholder="' + (placeholder || '') + '" aria-label="' + labelText + '">' +
    '</label>';
}}

// 聚合可用端口缓存（由 buildAggConfigHtml 在打开编辑器时注入）
var _aggAvailablePorts = {{}};

function aggPortSelectHtml(selectedPort) {{
  var html = '<select class="agg-input agg-mem-port" aria-label="端口" onchange="onAggPortChange(this)">';
  html += '<option value=""' + (selectedPort ? '' : ' selected') + '>选择端口</option>';
  var keys = Object.keys(_aggAvailablePorts).sort(function(a, b) {{ return Number(a) - Number(b); }});
  keys.forEach(function(pk) {{
    var info = _aggAvailablePorts[pk];
    var label = pk + ' · ' + (info.label || info.handler || '');
    var sel = (String(selectedPort) === pk) ? ' selected' : '';
    html += '<option value="' + pk + '"' + sel + '>' + escHtml(label) + '</option>';
  }});
  if (selectedPort !== undefined && selectedPort !== null && selectedPort !== '' && !_aggAvailablePorts[String(selectedPort)]) {{
    html += '<option value="' + escHtml(String(selectedPort)) + '" selected>' + escHtml(String(selectedPort)) + ' (自定义)</option>';
  }}
  html += '</select>';
  return html;
}}

function aggModelSelectHtml(selectedPort, selectedModel) {{
  var html = '<select class="agg-input agg-mem-model" aria-label="模型" onchange="onAggModelChange(this)">';
  html += '<option value=""' + (selectedModel ? '' : ' selected') + '>选择模型</option>';
  var models = [];
  if (selectedPort !== undefined && selectedPort !== null && selectedPort !== '' && _aggAvailablePorts[String(selectedPort)]) {{
    models = _aggAvailablePorts[String(selectedPort)].models || [];
  }}
  // 仅显示所选端口的真实上游模型，不追加虚拟模型（agg:xxx）
  var all = models.slice();
  all.sort();
  all.forEach(function(m) {{
    var sel = (m === selectedModel) ? ' selected' : '';
    html += '<option value="' + escHtml(m) + '"' + sel + '>' + escHtml(m) + '</option>';
  }});
  if (selectedModel !== undefined && selectedModel !== null && selectedModel !== '' && all.indexOf(selectedModel) === -1) {{
    html += '<option value="' + escHtml(String(selectedModel)) + '" selected>' + escHtml(String(selectedModel)) + ' (自定义)</option>';
  }}
  html += '</select>';
  return html;
}}

function onAggPortChange(selEl) {{
  var row = selEl.closest('.agg-pool-row, .mm-row');
  if (!row) return;
  var modelSel = row.querySelector('.agg-mem-model');
  if (!modelSel) return;
  var port = selEl.value;
  // 重建模型下拉（不传 poolKey，统一只显示所选端口的真实模型）
  var newHtml = aggModelSelectHtml(port, '');
  var tmp = document.createElement('div');
  tmp.innerHTML = newHtml;
  var newSel = tmp.firstChild;
  if (newSel) {{
    modelSel.parentNode.insertBefore(newSel, modelSel);
    modelSel.parentNode.removeChild(modelSel);
  }}
}}

function onAggModelChange(selEl) {{
  // 保留钩子：未来可扩展 agg:xxx 模型的特殊处理
}}

function aggPoolMemberRow(port, model, weight, poolKey) {{
  var w = (weight === undefined || weight === null) ? '' : escHtml(String(weight));
  return '<div class="agg-pool-row">' +
    aggPortSelectHtml(port) +
    aggModelSelectHtml(port, model) +
    '<input type="number" class="agg-input agg-mem-weight" value="' + w + '" placeholder="权重" aria-label="权重">' +
    '<button class="mm-del" onclick="removeAggPoolMember(this)" title="删除成员">×</button>' +
    '</div>';
}}

function aggVmBlock(id, vm) {{
  var d = (vm && vm.defaultPool) || [];
  var f = (vm && vm.fallbackPool) || [];
  var dr = (vm && vm.defaultRetries !== undefined && vm.defaultRetries !== null) ? escHtml(String(vm.defaultRetries)) : '';
  var fr = (vm && vm.fallbackRetries !== undefined && vm.fallbackRetries !== null) ? escHtml(String(vm.fallbackRetries)) : '';
  var html = '<div class="agg-vm">' +
    '<div class="agg-vm-head">' +
    '  <span class="agg-label">虚拟模型 id</span>' +
    '  <input type="text" class="agg-input agg-vm-id" value="' + escHtml(String(id)) + '" placeholder="如 agg:sonnet" aria-label="虚拟模型 id">' +
    '  <button class="mm-del" onclick="removeAggVm(this)" title="删除此虚拟模型">🗑</button>' +
    '</div>';
  html += '<div class="agg-pool" data-pool="default">' +
    '<div class="agg-pool-title">默认池 defaultPool</div>';
  if (d.length) {{ d.forEach(function(mem) {{ html += aggPoolMemberRow(mem.port, mem.model, mem.weight, 'default'); }}); }}
  else {{ html += aggPoolMemberRow('', '', '', 'default'); }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addAggPoolMember(this, &quot;default&quot;)">+ 添加成员</button></div></div>';
  html += '<div class="agg-pool" data-pool="fallback">' +
    '<div class="agg-pool-title">降级池 fallbackPool</div>';
  if (f.length) {{ f.forEach(function(mem) {{ html += aggPoolMemberRow(mem.port, mem.model, mem.weight, 'fallback'); }}); }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addAggPoolMember(this, &quot;fallback&quot;)">+ 添加降级成员</button></div></div>';
  html += '<div class="agg-vm-retries">' +
    '<label class="agg-field">' +
    '  <span class="agg-label">defaultRetries（空=继承池默认）</span>' +
    '  <input type="number" class="agg-input agg-vm-dr" value="' + dr + '" placeholder="继承" aria-label="defaultRetries">' +
    '</label>' +
    '<label class="agg-field">' +
    '  <span class="agg-label">fallbackRetries（空=继承池默认）</span>' +
    '  <input type="number" class="agg-input agg-vm-fr" value="' + fr + '" placeholder="继承" aria-label="fallbackRetries">' +
    '</label>' +
    '</div>' +
    '</div>';
  return html;
}}

async function openAggConfigEditor(btn) {{
  var overlay = document.getElementById('agg-modal');
  var body = document.getElementById('agg-modal-body');
  var title = document.getElementById('agg-modal-title');
  var msg = document.getElementById('agg-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '聚合网关配置 — 8080';
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var resp = await fetch('/api/aggregate/config');
    var r = await resp.json();
    if (!resp.ok) {{
      body.innerHTML = '<div class="no-models">加载失败: ' + (r.detail || JSON.stringify(r)) + '</div>';
      return;
    }}
    if (!r.configured) {{
      body.innerHTML = '<div class="no-models">聚合网关未配置（targets.json 中缺少 handler=aggregator 的 target）</div>';
      return;
    }}
    body.innerHTML = buildAggConfigHtml(r);
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function buildAggConfigHtml(r) {{
  // 注入全局缓存，供 aggPortSelectHtml / aggModelSelectHtml 使用
  _aggAvailablePorts = r.availablePorts || {{}};
  var pd = r.poolDefaults || {{}};
  var html = mmScope('本页配置仅影响 8080 聚合网关的虚拟模型路由，保存后引擎热重载。',
    '不改变 8081 模型列表，也不改变各下游端口自身的模型白名单。');
  html += '<div class="agg-hint">虚拟模型池配置：成员端口指向本地真实网关端口，模型为上游模型名（可填 agg:xxx 链式聚合）。' +
    '权重与重试留空 = 继承池默认值；保存后热生效。</div>';
  html += '<div class="agg-section"><div class="agg-section-title">池默认值 poolDefaults</div><div class="agg-fields">' +
    aggNumField('defaultRetries', 'defaultRetries', pd.defaultRetries, '如 2') +
    aggNumField('fallbackRetries', 'fallbackRetries', pd.fallbackRetries, '如 1') +
    aggNumField('sessionAffinityTtlSeconds', 'sessionAffinityTtlSeconds', pd.sessionAffinityTtlSeconds, '如 3600') +
    aggNumField('probeIntervalSeconds', 'probeIntervalSeconds', pd.probeIntervalSeconds, '如 300') +
    aggNumField('weight', 'weight（成员默认权重）', pd.weight, '如 1') +
    '</div></div>';
  html += '<div class="agg-section" id="agg-vm-section"><div class="agg-section-title">虚拟模型 virtualModels</div>';
  var vms = r.virtualModels || {{}};
  var keys = Object.keys(vms);
  if (keys.length === 0) {{
    html += '<div class="no-models">(暂无虚拟模型，点击下方「+ 新增虚拟模型」添加)</div>';
  }} else {{
    keys.forEach(function(k) {{ html += aggVmBlock(k, vms[k]); }});
  }}
  html += '<button class="mm-add-btn agg-vm-add" onclick="addAggVm()">+ 新增虚拟模型</button></div>';
  return html;
}}

function addAggVm() {{
  var body = document.getElementById('agg-modal-body');
  var section = document.getElementById('agg-vm-section');
  if (!body || !section) return;
  var html = aggVmBlock('', {{defaultPool: [], fallbackPool: []}});
  // 锚点必须用专属类名 agg-vm-add：section 内每个虚拟模型块还含「+ 添加成员」
  // 等同类 .mm-add-btn 按钮，querySelector('.mm-add-btn') 会取到第一个（嵌套插错位置）。
  // mmInsertRow 额外做本层归属校验（mmOwnsNode），双重保险。
  mmInsertRow(section, html, '.agg-vm-add');
  var ids = body.querySelectorAll('.agg-vm-id');
  if (ids.length) ids[ids.length - 1].focus();
}}

function removeAggVm(btn) {{
  var block = btn.closest('.agg-vm');
  if (block) block.remove();
}}

function addAggPoolMember(btn, poolKey) {{
  var pool = btn.closest('.agg-pool');
  if (!pool) return;
  var html = aggPoolMemberRow('', '', '', poolKey || (pool.dataset ? pool.dataset.pool : ''));
  mmInsertRow(pool, html, '.agg-add-row');
}}

function removeAggPoolMember(btn) {{
  var row = btn.closest('.agg-pool-row');
  if (row) row.remove();
}}

async function saveAggConfig(btn) {{
  var body = document.getElementById('agg-modal-body');
  var msg = document.getElementById('agg-modal-msg');
  if (!body || !msg) return;
  var poolDefaults = {{}};
  var bad = false;
  body.querySelectorAll('.agg-pd-num').forEach(function(inp) {{
    if (bad) return;
    var key = inp.dataset.key;
    var v = inp.value.trim();
    if (v === '') return;
    var n = Number(v);
    if (isNaN(n) || n < 0) {{
      mmMsg(msg, 'err', '⚠️ poolDefaults.' + key + ' 必须为非负数字');
      bad = true; return;
    }}
    poolDefaults[key] = n;
  }});
  var virtualModels = {{}};
  if (!bad) {{
    body.querySelectorAll('.agg-vm').forEach(function(vm) {{
      if (bad) return;
      var idEl = vm.querySelector('.agg-vm-id');
      var vid = (idEl ? idEl.value : '').trim();
      if (!vid) return;  // 空 id 块忽略
      if (virtualModels[vid]) {{
        mmMsg(msg, 'err', '⚠️ 虚拟模型 id 重复: ' + vid);
        bad = true; return;
      }}
      var entry = {{}};
      ['default', 'fallback'].forEach(function(poolKey) {{
        if (bad) return;
        var list = [];
        var pool = vm.querySelector('.agg-pool[data-pool="' + poolKey + '"]');
        if (pool) {{
          pool.querySelectorAll('.agg-pool-row').forEach(function(row) {{
            if (bad) return;
            var portEl = row.querySelector('.agg-mem-port');
            var modelEl = row.querySelector('.agg-mem-model');
            var wEl = row.querySelector('.agg-mem-weight');
                    var port = (portEl ? portEl.value : '').trim();
            var model = (modelEl ? modelEl.value : '').trim();
            var w = (wEl ? wEl.value : '').trim();
            if (!port && !model && !w) return;  // 空行忽略
            if (port === '' || isNaN(Number(port)) || Number(port) < 0) {{
              mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员端口必须为非负整数');
              bad = true; return;
            }}
            if (!model) {{
              mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员模型名不能为空');
              bad = true; return;
            }}
            var mem = {{port: Number(port), model: model}};
            // 端口已在下拉列表外（自定义）：允许 agg:xxx 等链式聚合，模型也允许自由输入
            if (w !== '') {{
              var wn = Number(w);
              if (isNaN(wn) || wn < 0) {{
                mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员权重必须为非负数字');
                bad = true; return;
              }}
              mem.weight = wn;
            }}
            list.push(mem);
          }});
        }}
        entry[poolKey === 'default' ? 'defaultPool' : 'fallbackPool'] = list;
      }});
      if (bad) return;
      var drEl = vm.querySelector('.agg-vm-dr');
      var frEl = vm.querySelector('.agg-vm-fr');
      var dr = drEl ? drEl.value.trim() : '';
      var fr = frEl ? frEl.value.trim() : '';
      if (dr !== '') {{
        var drn = Number(dr);
        if (isNaN(drn) || drn < 0 || drn % 1 !== 0) {{
          mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的 defaultRetries 必须为非负整数');
          bad = true; return;
        }}
        entry.defaultRetries = drn;
      }}
      if (fr !== '') {{
        var frn = Number(fr);
        if (isNaN(frn) || frn < 0 || frn % 1 !== 0) {{
          mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的 fallbackRetries 必须为非负整数');
          bad = true; return;
        }}
        entry.fallbackRetries = frn;
      }}
      virtualModels[vid] = entry;
    }});
  }}
  if (bad) return;
  if (Object.keys(virtualModels).length === 0) {{
    mmMsg(msg, 'err', '⚠️ 至少需要一个虚拟模型');
    return;
  }}
  var payload = {{virtualModels: virtualModels}};
  if (Object.keys(poolDefaults).length) payload.poolDefaults = poolDefaults;
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/aggregate/config', {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      var _vmN = Object.keys(virtualModels).length;
      mmMsg(msg, 'ok', '✅ 已保存 ' + _vmN + ' 个虚拟模型 → 聚合路由已热重载（8080 卡片已更新）');
      btn.textContent = '✅ 已保存'; btn.style.background = '#4ade80';
      await refreshCardDom(8080);
      loadDanglingBar();
      setTimeout(function() {{
        closeAggConfigEditor();
        btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
      }}, 1200);
    }} else {{
      var errs = Array.isArray(r.detail) ? r.detail.join('；') : JSON.stringify(r.detail || r);
      mmMsg(msg, 'err', '❌ 保存失败: ' + errs);
      btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
    }}
  }} catch (e) {{
    mmMsg(msg, 'err', '❌ 保存异常: ' + e);
    btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
  }}
}}

function closeAggConfigEditor() {{
  var overlay = document.getElementById('agg-modal');
  if (overlay) overlay.classList.remove('open');
}}

// 点击遮罩关闭聚合配置弹框
(function() {{
  var overlay = document.getElementById('agg-modal');
  if (overlay) {{
    overlay.addEventListener('click', function(e) {{
      if (e.target === overlay) overlay.classList.remove('open');
    }});
  }}
}})();


// ── 破解 token 重试 ──
async function recrackCard(label, btn) {{
  btn.disabled = true; btn.textContent = '破解中...';
  try {{
    var resp = await fetch('/api/targets/' + label + '/recrack', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      btn.textContent = '✅ 已破解'; btn.style.background = '#4ade80';
      setTimeout(function() {{ location.reload(); }}, 1200);
    }} else {{
      btn.textContent = '❌ 失败'; btn.style.background = '#ef4444';
      setOvMsg('❌ ' + (r.message || JSON.stringify(r)), 'danger');
      setTimeout(function() {{ btn.disabled = false; btn.textContent = '重新破解'; btn.style.background = ''; }}, 2000);
    }}
  }} catch (e) {{
    btn.textContent = '❌ 失败'; btn.style.background = '#ef4444';
    setOvMsg('❌ 破解异常: ' + e, 'danger');
    setTimeout(function() {{ btn.disabled = false; btn.textContent = '重新破解'; btn.style.background = ''; }}, 2000);
  }}
}}

async function saveCardToken(label, btn) {{
  var row = btn.closest('.token-edit');
  var input = row.querySelector('.te-input');
  // 无 secretRef 的直连网关：后端按约定落到 secrets.json 的 "<label>_token"，无需前端拦截
  var ref = input.dataset.ref || (label + '_token');
  var val = input.value;
  if (!val || val === '******') {{
    showTeStatus(row, '⚠️ 请输入新的 token 值', 'warning');
    return;
  }}
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/secrets/' + label, {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{value: val}}),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      btn.textContent = '✅ 已保存'; btn.style.background = '#4ade80';
      input.value = '******'; input.placeholder = '已配置，输入新值覆盖';
      var savedRef = (r && r.secretRef) ? r.secretRef : ref;
      showTeStatus(row, '✅ 已保存到 secrets.json (' + savedRef + ')，热生效；未带 key 的客户端将用它兜底', 'success');
      setTimeout(function() {{ btn.disabled = false; btn.textContent = '保存'; btn.style.background = ''; }}, 2000);
    }} else {{
      btn.disabled = false; btn.textContent = '保存';
      showTeStatus(row, '❌ 保存失败: ' + JSON.stringify(r.detail || r), 'danger');
    }}
  }} catch (e) {{
    btn.disabled = false; btn.textContent = '保存';
    showTeStatus(row, '❌ 保存异常: ' + e, 'danger');
  }}
}}

function showTeStatus(row, msg, level) {{
  var status = row.querySelector('.te-status');
  if (status) {{
    var colors = {{'success': '#4ade80', 'warning': '#fbbf24', 'danger': '#f87171'}};
    status.textContent = msg;
    status.style.color = colors[level] || '#9ca3af';
    if (level === 'success') {{
      setTimeout(function() {{ status.style.color = '#9ca3af'; }}, 3000);
    }}
  }}
}}

// ── 模型编辑 modal（openModelEditor/saveModelEditor 在上方定义）──

function setOvMsg(msg, level) {{
  var el = document.getElementById('ov-msg');
  if (!el) return;
  el.textContent = msg;
  el.className = 'ov-msg ' + (level || '');
  if (level !== 'danger') {{
    setTimeout(function() {{ el.textContent = ''; }}, 4000);
  }}
}}

async function doReload() {{
  var btn = event.target;
  if (btn) {{ btn.disabled = true; btn.textContent = '重载中...'; }}
  try {{
    var resp = await fetch('/api/reload', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      setOvMsg('✅ 配置已重载（' + (r.changes ? JSON.stringify(r.changes) : 'ok') + '）', 'success');
      setTimeout(function() {{ location.reload(); }}, 800);
    }} else {{
      setOvMsg('❌ 重载失败: ' + JSON.stringify(r), 'danger');
      if (btn) {{ btn.disabled = false; btn.textContent = '♻️ 重载配置'; }}
    }}
  }} catch (e) {{
    setOvMsg('❌ 重载异常: ' + e, 'danger');
    if (btn) {{ btn.disabled = false; btn.textContent = '♻️ 重载配置'; }}
  }}
}}

// ── 初始化 ──
bindModelEvents();
loadDanglingBar();

// ── 破解网关：凭据管理弹窗（schema 驱动，表单/JSON 双模式）──
var credModal = null;
var credSchema = null;
var credLabel = '';

async function openCredentialModal(label, btn) {{
  credLabel = label;
  if (!credModal) {{
    var div = document.createElement('div');
    div.className = 'modal-overlay cred-modal';
    div.innerHTML =
      '<div class="modal cred-box">' +
      '  <div class="modal-head cred-head">' +
      '    <h3 id="cred-title">凭据管理</h3>' +
      '    <button class="modal-close cred-close" onclick="closeCredModal()" aria-label="关闭">×</button>' +
      '  </div>' +
      '  <div class="modal-body cred-body">' +
      '    <div class="cred-tabs">' +
      '      <button class="cred-tab active" data-mode="form" onclick="switchCredTab(&quot;form&quot;)">表单</button>' +
      '      <button class="cred-tab" data-mode="json" onclick="switchCredTab(&quot;json&quot;)">JSON</button>' +
      '    </div>' +
      '    <div id="cred-form" class="cred-pane active"></div>' +
      '    <div id="cred-json" class="cred-pane" style="display:none">' +
      '      <p class="cred-hint" id="cred-json-hint"></p>' +
      '      <textarea id="cred-json-input" placeholder="粘贴 JSON 凭据..."></textarea>' +
      '    </div>' +
      '  </div>' +
      '  <div class="modal-foot cred-foot">' +
      '    <span class="modal-msg cred-msg" id="cred-msg"></span>' +
      '    <button class="modal-btn cred-cancel" onclick="closeCredModal()">取消</button>' +
      '    <button class="modal-btn modal-btn-primary cred-save" onclick="submitCredential()">保存</button>' +
      '  </div>' +
      '</div>';
    div.addEventListener('click', function(e) {{ if (e.target === div) closeCredModal(); }});
    document.body.appendChild(div);
    credModal = div;
  }}
  document.getElementById('cred-msg').textContent = '';
  document.getElementById('cred-json-input').value = '';
  showCredMsg('加载中...', '');
  try {{
    var resp = await fetch('/api/crack/' + label + '/schema');
    if (!resp.ok) {{ showCredMsg('获取 schema 失败: HTTP ' + resp.status, 'err'); return; }}
    credSchema = await resp.json();
    document.getElementById('cred-title').textContent = '凭据 · ' + (credSchema.displayName || label);
    // 渲染表单
    var formHtml = '';
    credSchema.fields.forEach(function(f) {{
      var masked = (f.type === 'password') ? '留空则不修改' : '可选';
      formHtml += '<div class="cred-field">' +
        '<label>' + f.label + (f.required ? ' <span class="cred-req">*</span>' : '') + '</label>' +
        '<input type="' + f.type + '" data-key="' + f.key + '"' +
        '       placeholder="' + (f.placeholder || masked) + '">' +
        '<span class="cred-hint">' + (f.hint || '') + '</span>' +
        '<span class="cred-field-err"></span>' +
        '</div>';
    }});
    if (credSchema.readonlyFields && credSchema.readonlyFields.length) {{
      formHtml += '<div class="cred-readonly">只读字段（查询结果，不需手动填写）: ' +
        credSchema.readonlyFields.join(', ') + '</div>';
    }}
    document.getElementById('cred-form').innerHTML = formHtml;
    // JSON 模式提示
    var jsonHint = '粘贴 JSON，支持字段: ' + credSchema.fields.map(function(f){{return f.key}}).join(' / ');
    var rawKeys = Object.keys(credSchema.jsonImportMapping || {{}});
    if (rawKeys.length) jsonHint += '（或原始命名: ' + rawKeys.join(' / ') + '）';
    document.getElementById('cred-json-hint').textContent = jsonHint;
    showCredMsg('', '');
    credModal.classList.add('open');
    switchCredTab('form');
  }} catch (e) {{
    showCredMsg('加载异常: ' + e, 'err');
  }}
}}

function switchCredTab(mode) {{
  document.querySelectorAll('.cred-tab').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.mode === mode);
  }});
  document.getElementById('cred-form').style.display = (mode === 'form') ? '' : 'none';
  document.getElementById('cred-json').style.display = (mode === 'json') ? '' : 'none';
}}

function closeCredModal() {{
  if (credModal) credModal.classList.remove('open');
}}

function showCredMsg(text, kind) {{
  var el = document.getElementById('cred-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'cred-msg' + (kind ? ' ' + kind : '');
}}

async function submitCredential() {{
  if (!credSchema || !credLabel) return;
  var activeMode = document.querySelector('.cred-tab.active').dataset.mode;
  var data = {{}};
  if (activeMode === 'form') {{
    var errors = [];
    credSchema.fields.forEach(function(f) {{
      var input = document.querySelector('#cred-form input[data-key="' + f.key + '"]');
      if (!input) return;
      var val = (input.value || '').trim();
      if (!val) return;  // 留空 = 不修改
      if (f.pattern) {{
        try {{
          var re = new RegExp(f.pattern);
          if (!re.test(val)) {{
            input.closest('.cred-field').querySelector('.cred-field-err').textContent = '格式不符';
            errors.push(f.label + ' 格式不符');
            return;
          }}
        }} catch (e) {{}}
      }}
      input.closest('.cred-field').querySelector('.cred-field-err').textContent = '';
      data[f.key] = val;
    }});
    if (errors.length) {{ showCredMsg(errors.join('; '), 'err'); return; }}
    if (Object.keys(data).length === 0) {{ showCredMsg('没有填写任何字段（留空 = 不修改）', 'warn'); return; }}
  }} else {{
    var raw = document.getElementById('cred-json-input').value.trim();
    if (!raw) {{ showCredMsg('请输入 JSON', 'err'); return; }}
    try {{ data = JSON.parse(raw); }}
    catch (e) {{ showCredMsg('JSON 解析失败: ' + e.message, 'err'); return; }}
    if (typeof data !== 'object' || Array.isArray(data)) {{ showCredMsg('JSON 必须是对象', 'err'); return; }}
  }}
  showCredMsg('保存中...', '');
  try {{
    var resp = await fetch('/api/secrets/' + credLabel + '/bulk', {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{data: data}})
    }});
    var r = await resp.json();
    if (resp.ok) {{
      showCredMsg('✅ 已保存 ' + (r.imported || 0) + ' 个字段', 'ok');
      setTimeout(function() {{ closeCredModal(); location.reload(); }}, 800);
    }} else {{
      showCredMsg('保存失败: ' + (r.detail || JSON.stringify(r)), 'err');
    }}
  }} catch (e) {{
    showCredMsg('请求异常: ' + e, 'err');
  }}
}}

// ── 破解网关：额度/签到状态加载 ──
function loadCrackStatus(label, el) {{
  if (!el || !label) return;
  fetch('/api/crack/' + label + '/status')
    .then(function(resp) {{ return resp.json(); }})
    .then(function(r) {{
      if (!r.supported) {{
        el.innerHTML = '<div class="cs-err">该破解网关未接入状态查询</div>';
        return;
      }}
      if (!r.configured) {{
        el.innerHTML = '<div class="cs-err">凭据未配置，无法查询状态</div>';
        return;
      }}
      if (r.error) {{
        el.innerHTML = '<div class="cs-err">状态查询失败: ' + r.error + '</div>';
        return;
      }}
      var caps = r.capabilities || {{}};
      var account = r.account || '—';
      // 标题：网关名 · 账号（让用户知道是哪个账号登录的）
      var title = (r.displayName || label) + ' · ' + account;
      var html = '<div class="cs-head">' + title + '</div>';
      // 签到行（仅该网关有签到机制时显示）
      if (caps.hasCheckin && r.checkin) {{
        var c = r.checkin;
        var ciText = c.checkedIn
          ? '✅ 已签' + (c.credits ? ' (+' + c.credits + ')' : '')
          : '⚠️ 未签';
        var ciClass = c.checkedIn ? 'cs-checkin-ok' : 'cs-checkin-no';
        html += '<div class="cs-row"><span class="k">签到</span>' +
          '<span class="' + ciClass + '">' + ciText + '</span></div>';
      }}
      // token 到期（有值才显示）
      if (r.refresh && r.refresh.tokenExpireAt) {{
        var exp = r.refresh.tokenExpireAt.replace('T', ' ').slice(0, 16);
        html += '<div class="cs-row"><span class="k">token 到期</span><span>' + exp + '</span></div>';
      }}
      // 最后定时刷新（仅需签到/刷 token 的网关显示）
      if (caps.hasCheckin || caps.hasRefresh) {{
        var last = r.lastDailyRun ? r.lastDailyRun.replace('T', ' ').slice(0, 16) : '';
        html += '<div class="cs-row"><span class="k">最后定时刷新</span>' +
          (last ? '<span>' + last + '</span>' : '<span class="cs-never">尚未运行</span>') + '</div>';
      }}
      // 额度明细
      if (r.quota && r.quota.length) {{
        html += '<div class="cs-quota">';
        r.quota.forEach(function(q) {{
          if (q.error) {{ html += '<div class="cs-err">' + q.error + '</div>'; return; }}
          var limit = (q.limit === undefined || q.limit === null) ? '∞' : q.limit;
          var exp = q.expireAt ? (' · ' + q.expireAt.replace('T', ' ').slice(0, 10)) : '';
          html += '<div class="cs-qrow"><span class="qname">' + q.name + '</span>' +
            '<span>' + q.used + ' / ' + limit + '<span class="qexp">' + exp + '</span></span></div>';
        }});
        html += '</div>';
      }}
      el.innerHTML = html;
    }})
    .catch(function(e) {{
      el.innerHTML = '<div class="cs-err">加载失败: ' + e + '</div>';
    }});
}}

// 页面加载后加载所有 crack 卡片的额度/签到状态
function initCrackStatus() {{
  document.querySelectorAll('.crack-status').forEach(function(el) {{
    loadCrackStatus(el.dataset.label, el);
  }});
}}
document.addEventListener('DOMContentLoaded', initCrackStatus);
setTimeout(initCrackStatus, 300);

// ── 聚合网关（8080）：虚拟模型/会话/熔断状态加载 + 10s 自动刷新 ──
function escHtml(s) {{
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function aggBreakerInfo(state) {{
  if (state === 'tripped') return {{cls: 'agg-dot bad', label: '已熔断'}};
  if (state === 'probing') return {{cls: 'agg-dot warn', label: '探测中'}};
  return {{cls: 'agg-dot ok', label: '正常'}};
}}

function aggMemberDot(m) {{
  if (!m || m.requests === 0) return 'agg-dot dim';
  var bad = (m.err || 0) + (m.degraded || 0);
  if (bad === 0) return 'agg-dot ok';
  if ((m.err || 0) > 0 && (m.err || 0) >= m.requests * 0.5) return 'agg-dot bad';
  return 'agg-dot warn';
}}

function togglePoolDetail(rowEl) {{
  var vmId = rowEl.getAttribute('data-vmid');
  if (!vmId) return;
  var d = document.getElementById('pool-' + vmId);
  if (d) d.open = !d.open;
}}

async function loadAggregateStatus() {{
  var el = document.getElementById('agg-status');
  if (!el) return;
  // 保留用户当前展开的池详情 id（10s 刷新会重写 innerHTML，需恢复 open 状态）
  var openIds = [];
  el.querySelectorAll('details.agg-vm-detail[open]').forEach(function(d){{ openIds.push(d.id); }});
  try {{
    var resp = await fetch('/api/aggregate/status');
    var r = await resp.json();
    if (!r.configured) {{
      el.innerHTML = '<div class="cs-err">聚合网关未配置（targets.json 中缺少聚合 target）</div>';
      return;
    }}
    var sess = r.session || {{}};
    var hitRate = (sess.hit_rate || 0) * 100;
    var cacheSize = sess.cache_size || 0;
    var html = '<div class="cs-head">🔀 聚合网关 · 命中率 ' + hitRate.toFixed(1) + '% · 粘性缓存 ' + cacheSize + ' 条</div>';
    // 池配置 JSON（注入自服务端 <script id="agg-pool-data">）
    var cfgScript = document.getElementById('agg-pool-data');
    var cfg = {{}};
    try {{ cfg = cfgScript ? JSON.parse(cfgScript.textContent || '{{}}') : {{}}; }} catch(e) {{ cfg = {{}}; }}
    // ── 主表：每虚拟模型一行（配置全貌，含无流量 vm）──
    var vms = r.virtual_models || {{}};
    var vmIds = Object.keys(cfg);
    var vmRowHtml = '';
    var i = 0;
    vmIds.forEach(function(vmId) {{
      i++;
      var cfgVm = cfg[vmId] || {{}};
      var defPool = cfgVm.defaultPool || [];
      var fbPool = cfgVm.fallbackPool || [];
      var membersStats = vms[vmId] || {{}};
      var totReq=0, totOk=0, totErr=0, totDeg=0;
      Object.keys(membersStats).forEach(function(mk){{
        var m = membersStats[mk] || {{}};
        totReq += m.requests||0; totOk += m.ok||0; totErr += m.err||0; totDeg += m.degraded||0;
      }});
      var hasTraf = totReq > 0;
      var rate = hasTraf ? (totOk/totReq*100).toFixed(1) + '%' : '—';
      vmRowHtml += '<tr data-vmid="' + escHtml(vmId) + '" onclick="togglePoolDetail(this)" style="cursor:pointer;" title="点击展开池详情">' +
        '<td class="num">' + i + '</td>' +
        '<td class="mid"><code>' + escHtml(vmId) + '</code></td>' +
        '<td class="name">默认池 ' + defPool.length + ' · 降级池 ' + fbPool.length + '</td>' +
        '<td class="mstat">' + (hasTraf?totReq:'—') + '</td>' +
        '<td class="mstat">' + rate + '</td>' +
        '<td class="mstat err">' + (hasTraf?totErr:'—') + '</td>' +
        '<td class="mstat warn">' + (hasTraf?totDeg:'—') + '</td>' +
        '</tr>';
    }});
    if (vmIds.length === 0) {{
      html += '<div class="no-models">(暂无虚拟模型配置)</div>';
    }} else {{
      html += '<table class="model-table"><thead><tr>' +
        '<th>#</th><th>模型 ID</th><th>名称</th><th>请求</th><th>成功率</th><th>错误</th><th>降级</th>' +
        '</tr></thead><tbody>' + vmRowHtml + '</tbody></table>';
    }}
    // ── 池详情折叠：每个 vm 一个 details（默认收起，点击主表行 toggle）──
    vmIds.forEach(function(vmId) {{
      var cfgVm = cfg[vmId] || {{}};
      var defPool = cfgVm.defaultPool || [];
      var fbPool = cfgVm.fallbackPool || [];
      var membersStats = vms[vmId] || {{}};
      function renderPool(pool, label) {{
        if (pool.length === 0) return '<div class="no-models">(' + label + ' 为空)</div>';
        var rows = '';
        for (var j=0; j<pool.length; j++) {{
          var p = pool[j] || {{}};
          var port = p.port;
          var model = p.model || '';
          var w = p.weight;
          var mk = port + ':' + model;
          var ms = membersStats[mk] || {{}};
          var req = ms.requests || 0;
          var ok = ms.ok || 0;
          var err = ms.err || 0;
          var deg = ms.degraded || 0;
          var lraw = ms.avg_latency_ms || 0;
          var hasTraf = req > 0;
          var rateStr = hasTraf ? (ok/req*100).toFixed(1) + '%' : '—';
          var latStr = hasTraf ? lraw.toFixed(0) + 'ms' : '—';
          var wStr = (w === undefined || w === null) ? '—' : String(w);
          rows += '<tr>' +
            '<td class="num">' + (j+1) + '</td>' +
            '<td class="mid"><code>:' + port + '</code> · <code>' + escHtml(model) + '</code></td>' +
            '<td class="mstat">' + wStr + '</td>' +
            '<td class="mstat">' + (hasTraf?req:'—') + '</td>' +
            '<td class="mstat">' + rateStr + '</td>' +
            '<td class="mstat err">' + (hasTraf?err:'—') + '</td>' +
            '<td class="mstat warn">' + (hasTraf?deg:'—') + '</td>' +
            '<td class="mstat">' + latStr + '</td>' +
            '</tr>';
        }}
        return '<div class="agg-pool-block"><div class="agg-vm-head">' + label + '（' + pool.length + ' 成员）</div>' +
          '<table class="model-table"><thead><tr>' +
          '<th>#</th><th>端口 · 模型</th><th>权重</th><th>请求</th><th>成功率</th><th>错误</th><th>降级</th><th>延迟</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table></div>';
      }}
      html += '<details class="agg-vm-detail" id="pool-' + escHtml(vmId) + '">' +
        '<summary><span class="agg-vm-head" style="margin:0; font-size:12.5px;">📦 ' + escHtml(vmId) + ' · ' + defPool.length + ' 默认 + ' + fbPool.length + ' 降级</span></summary>' +
        '<div class="agg-vm-body">' +
        renderPool(defPool, '默认池') +
        renderPool(fbPool, '降级池') +
        '</div></details>';
    }});
    // ── 熔断状态区（保留）──
    var brks = r.breakers || {{}};
    var brkKeys = Object.keys(brks);
    html += '<div class="agg-vm"><div class="agg-vm-head">熔断状态</div>';
    if (brkKeys.length === 0) {{
      html += '<div class="agg-vm-row"><span class="m">无熔断端口</span></div>';
    }} else {{
      brkKeys.forEach(function(port) {{
        var b = brks[port] || {{}};
        var info = aggBreakerInfo(b.state);
        html += '<div class="agg-brk"><span class="' + info.cls + '"></span>' +
          '<span class="m">:' + escHtml(port) + '</span>' +
          '<span>' + info.label + '</span>' +
          (b.reason ? '<span class="reason">' + escHtml(b.reason) + '</span>' : '') + '</div>';
      }});
    }}
    html += '</div>';
    el.innerHTML = html;
    // 恢复用户展开的池详情（10s 刷新重写 innerHTML 后 open 状态会丢失）
    openIds.forEach(function(id){{
      var d = document.getElementById(id);
      if (d) d.open = true;
    }});
  }} catch (e) {{
    el.innerHTML = '<div class="cs-err">加载失败: ' + e + '</div>';
  }}
}}

// 聚合网关：页面加载立即拉取一次 + 每 10s 自动刷新（仅 8080 卡片，独立于破解卡片刷新）
(function() {{
  if (document.getElementById('agg-status')) {{
    loadAggregateStatus();
    setInterval(loadAggregateStatus, 10000);
  }}
}})();
</script>
</body>
</html>"""


# Catch-all route to handle OAuth and other unexpected endpoints
# Note: FastAPI will NOT match "/" to this because root is defined above as exact match
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str):
    """Catch-all for any unhandled endpoints (OAuth, health checks, etc.)"""
    # Skip root path — handled by root() above
    if path == "" or path == "/":
        return {"message": "Anthropic Proxy for LiteLLM"}
    body = None
    try:
        body = await request.body()
        if body:
            body = body.decode("utf-8", errors="replace")[:500]
    except:
        pass
    logger.warning(f"⚠️ UNHANDLED: {request.method} /{path} body={body}")
    return JSONResponse(
        content={"error": f"Endpoint /{path} not implemented by this proxy"},
        status_code=404,
    )


# Define ANSI color codes for terminal output
class Colors:
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"


def log_request_beautifully(
    method, path, claude_model, openai_model, num_messages, num_tools, status_code
):
    """Log requests in a beautiful, twitter-friendly format showing Claude to OpenAI mapping."""
    # Format the Claude model name nicely
    claude_display = f"{Colors.CYAN}{claude_model}{Colors.RESET}"

    # Extract endpoint name
    endpoint = path
    if "?" in endpoint:
        endpoint = endpoint.split("?")[0]

    # Extract just the OpenAI model name without provider prefix
    openai_display = openai_model
    if "/" in openai_display:
        openai_display = openai_display.split("/")[-1]
    openai_display = f"{Colors.GREEN}{openai_display}{Colors.RESET}"

    # Format tools and messages
    tools_str = f"{Colors.MAGENTA}{num_tools} tools{Colors.RESET}"
    messages_str = f"{Colors.BLUE}{num_messages} messages{Colors.RESET}"

    # Format status code
    status_str = (
        f"{Colors.GREEN}✓ {status_code} OK{Colors.RESET}"
        if status_code == 200
        else f"{Colors.RED}✗ {status_code}{Colors.RESET}"
    )

    # Put it all together in a clear, beautiful format
    log_line = f"{Colors.BOLD}{method} {endpoint}{Colors.RESET} {status_str}"
    model_line = f"{claude_display} → {openai_display} {tools_str} {messages_str}"

    logger.info(log_line)
    logger.info(model_line)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Run with: python server.py [--port 8082]")
        print("")
        print("Set PREFERRED_PROVIDER environment variable to choose backend:")
        print("  openai       OpenAI compatible API (default)")
        print("  anthropic    Anthropic Claude API")
        print("  google       Google Gemini API")
        print("  qclaw        QClaw upstream (mmgrcalltoken.3g.qq.com, auto-decrypt API key)")
        print("")
        print("Example: PREFERRED_PROVIDER=qclaw python server.py")
        sys.exit(0)

    port = 8081
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])
    if "-p" in sys.argv:
        idx = sys.argv.index("-p")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Configure uvicorn to run with minimal logs
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="error")
