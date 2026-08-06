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
import inspect
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

# 主模块别名：python server.py 运行时，延迟导入的 from server import X
# 必须解析到已加载的 __main__，否则会重新执行整个 server.py（双份初始化）
if __name__ == "__main__" and "server" not in sys.modules:
    sys.modules["server"] = sys.modules["__main__"]

# QClaw 网关符号（从 gateways/qclaw.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.qclaw import (
    _QCLAW_ALLOWED_KEYS,
    _clean_qclaw_body,
    _passthrough_to_qclaw,
    _dpapi_unprotect,
    _decrypt_qclaw_api_key,
    _qclaw_provider,
)

# Copilot 网关符号（从 gateways/copilot.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.copilot import (
    _RESPONSES_FINISH_REASON_MAP,
    _copilot_chat_to_responses_body,
    _copilot_responses_to_chat_body,
    _ClientDisconnected,
    _copilot_responses_usage_to_chat,
    _copilot_stream_chunk,
    _write_copilot_responses_stream,
    _copilot_model_name,
    _is_claude_family_model,
    _copilot_provider,
)

# CodeBuddy 网关符号（从 gateways/codebuddy.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.codebuddy import (
    _CODEBUDDY_DROP_KEYS,
    _CODEBUDDY_SYS_REWRITES,
    _clean_codebuddy_body,
    _aggregate_codebuddy_stream,
    _normalize_codebuddy_sse_line,
)

# Gemini 原生协议转换（从 gateways/gemini_native.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.gemini_native import (
    _GEMINI_NATIVE_BASE,
    _openai_to_gemini_body,
    _gemini_to_openai_response,
    _gemini_chunk_to_openai,
    _handle_gemini_native,
    _gemini_provider,
)

# Trae Work 网关符号（从 gateways/trae_work.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.trae_work import _handle_traework

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
        from gateways.aggregator import engine as _agg
        _AGGREGATOR_ENGINE = _agg.AggregatorEngine.from_target(_agg_preinit)
        _AGGREGATOR_CONFIG_SIG = json.dumps(_agg_preinit, sort_keys=True, ensure_ascii=False)
        print(f"🚀 [aggregator] 聚合网关引擎预初始化（{len(_agg_preinit.get('virtualModels', {}))} 个虚拟模型）")

    # P2: 启动即构建 ModelRegistry 单一事实源（lifespan 运行时，所有类已定义）
    global _MODEL_REGISTRY
    _MODEL_REGISTRY = ModelRegistry({
        "targets": _TARGETS,
        "modelDefaults": _MODELS_CFG.get("modelDefaults", {}),
        "models": _MODELS_CFG.get("models", []),
    })

    # ── 启动配置热重载 watcher ──
    watcher_task = asyncio.create_task(_config_watcher())
    from gateways.aggregator.http_adapter import _aggregator_prober
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
_MODEL_REGISTRY = None  # P2: ModelRegistry 内存索引，热重载时重建（dashboard 渲染消费的单一事实源）
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
# 符号已拆分到 gateways/gemini_native.py（见文件顶部 import）。



# ── Gemini 原生协议转换（handler=gemini-native）──
# 符号已拆分到 gateways/gemini_native.py（见文件顶部 import）。


# ── Trae Work 协议转换（handler=trae-work）──
# 符号已拆分到 gateways/trae_work.py（见文件顶部 import）。


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
    global _TARGETS, _SECRETS, _MODELS_CFG, COPILOT_GHE_TOKEN, _MODEL_REGISTRY
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        for e in errors:
            logger.warning(f"targets.json 配置错误: {e['path']}: {e['msg']}")
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
    global _TARGETS, _SECRETS, _MODELS_CFG, COPILOT_GHE_TOKEN, _MODEL_REGISTRY
    changes = []
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        summary = [f"{e['path']}: {e['msg']}" for e in errors]
        logger.error(f"配置校验失败，拒绝重载: {summary}")
        return [f"❌ 校验失败: {summary}"]
    _TARGETS = cfg.get("targets", [])
    _MODELS_CFG["models"] = cfg.get("models", [])
    _MODELS_CFG["modelDefaults"] = cfg.get("modelDefaults", {"defaultPort": 8082})
    # P2: 重建 ModelRegistry 单一事实源（dashboard 渲染改读它，targets.json 结构不变）
    _MODEL_REGISTRY = ModelRegistry({
        "targets": _TARGETS,
        "modelDefaults": _MODELS_CFG.get("modelDefaults", {}),
        "models": _MODELS_CFG.get("models", []),
    })
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
                from gateways.aggregator import engine as _agg
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


# ─── 统一模型列表接口（收敛四种 modelsSource）───

_STATIC_SOURCE_BY_LABEL = {"codebuddy": "codebuddy", "qclaw": "qclaw", "trae-work": "trae-work"}


def _static_model_source(target: dict) -> str:
    """静态 models[] 的来源标记：label 前缀优先，否则回落 handler。"""
    label = str(target.get("label") or "")
    for prefix, source in _STATIC_SOURCE_BY_LABEL.items():
        if label == prefix or label.startswith(prefix + "-"):
            return source
    return str(target.get("handler") or "passthrough")


def _live_model_ids(target: dict) -> List[str]:
    """copilot 上游实时模型 id 列表。

    _fetch_live_models 是 async；测试以同步 MagicMock 替换它，故按返回值是否
    awaitable 运行时判定，而非 iscoroutinefunction（mock 后函数对象已被替换）。
    """
    result = _fetch_live_models(target)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return [str(m) for m in (result or [])]


def _target_model_source(target: dict) -> str:
    """target 的模型来源标记，与 _get_target_models 的分派规则同源。

    纯配置推断，不触发任何上游请求 —— 因此可在 async 的 dashboard 渲染路径里
    安全调用（_get_target_models 对 copilot 会 asyncio.run 拉取上游，在运行中的
    事件循环里会抛 RuntimeError，且每张卡片一次网络往返）。
    """
    label = str(target.get("label") or "")
    if target.get("listenPort") == 8081 or label == "anthropic-compatible":
        return "anthropic"
    handler = target.get("handler")
    if handler == "aggregator":
        return "aggregator"
    if handler == "copilot":
        return "copilot"
    return _static_model_source(target)


def _build_target_models(target: dict, source: str, live_ids: List[str]) -> List[dict]:
    """按 source 装配模型列表。copilot 的上游 id 由调用方注入。

    唯一实现，_get_target_models（同步）与 _get_target_models_async（async 路径）
    共用；copilot 的上游拉取方式是二者唯一的差异，故作为参数传入而非在此分支。
    """
    if source == "anthropic":
        return [{**m, "source": "anthropic"} for m in _anthropic_port_models()]

    if source == "aggregator":
        return [
            {"id": str(vid), "display_name": str(vid), "aliases": [], "target": {}, "source": "aggregator"}
            for vid in (target.get("virtualModels") or {})
        ]

    if source == "copilot":
        # enabled 取自 targets.json 白名单：上游返回的是全量模型，而面板只展示
        # 已开启的那些。一律 True 会把被关掉的模型重新显示出来（copilot 曾由 4 变 44）。
        local_enabled = {
            str(m.get("id")): m.get("enabled", True)
            for m in (target.get("models") or []) if isinstance(m, dict) and m.get("id")
        }
        return [
            {"id": mid, "display_name": _humanize_model_name(mid), "aliases": [],
             "enabled": local_enabled.get(mid, True), "target": {}, "source": "copilot"}
            for mid in live_ids
        ]

    out: List[dict] = []
    for m in (target.get("models") or []):
        mid = str(m.get("id")) if isinstance(m, dict) else str(m)
        if not mid:
            continue
        out.append({
            "id": mid,
            # 无显式 display_name 时回落 _humanize_model_name，与 _anthropic_port_models
            # 及 _model_details_html 的既有渲染保持一致（回落成裸 id 会改变面板显示名）。
            "display_name": (m.get("display_name") if isinstance(m, dict) else None) or _humanize_model_name(mid),
            "aliases": list(m.get("aliases") or []) if isinstance(m, dict) else [],
            # enabled 是模型白名单开关，必须原样带出：dashboard 只渲染 enabled 的模型，
            # 缺字段会被 _model_details_html 默认成 True，把已关闭的模型重新显示出来。
            "enabled": m.get("enabled", True) if isinstance(m, dict) else True,
            "target": {},
            "source": source,
        })
    return out


def _get_target_models(label: str) -> List[dict]:
    """统一接口（同步）：返回某 target 的模型列表，收敛四种 modelsSource。

    返回 [{id, display_name, aliases, enabled, target, source}]，source 标记来源：
    anthropic（8081 顶层 models[]）/ aggregator（virtualModels）/
    copilot（上游实时拉取）/ codebuddy|qclaw|trae-work|<handler>（静态 models[]）。
    label 不存在时返回 []。

    注意：copilot target 会同步拉取上游（asyncio.run），故不可在运行中的事件
    循环里调用——async 路径请用 _get_target_models_async。
    """
    target = next((t for t in _TARGETS if t.get("label") == label), None)
    if target is None:
        return []
    source = _target_model_source(target)
    live_ids = _live_model_ids(target) if source == "copilot" else []
    return _build_target_models(target, source, live_ids)


async def _get_target_models_async(label: str) -> List[dict]:
    """统一接口（async）：与 _get_target_models 同结果，copilot 走 await 拉取。

    async 端点（FastAPI 路由）必须用这个版本：同步版对 copilot 会
    asyncio.run() 到运行中的事件循环上，直接抛 RuntimeError。
    """
    target = next((t for t in _TARGETS if t.get("label") == label), None)
    if target is None:
        return []
    source = _target_model_source(target)
    live_ids: List[str] = []
    if source == "copilot":
        live_ids = [str(m) for m in (await _fetch_live_models(target) or [])]
    return _build_target_models(target, source, live_ids)


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
            from gateways.aggregator.http_adapter import _handle_aggregate_request
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

def _anthropic_provider(req, litellm_req, _orig):
    """Anthropic / 自定义 Anthropic API"""
    litellm_req["api_key"] = ANTHROPIC_API_KEY
    if ANTHROPIC_BASE_URL:
        litellm_req["api_base"] = ANTHROPIC_BASE_URL
        logger.debug(f"Anthropic: base={ANTHROPIC_BASE_URL}")
    else:
        logger.debug(f"Anthropic: default")
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

def _scan_dangling_refs_cfg(cfg: dict) -> List[dict]:
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

    cfg 形如 {"targets": [...], "models": [...]}；无参入口 _scan_dangling_refs()
    传入全局 _TARGETS / _MODELS_CFG 组装的 cfg，行为与参数化前完全一致。
    """
    items: List[dict] = []
    targets = cfg.get("targets") or []
    top_models = cfg.get("models") or []
    # 端口 → 该端口可被请求的模型名集合（None 表示不限制，不做模型级校验）
    port_models: Dict[int, Optional[set]] = {}
    port_labels: Dict[int, str] = {}
    for t in targets:
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

    for idx, m in enumerate(top_models):
        if not isinstance(m, dict):
            continue
        ref = m.get("target")
        if isinstance(ref, dict):
            _check(f"models[{idx}].target", ref, f"模型定义 {m.get('name') or idx}")

    for t in targets:
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


def _scan_dangling_refs() -> List[dict]:
    """无参入口：扫描当前全局配置（_TARGETS / _MODELS_CFG）的悬空引用。

    保留无参签名，`/api/config/dangling` 等既有调用点无需改动。
    """
    return _scan_dangling_refs_cfg({
        "targets": _TARGETS,
        "models": _MODELS_CFG.get("models", []) or [],
    })


# handler → modelsSource 的直接映射（handler 已经明确表达上游协议来源）
_MODELS_SOURCE_BY_HANDLER: Dict[str, str] = {
    "copilot": "copilot",
    "aggregator": "aggregator",
    "gemini-native": "gemini-native",
    "qclaw": "qclaw",
    "trae-work": "trae-work",
}
# handler=passthrough 时按 label 细分（label 是供应商身份，handler 只说明转发方式）
_MODELS_SOURCE_BY_LABEL: Dict[str, str] = {
    "codebuddy": "codebuddy",
    "qclaw": "qclaw",
    "trae-work": "trae-work",
    "anthropic": "anthropic",
    "anthropic-compatible": "anthropic",
}
_ANTHROPIC_ENTRY_PORT = 8081


def _derive_models_source(target: dict) -> str:
    """推导 target 的模型来源枚举值。

    优先级：handler 直映射 > Anthropic 入口（8081 / anthropic* label）> label 细分 > passthrough。
    """
    handler = target.get("handler")
    direct = _MODELS_SOURCE_BY_HANDLER.get(handler)
    if direct:
        return direct
    label = target.get("label") or ""
    if target.get("listenPort") == _ANTHROPIC_ENTRY_PORT:
        return "anthropic"
    return _MODELS_SOURCE_BY_LABEL.get(label, "passthrough")


class ModelRegistry:
    """targets 配置的只读内存索引（纯函数式：构建后不再读全局状态）。

    三个属性：
      byPort       — listenPort → {label, handler, category, models, target}
      dangling     — 与 _scan_dangling_refs_cfg(cfg) 等价的悬空引用列表
      capabilities — listenPort → {can_prune, modelsSource}

    can_prune 与 dashboard 现有判据保持一致：显式 hasModels=true 或 handler=copilot
    （只有 copilot 系上游提供 /models 列表，才谈得上"对照上游清理过期模型"）。
    """

    __slots__ = ("byPort", "dangling", "capabilities")

    def __init__(self, cfg: dict) -> None:
        targets = cfg.get("targets") or []
        by_port: Dict[int, dict] = {}
        caps: Dict[int, dict] = {}
        for t in targets:
            port = t.get("listenPort")
            if port is None:
                continue
            by_port[port] = {
                "label": t.get("label"),
                "handler": t.get("handler"),
                "category": t.get("category"),
                "models": list(t.get("models") or []),
                "target": t,
            }
            caps[port] = {
                "can_prune": t.get("hasModels") is True or t.get("handler") == "copilot",
                "modelsSource": _derive_models_source(t),
            }
        self.byPort = by_port
        self.capabilities = caps
        self.dangling = _scan_dangling_refs_cfg(cfg)
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


# ─── 统一管理面板（dashboard 包）─────────────────────────────────────
# CSS/HTML 渲染与全部 /api/* 路由已拆分到 dashboard/routes.py，逻辑原样搬迁。
# 挂载点必须满足两个约束：
#   1. 在上面这些被 dashboard 复用的符号（_humanize_model_name /
#      _scan_dangling_refs / ModelRegistry / _fetch_live_models）定义之后
#      —— dashboard.routes 在模块级 import 它们；
#   2. 在 catch_all 之前 —— 否则 "/{path:path}" 会先匹配掉 /dashboard 与
#      /api/*，与拆分前的注册顺序不一致。
from dashboard.routes import dashboard_router  # noqa: E402

app.include_router(dashboard_router)


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
