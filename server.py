from fastapi import FastAPI, Request, HTTPException
import uvicorn
import logging
from logging.handlers import TimedRotatingFileHandler
import json
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import AsyncIterator, Callable, List, Dict, Any, Optional, Tuple, Union, Literal
import httpx
import os
import asyncio
from urllib.parse import urlparse
import ipaddress
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.responses import HTMLResponse
import uuid
import time
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
    _dpapi_unprotect,
    _decrypt_qclaw_api_key,
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
)

# Trae Work 网关符号（从 gateways/trae_work.py 拆分导入；内部对 server 模块为延迟导入，无循环依赖）
from gateways.trae_work import _handle_traework


# 模型注册表（从 gateways/models.py 下沉；内部对 server 模块为延迟导入，无循环依赖）
from gateways.models import (
    ModelRegistry,
    _anthropic_port_models,
    _build_models_list,
    _derive_models_source,
    _fetch_downstream_models,
    _fetch_live_models,
    _get_target_models,
    _get_target_models_async,
    _humanize_model_name,
    _scan_dangling_refs,
    _scan_dangling_refs_cfg,
    _target_model_source,
)
import gateways.models as _gmodels  # 模型缓存状态（_DOWNSTREAM_MODELS_CACHE）归 gateways.models 模块所有，需模块属性实时读取
from gateways.messages_contract import filter_messages_request  # passthrough /v1/messages 请求体字段白名单过滤

# 翻译层符号（从 server.py 拆出至 gateways/translate.py；内部对 server 模块为延迟导入，
# 现仅保留 token 估算族——LiteLLM 翻译链与 provider 策略已随分支 C 删除）
from gateways.translate import (
    _estimate_messages_tokens,
)

# 错误翻译层（从 gateways/errors.py 下沉；内部对 server 模块为延迟导入，无循环依赖）
from gateways.errors import (
    _is_auth_expired_error,
    _VENDOR_ERROR_MAPS,
    _VENDOR_ERROR_PATTERNS,
    _VENDOR_RETRY_AFTER,
    _map_upstream_error,
    _vendor_body_retryable,
)

# 破解网关公共能力（额度/签到/刷新状态查询 + tc 解密）
try:
    import crack_common
except Exception:
    crack_common = None

# ─── 配置事实源：targets.json（server 段承载主服务运行配置，.env 已废弃）───
# 必须在日志初始化（下方 DEBUG/LOG_*）与所有 _SERVER_CFG 消费点之前完成加载。
import config_store as _cfg
_CFG = _cfg.load_targets()
_SERVER_CFG = _CFG.get("server", _cfg.DEFAULT_SERVER_CONFIG)

# Debug mode：环境变量 DEBUG 优先（兼容 systemctl edit 临时开），未设置时回退 targets.json
# server.log.debug（配置统一）。注意：logger 初始化在下方读取本值，勿在此之后改动。
DEBUG = os.environ.get("DEBUG", str(_SERVER_CFG["log"]["debug"])).lower() == "true"
LOG_FILE = _SERVER_CFG["log"]["file"]  # 非空则同时输出到文件
LOG_RETENTION_DAYS = int(_SERVER_CFG["log"]["retentionDays"])
LOG_ROTATE_WHEN = _SERVER_CFG["log"]["rotateWhen"]
LOG_ROTATE_INTERVAL = int(_SERVER_CFG["log"]["rotateInterval"])

# Response cache configuration
CACHE_ENABLED = bool(_SERVER_CFG["cache"]["enabled"])
CACHE_MAX_SIZE = int(_SERVER_CFG["cache"]["maxSize"])
CACHE_TTL_SECONDS = int(_SERVER_CFG["cache"]["ttlSeconds"])
CACHE_MAX_ITEM_SIZE_KB = int(_SERVER_CFG["cache"]["maxItemSizeKb"])

# Configure logging
_log_level = logging.DEBUG if DEBUG else logging.INFO
_log_fmt = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(level=_log_level, format=_log_fmt)
logger = logging.getLogger(__name__)

# Module-level timeout constant for target forwarding engine (used in _handle_target_request)
# Can be monkeypatched in tests for fast timeout simulation
_TARGET_HTTPX_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


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

# #7 Anthropic 标准 error.type → HTTP 状态码映射（供 messages 流式路径的
# embedded-200 SSE error 帧检测使用，与 gateways/errors.py 的 _ANTHROPIC_ERROR_MAPS
# 目标状态码保持一致）。
_ANTHROPIC_ERR_TYPE_TO_STATUS = {
    "authentication_error": 401,
    "invalid_request_error": 400,
    "permission_error": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "overloaded_error": 503,
    "timeout_error": 504,
    "api_error": 500,
}

# #8 config-gated Anthropic-frame `event: error` 流内哨兵开关（默认关闭，未验证前
# 不改变既有行为）。与 #7 的 embedded-200 检测互补：#7 只在整流读完后做一次性扫描
# （已在 create_message 的 passthrough messages 分支内联实现），本开关服务于未来
# 逐帧扫描场景的守护函数 _guard_anthropic_sse_error_frame（见下）。
# 显式区别于 normalizeSse（server_http._write_response 的 OpenAI 帧规范化）——
# 那是改写 OpenAI SSE 帧结构，对 Anthropic 帧结构不适用，不可复用。
ANTHROPIC_SSE_ERROR_GUARD_ENABLED = os.environ.get(
    "ANTHROPIC_SSE_ERROR_GUARD_ENABLED", "false"
).lower() == "true"


def _guard_anthropic_sse_error_frame(frame_text: str) -> Optional[Tuple[int, str, str]]:
    """识别单个 Anthropic 格式 SSE 帧是否为 `event: error` 错误帧，命中则翻译。

    与 `normalizeSse`（server_http._write_response 的 OpenAI SSE 帧改写）
    是两回事，绝不可混用：normalizeSse 面向 OpenAI chat.completions SSE 帧
    结构（`choices[].delta` 等），Anthropic 帧结构完全不同
    （`event: message_start/content_block_delta/...`），套用 normalizeSse
    会把 Anthropic 帧错误地当成 OpenAI 帧解析/改写。本函数只做“识别 error
    帧 → 翻译”，不改写任何非 error 帧的字节。

    参数 ``frame_text`` 是一个完整 SSE 帧（不含帧间分隔空行，形如
    ``event: error\\ndata: {...}``）。

    返回 ``(http_status, err_type, err_message)``；不是可识别的 error 信封
    （非 error 帧 / data 缺失 / JSON 解析失败 / 结构不符）一律返回
    ``None`` —— fail-open，调用方对 None 必须原样透传该帧，不中断流。
    """
    if "event: error" not in frame_text and "event:error" not in frame_text:
        return None
    data_line = None
    for line in frame_text.splitlines():
        if line.startswith("data:"):
            data_line = line[len("data:"):].strip()
            break
    if not data_line:
        return None
    try:
        err_obj = json.loads(data_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(err_obj, dict):
        return None
    err_detail = err_obj.get("error")
    if not isinstance(err_detail, dict):
        return None
    err_type = err_detail.get("type", "api_error")
    err_msg = err_detail.get("message", "SSE error frame")
    err_status = _ANTHROPIC_ERR_TYPE_TO_STATUS.get(err_type, 500)
    return (err_status, err_type, err_msg)


def _guard_anthropic_sse_stream(raw_bytes: bytes) -> Optional[Tuple[int, str, str]]:
    """对整段 Anthropic SSE 响应字节做 `event: error` / 畸形终止帧扫描（config-gated 调用方）。

    两级检测，均 fail-open（识别不了就返回 None，调用方须原样透传，不中断流）：
    1. 逐帧（以空行分隔）调用 `_guard_anthropic_sse_error_frame`——严格匹配
       `event: error` + 合法 JSON error 信封的帧，命中即返回其翻译结果。
    2. 若未命中任何结构化 error 帧，回退用 (T6 broadened) `_map_upstream_error`
       对整段文本做子串匹配——覆盖“畸形终止帧”场景：上游未按标准 SSE error
       信封格式收尾，而是吐出裸文本/非 JSON 错误片段（如限流/过载文案），
       命中映射表关键字仍可翻译；未命中任何已知特征则返回 None。

    本函数只读不改：不修改 `raw_bytes`，不改写任何非 error 帧。仅用于
    “是否需要拦截整个响应”的判定，真正的透传字节流由调用方另行处理。
    """
    try:
        text = raw_bytes.decode("utf-8")
    except Exception:
        return None
    for frame in text.split("\n\n"):
        result = _guard_anthropic_sse_error_frame(frame)
        if result is not None:
            return result
    fallback = _map_upstream_error(text)
    if fallback is not None:
        status, err_type = fallback
        return (status, err_type, "malformed terminal SSE frame recognized via vendor error map")
    return None


@asynccontextmanager
async def lifespan(app):
    # 网关抓包：CAPTURE_GATEWAY=true 时激活
    if os.environ.get("CAPTURE_GATEWAY", "").lower() == "true":
        try:
            from _gateway_capture import activate_capture, get_capture_file  # pyright: ignore[reportMissingImports] - 可选调试模块（CAPTURE_GATEWAY=true 才存在）
            activate_capture()
            logger.info(f"📡 Gateway capture activated → {get_capture_file()}")
        except Exception as _ce:
            logger.warning(f"Failed to activate gateway capture: {_ce}")

    # 启动诊断：验证 QClaw 链路是否正常
    import httpx as _httpx
    _qclaw_diag_base = _qclaw_base_url()
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
    try:
        await _fetch_downstream_models()
        logger.info(f"startup: preloaded {len(_gmodels._DOWNSTREAM_MODELS_CACHE or [])} downstream models")
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
    # 8081 Anthropic 由 uvicorn FastAPI 处理（不在此处启动）；排除 handler=="anthropic"
    for t in _TARGETS:
        if not t.get("enabled", True):
            print(f"⏭️  [{t['label']}] disabled, skip")
            continue
        if t.get("handler") == "anthropic":
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

    # ── usage 持久化：建表 + 启动 60s flush 循环（旁路观测，失败不影响启动）──
    try:
        from gateways import usage_store as _ustore
        _ustore.init_db()
    except Exception as _ue:
        logger.warning(f"usage_store: init skipped: {_ue}")
    usage_flush_task = asyncio.create_task(_usage_flush_loop())

    # ── nous 凭据同步：定时从 hermes 容器 auth.json 拷贝 access_token 到 secrets.json
    #    （hermes 负责 token 刷新，代理只同步，见 gateways/nous.py docstring）
    nous_sync_task = None
    try:
        from gateways.nous import nous_sync_loop
        nous_sync_task = asyncio.create_task(nous_sync_loop())
        # 启动即同步一次（拿初值 + 提前触发过期 token 刷新）
        await asyncio.to_thread(_nous_sync_once)
    except Exception as _ne:
        logger.warning(f"nous: 同步器启动失败（不影响主服务）: {_ne}")

    yield

    # 停止 nous 凭据同步任务
    if nous_sync_task is not None:
        nous_sync_task.cancel()
        try:
            await nous_sync_task
        except asyncio.CancelledError:
            pass

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

    # 停止 usage flush 循环，并同步 flush 最后一轮增量（否则丢最后 ≤60s 的计数）
    usage_flush_task.cancel()
    try:
        await usage_flush_task
    except asyncio.CancelledError:
        pass
    try:
        _flush_usage_accum()
    except Exception as _fe:
        logger.warning(f"usage final flush failed: {_fe}")
    try:
        _flush_anthropic_accum()
    except Exception as _fe:
        logger.warning(f"anthropic usage final flush failed: {_fe}")
    try:
        _flush_aggregator_accum()
    except Exception as _fe:
        logger.warning(f"aggregator usage final flush failed: {_fe}")

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

# ─── 管理面 FastAPI app（dashboard + 管理 API，独立端口）───
# dashboard_app 复用主 app 的全局状态（_TARGETS/_MODELS_CFG/_SECRETS），
# 数据由主 app 的 lifespan / _reload_targets 维护，无需独立 lifespan。
# 路由挂载由 T7 完成（当前为空挂骨架，仅占端口监听）。
dashboard_app = FastAPI()

from fastapi.exceptions import RequestValidationError

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

# ─── QClaw 上游直连配置 ───
# 上游 LLM 接口（OpenAI 兼容），从 QClaw 客户端本地存储解密 API Key。
# base URL 不再读 server 段（已删除 server.qclaw），由 _qclaw_base_url() 在调用时
# 从 qclaw target（targetHost + routePrefix）实时推导（见下方函数，定义在 _TARGETS 之后）。


QCLAW_API_KEY = _decrypt_qclaw_api_key()
if QCLAW_API_KEY:
    print(f"🔑 QClaw API Key decrypted: {QCLAW_API_KEY[:12]}...{QCLAW_API_KEY[-4:]}")
else:
    print("⚠️  QClaw API Key not available (set QCLAW_API_KEY env or ensure QClaw client is logged in)")

# ─── GitHub Copilot Enterprise 配置 ───
# COPILOT_GHE_TOKEN：私密凭据，已收敛到 secrets.json copilot_token 字段（唯一事实源，
# 与 8082 企业 GHE target 的 secretRef 同源）。模块加载时从 env 读一次作初始兜底；
# _load_vendor_targets() / _reload_targets() / _refresh_secrets() 热重载时从 secrets.json
# 覆盖（dashboard 可编辑、热生效）。
COPILOT_GHE_TOKEN = os.environ.get("COPILOT_GHE_TOKEN", "")
# 其余 COPILOT_*（host/integrationId/模型角色）不再是模块级常量：模块加载时 _TARGETS
# 尚为空列表（_load_vendor_targets 在 lifespan 才填充），且已删除 server.copilot 段。
# 改为函数化实时解析（见下方 _copilot_ghe_host/_copilot_integration_id/_copilot_api_base/
# _copilot_big_model/_copilot_medium_model/_copilot_small_model，定义在 _TARGETS 之后），
# 每次调用从第一个 enabled 的 copilot handler target 推导，天然随热重载生效。

# ─── 统一透传引擎配置（targets.json 驱动）───
_TARGETS: list = []
_MODEL_REGISTRY = None  # P2: ModelRegistry 内存索引，热重载时重建（dashboard 渲染消费的单一事实源）
_SECRETS: dict = {}
_TARGET_STATS: Dict[str, dict] = {}
# 模型级统计：{ label: { model_name: {"requests": N, "ok": N, "err": N, "translated429": N,
#                                     "error_types": {分类标签: N}} } }
# 值类型是 int | dict：计数字段为 int，error_types 是嵌套直方图（由 _bump_model_error 维护）。
_MODEL_STATS: Dict[str, Dict[str, Dict[str, Any]]] = {}
# 模型别名/转发目标配置（targets.json 顶层 models[] + modelDefaults）
_MODELS_CFG: dict = {"models": [], "modelDefaults": {"defaultPort": 8082}}

# ─── token-saver 开关（targets.json 的 8081 anthropic target 内 tokenSaver 字段）───
# 与 _MODELS_CFG 同源（都从 handler=="anthropic" 的 target 读），由 _load_token_saver_cfg()
# 在启动与热重载两处刷新。跨模块（anthropic_convert.py）访问必须走 `import server as _srv`
# + `_srv._TOKEN_SAVER_CFG` 模块属性，禁止 `from server import` 值拷贝（热重载后会读到旧快照）。
_TOKEN_SAVER_DEFAULTS: dict = {"enabled": False, "minSize": 1024, "maxSize": 200000}
_TOKEN_SAVER_CFG: dict = dict(_TOKEN_SAVER_DEFAULTS)


def _load_token_saver_cfg(anthropic_target) -> None:
    """从 anthropic target 刷新 _TOKEN_SAVER_CFG（缺失字段回落默认，就地替换全局）。"""
    global _TOKEN_SAVER_CFG
    raw = (anthropic_target or {}).get("tokenSaver") or {}
    if not isinstance(raw, dict):
        raw = {}
    _TOKEN_SAVER_CFG = {**_TOKEN_SAVER_DEFAULTS, **raw}


# ─── usage 持久化累加器（内存累加 → 60s 异步 flush 到 SQLite）───
# 为什么不在热转发路径直接写库：SQLite 写是同步阻塞 IO，落在每个请求上会拖慢转发。
# 这里只做纯内存自增（O(1)、无锁——单事件循环内全部同步代码，无 await 穿插，天然原子），
# 由 _usage_flush_loop() 周期性搬运；进程退出前 lifespan 关闭段再 flush 一次兜底。
#
# 结构（两个维度，落库时都写进同一张 usage_daily 表）：
#   _TODAY_ACCUM["targets"][label]         = {"totalRequests": N, "translated429": N}
#   _TODAY_ACCUM["models"][(label, model)] = {"requests": N, "ok": N, "err": N, "translated429": N,
#                                              "error_types": {分类标签: N}}
# models 维度的值类型是 int | dict（error_types 为嵌套直方图，由 _bump_model_error 维护）。
# targets 维度落为 model="__target__" 的伪行（刻意不给表加列——总请求数在 get_trend
# 里就是该 label 下所有行 requests 之和，含 __target__ 行）。
_USAGE_TARGET_PSEUDO_MODEL = "__target__"
_TODAY_ACCUM: Dict[str, Dict[Any, Dict[str, Any]]] = {"targets": {}, "models": {}}

# 8081 翻译入口独立统计累加器（落 anthropic_daily 表，与底层 usage_daily 隔离）
# 为什么单独一张表：8081 是翻译入口，它的一次请求会在下游 target 端口再记一次；
# 混进 usage_daily 会双计。独立表让 dashboard 能分别展示"入口视角"与"端口视角"。
_ANTHROPIC_ACCUM: Dict[str, int] = {"totalRequests": 0, "passthroughOk": 0, "passthroughError": 0}

# 8080 聚合网关独立统计累加器（落 aggregator_daily 表；member 用 "port:model" 字符串做 key）
# key=(vm_id, "port:model")；由 engine.note_request 经 _bump_aggregator_usage 旁路灌入。
_AGGREGATOR_ACCUM: Dict[tuple, Dict[str, Any]] = {}


def _bump_usage_target(label: str, field: str, n: int = 1):
    """累加 target 维度用量（label 级汇总）。旁路观测，绝不因异常影响主链路。"""
    try:
        d = _TODAY_ACCUM["targets"].setdefault(label, {"totalRequests": 0, "translated429": 0})
        d[field] = d.get(field, 0) + n
    except Exception:
        pass


def _bump_anthropic_usage(field: str, n: int = 1):
    """累加 8081 翻译入口用量。旁路观测，绝不因异常影响主链路。"""
    try:
        _ANTHROPIC_ACCUM[field] = _ANTHROPIC_ACCUM.get(field, 0) + n
    except Exception:
        pass


def _bump_aggregator_usage(vm_id, port, model, outcome, error_type):
    """聚合网关 note_request 旁路累加（engine.py 调用）。旁路观测，异常绝不回抛。"""
    try:
        if not vm_id:
            return
        key = (str(vm_id), f"{port}:{model}")
        d = _AGGREGATOR_ACCUM.setdefault(key, {
            "requests": 0, "ok": 0, "degraded": 0, "err": 0,
            "error_types": {}, "latency_sum_ms": 0, "latency_count": 0,
        })
        d["requests"] += 1
        if outcome == "ok":
            d["ok"] += 1
        elif outcome == "degraded":
            d["degraded"] += 1
        else:
            d["err"] += 1
            if error_type:
                d["error_types"][str(error_type)] = d["error_types"].get(str(error_type), 0) + 1
    except Exception:
        pass


def _flush_usage_accum(ustore=None) -> int:
    """把 _TODAY_ACCUM 的增量 UPSERT 进 SQLite，返回成功落盘的行数。

    先整体摘走累加器再写库（swap-then-write）：写库期间到达的新请求会累进新的空 dict，
    不会被后续 clear() 抹掉。任一行写失败只 warning 跳过——用量是旁路观测，
    丢几条计数远好于把异常抛回调用方（flush loop / lifespan 关闭段）。
    """
    if ustore is None:
        from gateways import usage_store as ustore  # 延迟导入（AGENTS.md §7 跨模块约定）
    targets = _TODAY_ACCUM["targets"]
    models = _TODAY_ACCUM["models"]
    if not targets and not models:
        return 0
    # swap：拿走旧桶，换上空桶（此后的自增都进新桶）
    _TODAY_ACCUM["targets"] = {}
    _TODAY_ACCUM["models"] = {}

    from datetime import date as _date
    today = _date.today().isoformat()
    written = 0
    for label, d in targets.items():
        delta = {"requests": d.get("totalRequests", 0), "translated429": d.get("translated429", 0)}
        if not any(delta.values()):
            continue
        if ustore.upsert_day(today, label, _USAGE_TARGET_PSEUDO_MODEL, delta):
            written += 1
    for key, d in models.items():
        try:
            label, model = key
        except Exception:
            continue
        if not any(d.values()):
            continue
        if ustore.upsert_day(today, label, model, dict(d)):
            written += 1
    return written


def _flush_anthropic_accum() -> int:
    """把 _ANTHROPIC_ACCUM 增量 UPSERT 进 anthropic_daily；swap-then-write。"""
    global _ANTHROPIC_ACCUM
    if not any(_ANTHROPIC_ACCUM.values()):
        return 0
    from gateways import usage_store as _ustore
    accum = dict(_ANTHROPIC_ACCUM)
    _ANTHROPIC_ACCUM = {"totalRequests": 0, "passthroughOk": 0, "passthroughError": 0}
    from datetime import date as _date
    if _ustore.upsert_anthropic_day(_date.today().isoformat(), accum):
        return 1
    return 0


def _flush_aggregator_accum() -> int:
    """把 _AGGREGATOR_ACCUM 增量 UPSERT 进 aggregator_daily；swap-then-write。"""
    if not _AGGREGATOR_ACCUM:
        return 0
    from gateways import usage_store as _ustore
    from datetime import date as _date
    accum = dict(_AGGREGATOR_ACCUM)
    _AGGREGATOR_ACCUM.clear()
    today = _date.today().isoformat()
    written = 0
    for (vm_id, member), delta in accum.items():
        if _ustore.upsert_aggregator_day(today, vm_id, member, delta):
            written += 1
    return written


def _nous_sync_once() -> None:
    """启动时同步一次 nous 凭据（线程内执行，避免阻塞事件循环）。"""
    try:
        from gateways.nous import sync_nous_token
        sync_nous_token()
    except Exception as e:
        logger.warning(f"nous: 启动同步失败: {e}")


async def _usage_flush_loop():
    """每 60s 把 _TODAY_ACCUM 增量 UPSERT 进 SQLite；异常仅 warning 不中断循环。"""
    from gateways import usage_store as _ustore
    while True:
        await asyncio.sleep(60)
        try:
            _flush_usage_accum(_ustore)
        except Exception as e:
            logger.warning(f"usage flush loop error: {e}")
        try:
            _flush_anthropic_accum()
        except Exception as e:
            logger.warning(f"anthropic usage flush loop error: {e}")
        try:
            _flush_aggregator_accum()
        except Exception as e:
            logger.warning(f"aggregator usage flush loop error: {e}")


def _first_enabled_target_with_handler(handler: str) -> Optional[dict]:
    """返回第一个 enabled 且 handler 匹配的 target，无则 None。每次调用实时扫 _TARGETS。"""
    for t in _TARGETS:
        if t.get("handler") == handler and t.get("enabled", True):
            return t
    return None


def _copilot_ghe_host() -> str:
    """Copilot GHE host：从第一个 enabled 的 copilot handler target 的 targetHost 推导。"""
    t = _first_enabled_target_with_handler("copilot")
    return (t or {}).get("targetHost", "")


def _copilot_integration_id() -> str:
    """Copilot Integration-Id：从 copilot target 的 extraHeaders.Copilot-Integration-Id 推导。"""
    t = _first_enabled_target_with_handler("copilot")
    return ((t or {}).get("extraHeaders") or {}).get("Copilot-Integration-Id", "")


def _copilot_api_base() -> str:
    """api_base（不含路径，LiteLLM 会追加 /chat/completions）。"""
    host = _copilot_ghe_host()
    return f"https://{host}" if host else ""


def _copilot_model_role(role: str) -> str:
    """Copilot 模型角色（big/medium/small）：从 copilot target 的 modelRoles 推导。"""
    t = _first_enabled_target_with_handler("copilot")
    return ((t or {}).get("modelRoles") or {}).get(role, "")


def _copilot_big_model() -> str:
    return _copilot_model_role("big")


def _copilot_medium_model() -> str:
    return _copilot_model_role("medium")


def _copilot_small_model() -> str:
    return _copilot_model_role("small")


def _qclaw_base_url() -> str:
    """QClaw 上游 base URL：从 qclaw target 的 targetHost + routePrefix 推导。

    qclaw target 的 handler 是 passthrough（非 "qclaw"），故按 label 匹配。
    """
    for t in _TARGETS:
        if t.get("label") == "qclaw" and t.get("enabled", True):
            host = t.get("targetHost", "")
            prefix = t.get("routePrefix", "")
            return f"https://{host}{prefix}" if host else ""
    return ""

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
    # 同步累加进持久化累加器（进程重启会清零的 _MODEL_STATS 之外的落盘副本）
    # 8081（label="anthropic"）跳过：它是翻译入口，有独立 anthropic_daily 存储，
    # 混入 usage_daily 会与下游端口转发重复计数（设计：独立存储不混写）。
    if label != "anthropic":
        try:
            a = _TODAY_ACCUM["models"].setdefault(
                (label, model), {"requests": 0, "ok": 0, "err": 0, "translated429": 0}
            )
            a["requests"] += 1
            if outcome in a:
                a[outcome] += 1
        except Exception:
            pass


def _classify_http_error(status_code: int) -> str:
    """状态码 → 错误分类标签（对齐聚合网关 error_types 命名风格）。"""
    if status_code == 401:
        return "401_auth"
    if status_code == 402:
        return "402_billing"
    if status_code == 403:
        return "403_forbidden"
    if status_code == 429:
        return "429_rate_limit"
    if 400 <= status_code < 500:
        return f"{status_code}_client"
    if 500 <= status_code < 600:
        return f"{status_code}_server"
    return f"http_{status_code}"


def _bump_model_error(label: str, model, error_type: str) -> None:
    """记录模型级错误分类。旁路观测，异常绝不回抛。

    与 _bump_model_stats 并列调用（不改后者签名）：后者管 requests/ok/err/translated429
    计数，本函数只往 error_types 直方图里加一笔。model 可能为 None（错误发生在解析
    请求体之前）→ 统一归到 "_unknown"，调用点无需判空。
    """
    try:
        if not error_type:
            return
        mid = model or "_unknown"
        ms = _MODEL_STATS.setdefault(label, {}).setdefault(
            mid, {"requests": 0, "ok": 0, "err": 0, "translated429": 0, "error_types": {}}
        )
        ms.setdefault("error_types", {})
        ms["error_types"][error_type] = ms["error_types"].get(error_type, 0) + 1
        # 持久化累加器（label="anthropic" 跳过——8081 有独立 anthropic_daily，不混写）
        if label != "anthropic":
            a = _TODAY_ACCUM["models"].setdefault(
                (label, mid), {"requests": 0, "ok": 0, "err": 0, "translated429": 0, "error_types": {}}
            )
            a.setdefault("error_types", {})
            a["error_types"][error_type] = a["error_types"].get(error_type, 0) + 1
    except Exception:
        pass


# ─── HTTP 代理共享工具函数（所有端口统一用，不要各写各的） ───

# 响应头透传时剔除的字段：
# - transfer-encoding/connection/content-length：由代理按实际 body 重算
# - content-encoding：httpx 已自动解压 body（gzip/br/deflate），再透传该头会让
#   客户端对"已解压的明文"再解压一次 → 报 "incorrect header check"（openrouter 实测）
_PROXY_STRIP_RESP_HEADERS = frozenset(("transfer-encoding", "connection", "content-length", "content-encoding"))

# HTTP 转发引擎核心工具（已下沉至 server_http.py，行为零变化）。
# 此处 re-export，保证 server.py 内部及 gateways/* 的
# `from server import _write_response` 等延迟导入继续有效（零改动网关层）。
from server_http import (
    _parse_http_request,
    _write_error_response,
    _SseLineBuffer,
    _write_response,
    _HTTP_STATUS_REASON,
    _get_status_reason,
    _write_response_with_status_override,
)


# ─── egress SSRF 防护（Task 3）───
#
# 威胁模型：targets.json 不是攻击者可写的输入（由运维配置，见 AGENTS.md），
# 本守卫是运维误配置场景下的纵深防御——一次笔误/复制粘贴错误让 targetHost
# 指向云元数据服务或内网地址时，T2 之后的凭据注入会让代理带着真实凭据把
# 请求打进内网。不是"攻击者直接控制 targetHost"的场景。
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "169.254.169.254/32",  # 云元数据服务（AWS/GCP/Azure IMDS）
        "10.0.0.0/8",
        "192.168.0.0/16",
        "172.16.0.0/12",
        "fe80::/10",  # IPv6 link-local（对应 IPv4 169.254.0.0/16 段）
        "fc00::/7",  # IPv6 unique local address（ULA，对应 IPv4 私有段）
    )
)
# 注意：故意不封锁 127.0.0.0/8 / ::1 —— 本代理的既有架构里多个 target
# 合法地把 targetHost 配成 127.0.0.1（转发到同机其它端口的本地服务，如
# crack 网关/聚合网关内部路由），封锁回环会误伤这条已验证的正常路径。


def _is_internal_host(host: str) -> bool:
    """判定 host 是否落在内网/链路本地/云元数据地址范围内，或是 *.internal 域名。

    仅按字面量判定（IP 字面量 + 域名后缀），不做 DNS 解析——targetHost 来自
    运维静态配置，不需要防"域名解析后才是内网 IP"的 DNS rebinding 场景。
    """
    host = host.strip().lower()
    if host.endswith(".internal") or host == "internal":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # IPv4-mapped IPv6 字面量（::ffff:a.b.c.d）需展开为其内嵌 IPv4 地址再比对，
    # 否则 IPv6Address 与 tuple 中的 IPv4Network 做 `in` 判断恒为 False（跨地址族
    # 静默不匹配，不抛异常），会让 ::ffff:169.254.169.254 之类字面量绕过内网判定。
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _INTERNAL_NETWORKS)


def _resolve_auth(headers: dict, target: Optional[dict] = None, provider: Optional[str] = None) -> dict:
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
        # api_key 非空蕴含 target 非空，(target or {}) 仅为类型收窄，取值等价
        logger.debug(f"key: injected ({(target or {}).get('label', 'unknown')})")
    elif headers.get("authorization"):
        fwd["authorization"] = headers["authorization"]
        logger.debug("key: passed through from client request")

    if target and target.get("targetHost"):
        fwd["host"] = target["targetHost"]

    return fwd


def _handler_prepare_body(target: dict, body_bytes: bytes):
    """按 handler 处理请求体：模型别名解析（仅 anthropic）+ qclaw body 清理。
    返回 (new_body_bytes, body_json_or_None, cross_port_target_or_None)。
    cross_port_target 非 None 时表示该请求应整体路由到另一端口（调用方处理）。
    """
    handler = target.get("handler", "passthrough")
    try:
        body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        return body_bytes, None, None
    # ── 模型别名解析：仅 8081 Anthropic 翻译入口（handler=="anthropic"）──
    # 方案 A（2026-08-16）：全局别名表（_MODELS_CFG = 8081 anthropic target 的
    # models[] 路由表）只服务 8081 的 /v1/messages 翻译链路；其他端口各服务自己
    # 的 models[] 白名单，模型名原样透传给本端口上游。此前对所有端口生效，会把
    # 8082 上 claude-sonnet-5 / claude-haiku-4.5 劫持跨端口路由到 8080 聚合网关
    # （deepseek-v4-flash:agg / hy3:agg），永远到不了 8082 自己的 GHE 上游——
    # 而 GHE 原生支持 /v1/messages 与 /chat/completions（models 能力声明 + 实测 200）。
    req_model = body_json.get("model")
    mapped = None
    if handler == "anthropic" and req_model and isinstance(req_model, str):
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
    - free/paid（passthrough）类：
      - 有 secretRef 的 target（如 opencode-zen）：用 secrets token 覆盖客户端传入
      - 无 secretRef 的 target：客户端传入的 Authorization 优先，未传时才用 secrets 兜底
    """
    handler = target.get("handler", "passthrough")
    category = target.get("category", "free")
    # 认证
    if category == "crack" or target.get("secretRef"):
        # crack 类或有 secretRef 的 free/paid 类：注入 secrets token 覆盖客户端传入
        token = _cfg.resolve_secret(target, _SECRETS)
        if token:
            fwd_headers["authorization"] = f"Bearer {token}"
        # crack 类凭据唯一事实源是 secrets.json 注入的 authorization；
        # 客户端透传的 x-api-key 必须删除——否则上游（如 codebuddy copilot.tencent.com）
        # 优先用 x-api-key 校验（dummy 值 → 401 invalid_format），无视已注入的 authorization。
        if category == "crack":
            fwd_headers.pop("x-api-key", None)
    elif "authorization" not in fwd_headers:
        # free/paid 无 secretRef：客户端未带 token → 用自己维护的 secrets.json / apikeyEnv 兜底
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


def _rewrite_upstream_path(handler: str, raw_path: str, route_prefix: str, strip_v1: bool = False) -> str:
    """按 handler 精准映射上游路径；无映射时退回通用 routePrefix / stripV1 重写。

    优先级：handler 映射表 > routePrefix 重写 > stripV1 剥离 > 原样。
    stripV1：上游为 OpenAI 兼容但无 /v1 前缀（如 DeepSeek api.deepseek.com 直接挂
    /chat/completions）时，把客户端 /v1/xxx 剥成 /xxx。
    """
    handler_map = _HANDLER_PATH_MAP.get(handler or "")
    if handler_map and raw_path in handler_map:
        return handler_map[raw_path]
    if route_prefix and raw_path.startswith("/v1"):
        return route_prefix + raw_path[3:]
    if strip_v1 and raw_path.startswith("/v1"):
        return raw_path[3:]
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
    # 8081 的 models/modelDefaults 现在嵌套在 handler=="anthropic" 的 target 内
    # （config_store.load_targets 已把旧顶层格式内存迁移进该 target）。
    # _get_anthropic_target 接受 targets list 或 cfg dict，这里传 _TARGETS。
    _anthropic_t = _cfg._get_anthropic_target(_TARGETS)
    if _anthropic_t is not None:
        _MODELS_CFG["models"] = _anthropic_t.get("models", [])
        _MODELS_CFG["modelDefaults"] = _anthropic_t.get("modelDefaults", {"defaultPort": 8082})
    else:
        _MODELS_CFG["models"] = []
        _MODELS_CFG["modelDefaults"] = {"defaultPort": 8082}
    _load_token_saver_cfg(_anthropic_t)
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
    # 8081 的 models/modelDefaults 嵌套在 handler=="anthropic" 的 target 内
    # （与 _load_vendor_targets 同源，热重载时同步更新 _MODELS_CFG）。
    _anthropic_t = _cfg._get_anthropic_target(_TARGETS)
    if _anthropic_t is not None:
        _MODELS_CFG["models"] = _anthropic_t.get("models", [])
        _MODELS_CFG["modelDefaults"] = _anthropic_t.get("modelDefaults", {"defaultPort": 8082})
    else:
        _MODELS_CFG["models"] = []
        _MODELS_CFG["modelDefaults"] = {"defaultPort": 8082}
    _load_token_saver_cfg(_anthropic_t)
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

    # diff 端口（排除 handler=="anthropic"：8081 由 uvicorn FastAPI 承载，不走 TCP 透传引擎）
    wanted = {t["listenPort"]: t for t in _TARGETS if t.get("enabled", True) and t.get("handler") != "anthropic"}
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

_ANTHROPIC_PORT = int(_SERVER_CFG["listenPort"])

# dashboard / 管理 API 服务端口（8079，与翻译端点 8081 分离）。
# target 端口的 /dashboard、/api/* 反向代理转发的目标端口。
_DASHBOARD_PORT = int(_SERVER_CFG.get("dashboardPort", 8079))


# 值混合 int 计数与 str 时间戳，故不加值类型约束（与 _TARGET_STATS 同风格）
_ANTHROPIC_STATS: dict = {"totalRequests": 0, "passthroughOk": 0, "passthroughError": 0, "startedAt": datetime.now().isoformat()}


async def _handle_target_request(reader, writer, target):  # pyright: ignore[reportGeneralTypeIssues] - 统一透传引擎分支路径多，pyright 放弃分析；拆分会改变转发/统计行为，故仅抑制该诊断
    """统一透传引擎：处理单个 target 端口的全部请求。
    与原 _handle_vendor_request 兼容，新增 handler 分发 / 鉴权注入 / 401 缺 token。
    """
    label = target["label"]
    stats = _TARGET_STATS.setdefault(label, {
        "totalRequests": 0, "translated429": 0,
        "passthroughOk": 0, "passthroughError": 0,
        "startedAt": datetime.now().isoformat(),
    })
    # 在 try 之前绑定：下方各 except 分支的 _bump_model_error 都会读它，而请求体解析
    # （真正赋值处）在 try 内部靠后位置——若异常发生在赋值之前，except 里读未绑定局部变量
    # 会抛 UnboundLocalError，把错误处理路径本身打断（连 502/503 都写不出去）。
    _req_model = None
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
                resp = await c.get(f"http://127.0.0.1:{_DASHBOARD_PORT}/dashboard")
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
                fwd["host"] = f"127.0.0.1:{_DASHBOARD_PORT}"
                req = c.build_request(method, f"http://127.0.0.1:{_DASHBOARD_PORT}{raw_path}", headers=fwd, content=body if body else None)
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
        # 8080 聚合网关的端口级统计走独立的 _AGGREGATOR_ACCUM → aggregator_daily，
        # 不进 usage_daily（否则与它转发到的下游真实端口重复计数）。
        if target.get("handler") != "aggregator":
            _bump_usage_target(label, "totalRequests")

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
                    "message": f"请到 dashboard (http://127.0.0.1:{_DASHBOARD_PORT}/dashboard) 填写 {target.get('secretRef', label)} token",
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
                bool(target.get("stripV1", False)),
            )
            if _use_responses:
                upstream_path = "/responses"
            # egress SSRF 防护（Task 3）：转发前拒绝内网/元数据 targetHost，fail-closed。
            if _is_internal_host(target["targetHost"]):
                logger.warning(f"[{label}] blocked egress to internal host: {target['targetHost']}")
                await _write_error_response(
                    writer, 502, f"targetHost {target['targetHost']!r} is blocked (internal/link-local range)"
                )
                return
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
                if target.get("handler") != "aggregator":
                    _bump_usage_target(label, "translated429")
                if _req_model:
                    _bump_model_stats(label, _req_model, "translated429")
                _bump_model_error(label, _req_model, "429_rate_limit")
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
                if resp.status_code >= 400:
                    _bump_model_error(label, _req_model, _classify_http_error(resp.status_code))
                await _write_response(writer, resp, stats=stats, write_state=write_state)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as exc:
        stats["passthroughError"] += 1
        _bump_model_error(label, _req_model, "connect_fail")
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
        _bump_model_error(label, _req_model, "read_timeout")
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
        _bump_model_error(label, _req_model, "protocol_error")
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
        _bump_model_error(label, _req_model, "internal_error")
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
    def validate_model_field(cls, v, info):
        values = info.data
        if isinstance(values, dict):
            values["original_model"] = v
        return v


class TokenCountRequest(BaseModel):
    model: str
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    tools: Optional[List[Tool]] = None
    thinking: Optional[ThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
    original_model: Optional[str] = None  # Will store the original model name

    @field_validator("model")
    def validate_model_token_count(cls, v, info):
        values = info.data
        if isinstance(values, dict):
            values["original_model"] = v
        return v


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
    req_model = "unknown"
    if path == "/v1/messages" and method == "POST":
        try:
            body = await request.body()
            req_model = json.loads(body.decode("utf-8")).get("model", "unknown") if body else "unknown"
        except Exception:
            pass
        _ANTHROPIC_STATS["totalRequests"] += 1
        _bump_anthropic_usage("totalRequests")

    response = await call_next(request)

    if path == "/v1/messages" and method == "POST":
        outcome = "ok" if response.status_code < 400 else "err"
        _ANTHROPIC_STATS["passthroughOk" if outcome == "ok" else "passthroughError"] += 1
        _bump_anthropic_usage("passthroughOk" if outcome == "ok" else "passthroughError")
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

        # Dump 上游原始请求关键字段，方便排查（INFO 级别：8081 入站可观测，勿降级 DEBUG）
        upstream_thinking = body_json.get("thinking", {})
        upstream_max_tokens = body_json.get("max_tokens", "N/A")
        logger.info(
            f"📊 UPSTREAM REQUEST: model={original_model} stream={body_json.get('stream')} max_tokens={upstream_max_tokens} thinking={upstream_thinking}"
        )

        # ── 统一模型定义解析：命中 models[] → Anthropic→OpenAI 翻译后转发到本地端口 ──
        # 未命中则不再有兜底路径（legacy 单端口模式已下线），落到函数尾直接返回 404。
        mapped = _cfg._resolve_model_alias(_MODELS_CFG, original_model)
        if mapped:
            _fwd_port = int(mapped["port"])
            _downstream_protocol = mapped.get("protocol", "openai")
            _is_stream = bool(body_json.get("stream", False))

            if _downstream_protocol == "messages":
                # 下游已是 Anthropic /v1/messages 协议，跳过翻译，原样透传。
                # 注意：下游目标端口必须确实是 Anthropic /v1/messages 兼容端点，
                # 否则请求会被原样发到 /v1/messages 得到 404（协议不匹配需自查配置）。
                logger.info(f"🔀 [8081] route (passthrough messages): {original_model} → 127.0.0.1:{_fwd_port}/v1/messages model={mapped['model']}")
                # 过滤请求体：只保留 Anthropic /v1/messages 标准字段，丢弃额外字段（如 context_management）
                # 白名单集中维护于 gateways/messages_contract（含 thinking），避免内联散落 + 漏放思考链。
                # 能力门控：取目标 target 的 messagesProfile（按下游端口查），显式声明不支持的语义字段
                # （thinking/top_k/tool_choice）在转发前剥离；profile 缺失则完全沿用既往行为（零回归）。
                _profile_target = next(
                    (t for t in _TARGETS if t.get("listenPort") == _fwd_port), None
                )
                _messages_profile = (
                    _profile_target.get("messagesProfile") if _profile_target else None
                )
                _passthrough_body = filter_messages_request(body_json, profile=_messages_profile)
                _passthrough_body["model"] = mapped["model"]
                _passthrough_payload = json.dumps(_passthrough_body).encode("utf-8")
                # #2 鉴权注入：复用与 OpenAI 分支一致的 _resolve_auth —— target 配置了
                # apikey/apikeyEnv 时注入目标凭据（覆盖客户端传入），使 passthrough messages
                # 分支也能对接真实外部 Anthropic 兼容端点，而非仅限本地回环 + dummy key。
                _fwd_headers = {
                    "content-type": "application/json",
                    "host": f"127.0.0.1:{_fwd_port}",
                }
                _fwd_headers.update(_resolve_auth(raw_request.headers, target=mapped))
                _fwd_headers["host"] = f"127.0.0.1:{_fwd_port}"  # 保持指向下游本地端口
                # 失败闭合 + 大小写归一：target 解析出凭据时，剔除客户端携带的安全字段
                # （authorization 任意大小写 / x-api-key / cookie），仅保留 _resolve_auth 注入的
                # 目标 authorization，防止伪造凭据泄露到上游；无凭据则原样透传，保持旧行为。
                _resolved_cred = bool(mapped) and bool(
                    mapped.get("apikey")
                    or (mapped.get("apikeyEnv") and os.environ.get(mapped["apikeyEnv"], ""))
                )
                if _resolved_cred:
                    for _sec in ("authorization", "x-api-key", "cookie"):
                        for _k in [k for k in _fwd_headers if k.lower() == _sec and k != "authorization"]:
                            del _fwd_headers[_k]
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                    _req = client.build_request("POST", f"http://127.0.0.1:{_fwd_port}/v1/messages", headers=_fwd_headers, content=_passthrough_payload)
                    _resp = await client.send(_req, stream=_is_stream)
                    _anth_t = _cfg._get_anthropic_target(_MODELS_CFG)
                    _label = _anth_t.get("label") if _anth_t else "8081"
                    if _is_stream:
                        # #4 流式错误兜底：若上游非 2xx，转成 Anthropic 错误 JSON 而非透传原始错误体，避免客户端解析失败。
                        if _resp.status_code >= 400:
                            _err_body = b""
                            try:
                                async for _c in _resp.aiter_bytes():
                                    _err_body += _c
                            finally:
                                await _resp.aclose()
                            _err_text = _err_body.decode("utf-8", "replace")
                            # #7 复用 _map_upstream_error：与非流式路径同等质量的错误标准化
                            # （429/限流/认证/权限/过载等 → 标准 Anthropic error.type + 目标状态码）。
                            _mapped_err = _map_upstream_error(_err_text)
                            if _mapped_err is not None:
                                _tgt_status, _err_type = _mapped_err
                                _bump_model_stats(_label, mapped["model"], "translated429")
                                return JSONResponse(
                                    content={
                                        "type": "error",
                                        "error": {
                                            "type": _err_type,
                                            "message": "Upstream temporarily over capacity.",
                                            "original_status": _resp.status_code,
                                        },
                                    },
                                    status_code=_tgt_status,
                                    headers={"Retry-After": str(_VENDOR_RETRY_AFTER)},
                                )
                            # 未命中映射表：保留原状态码，尽量透传上游原始错误信封
                            try:
                                _upstream_err = json.loads(_err_text)
                                _err_type = _upstream_err.get("type", "api_error")
                                _err_msg = _upstream_err.get("error", {}).get("message", str(_upstream_err))
                            except Exception:
                                _err_type = "api_error"
                                _err_msg = f"upstream messages error (status {_resp.status_code})"
                            _bump_model_stats(_label, mapped["model"], "err")
                            return JSONResponse(
                                content={"type": "error", "error": {"type": _err_type, "message": _err_msg}},
                                status_code=_resp.status_code,
                            )
                        # #1 流式正常路径：先在 async with 内读完所有字节再 yield（避免 client 关闭后迭代报 ReadError）
                        _raw_chunks: list[bytes] = []
                        try:
                            async for chunk in _resp.aiter_bytes():
                                _raw_chunks.append(chunk)
                        finally:
                            await _resp.aclose()
                        # #7 检测"上游 200 但 SSE 内嵌 event: error 帧"的伪装成功响应。
                        # 逐帧扫描（SSE 帧以空行分隔），只对形如 `event: error` + 合法
                        # error 信封的帧判定为错误；任何解析失败/结构不符一律 continue，
                        # 绝不中断流（fail-open 规则，镜像 anthropic_stream_convert.py:185-186）。
                        _full_bytes = b"".join(_raw_chunks)
                        # #8 config-gated 守护（默认关闭，见 ANTHROPIC_SSE_ERROR_GUARD_ENABLED）：
                        # 用 _guard_anthropic_sse_stream（非 normalizeSse——那是 OpenAI 帧改写，
                        # 结构上不适用于 Anthropic 帧）在 #7 既有检测之前额外扫描一遍，兼顾
                        # 畸形终止帧（无标准 error 信封但命中 T6 广化后的 vendor 错误特征表）。
                        # 关闭时（默认）不执行，行为与开发本开关前完全一致。
                        if ANTHROPIC_SSE_ERROR_GUARD_ENABLED:
                            _guard_result = _guard_anthropic_sse_stream(_full_bytes)
                            if _guard_result is not None:
                                _g_status, _g_type, _g_msg = _guard_result
                                _bump_model_stats(_label, mapped["model"], "err")
                                return JSONResponse(
                                    content={"type": "error", "error": {"type": _g_type, "message": _g_msg}},
                                    status_code=_g_status,
                                )
                        _embedded_err: Optional[Tuple[int, str, str]] = None
                        try:
                            _full_text = _full_bytes.decode("utf-8")
                        except Exception:
                            _full_text = None
                        if _full_text is not None:
                            for _frame in _full_text.split("\n\n"):
                                if "event: error" not in _frame and "event:error" not in _frame:
                                    continue
                                _data_line = None
                                for _line in _frame.splitlines():
                                    if _line.startswith("data:"):
                                        _data_line = _line[len("data:"):].strip()
                                        break
                                if not _data_line:
                                    continue
                                try:
                                    _err_obj = json.loads(_data_line)
                                except json.JSONDecodeError:
                                    # 解析失败绝不中断流：不是可识别的 error 信封，继续扫描/透传
                                    continue
                                if not isinstance(_err_obj, dict):
                                    continue
                                _err_detail = _err_obj.get("error")
                                if not isinstance(_err_detail, dict):
                                    continue
                                _e_type = _err_detail.get("type", "api_error")
                                _e_msg = _err_detail.get("message", "embedded SSE error")
                                _e_status = _ANTHROPIC_ERR_TYPE_TO_STATUS.get(_e_type, 500)
                                _embedded_err = (_e_status, _e_type, _e_msg)
                                break
                        if _embedded_err is not None:
                            _e_status, _e_type, _e_msg = _embedded_err
                            _bump_model_stats(_label, mapped["model"], "err")
                            return JSONResponse(
                                content={"type": "error", "error": {"type": _e_type, "message": _e_msg}},
                                status_code=_e_status,
                            )
                        async def _passthrough_stream():
                            for c in _raw_chunks:
                                yield c
                        _bump_model_stats(_label, mapped["model"], "ok")
                        return StreamingResponse(_passthrough_stream(), media_type="text/event-stream", status_code=_resp.status_code)
                    # 非流式
                    _body_bytes = await _resp.aread()
                    if _resp.status_code >= 400:
                        # #1 非流式错误标准化：复用 openai 分支的 _map_upstream_error 翻译 429 等
                        mapped_err = _map_upstream_error(_body_bytes.decode("utf-8", "replace"))
                        if mapped_err is not None:
                            _tgt_status, _err_type = mapped_err
                            _bump_model_stats(_label, mapped["model"], "translated429")
                            return JSONResponse(
                                content={
                                    "error": {
                                        "type": _err_type,
                                        "message": "Upstream temporarily over capacity.",
                                        "original_status": _resp.status_code,
                                    }
                                },
                                status_code=_tgt_status,
                                headers={"Retry-After": str(_VENDOR_RETRY_AFTER)},
                            )
                        # 非 429 等：透传上游错误（保持 Anthropic 格式）
                        _bump_model_stats(_label, mapped["model"], "err")
                        try:
                            _downstream_resp = json.loads(_body_bytes.decode("utf-8"))
                        except Exception:
                            return JSONResponse(content={"error": {"type": "proxy_error", "message": "upstream invalid response"}}, status_code=502)
                        return JSONResponse(content=_downstream_resp, status_code=_resp.status_code)
                    # 成功 2xx
                    _bump_model_stats(_label, mapped["model"], "ok")
                    try:
                        _downstream_resp = json.loads(_body_bytes.decode("utf-8"))
                    except Exception:
                        return JSONResponse(content={"error": {"type": "proxy_error", "message": "upstream invalid response"}}, status_code=502)
                    return JSONResponse(content=_downstream_resp, status_code=_resp.status_code)

            from anthropic_convert import convert_anthropic_request_to_openai, convert_openai_response_to_anthropic
            logger.info(f"🔀 [8081] route: {original_model} → 127.0.0.1:{_fwd_port}/v1/chat/completions model={mapped['model']}")
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
            if raw_request.headers.get("x-session-id"):
                _fwd_headers["x-session-id"] = raw_request.headers["x-session-id"]
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
                _req = client.build_request("POST", f"http://127.0.0.1:{_fwd_port}/v1/chat/completions", headers=_fwd_headers, content=openai_payload)
                _resp = await client.send(_req, stream=_is_stream)
                if _is_stream:
                    # 注意：必须在此 async with 块内完成全部字节读取——StreamingResponse
                    # 返回后 async with 立即退出（client 关闭），生成器若在外部迭代
                    # _resp 会 ReadError。故此处先收集全部 data 行，再转 Anthropic SSE。
                    from anthropic_stream_convert import convert_openai_sse_to_anthropic
                    line_buf = _SseLineBuffer()
                    data_lines: list = []
                    try:
                        async for chunk in _resp.aiter_bytes():
                            for line in line_buf.feed(chunk):
                                # 只取 data: 载荷行（event: / 空行丢弃），喂转换器
                                if line.startswith(b"data:"):
                                    data_lines.append(line[5:].strip().decode("utf-8", errors="replace"))
                    finally:
                        await _resp.aclose()
                    # 流结束吐残留（无末尾 \n 的最后一行，防御性透传）
                    tail = line_buf.flush()
                    if tail and tail.startswith(b"data:"):
                        data_lines.append(tail[5:].strip().decode("utf-8", errors="replace"))
                    # 转换 + 输出 Anthropic SSE（转换器内部处理 [DONE] 哨兵）
                    _anthropic_frames = list(convert_openai_sse_to_anthropic(data_lines, original_model))

                    async def _models_stream():
                        for frame in _anthropic_frames:
                            yield frame
                    return StreamingResponse(_models_stream(), media_type="text/event-stream")
                _body_bytes = await _resp.aread()
                try:
                    _openai_resp = json.loads(_body_bytes.decode("utf-8"))
                except Exception:
                    return JSONResponse(content={"error": {"type": "proxy_error", "message": "upstream invalid response"}}, status_code=502)
                if _resp.status_code >= 400:
                    # 与 _handle_target_request 统一：先经 _map_upstream_error 识别限流特征
                    # （翻译为标准 429 + rate_limit_error，让客户端按标准错误重试/降级），
                    # 未命中再原样透传上游错误体与状态码。
                    mapped_err = _map_upstream_error(_body_bytes.decode("utf-8", "replace"))
                    if mapped_err is not None:
                        _tgt_status, _err_type = mapped_err
                        logger.info(f"[/v1/messages] translated HTTP {_resp.status_code} → {_tgt_status} ({_err_type})")
                        return JSONResponse(
                            content={
                                "error": {
                                    "type": _err_type,
                                    "message": "Upstream temporarily over capacity.",
                                    "original_status": _resp.status_code,
                                }
                            },
                            status_code=_tgt_status,
                            headers={"Retry-After": str(_VENDOR_RETRY_AFTER)},
                        )
                    return JSONResponse(content=_openai_resp, status_code=_resp.status_code)
                _anthropic_resp = convert_openai_response_to_anthropic(_openai_resp, original_model)
                return JSONResponse(content=_anthropic_resp, status_code=_resp.status_code)

        # models[] 未命中：legacy 单端口模式已下线，无兜底路径，显式返回 404
        raise HTTPException(
            status_code=404,
            detail=f"模型 '{original_model}' 未在 models[] 中配置路由",
        )

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

        # Format error for response
        error_message = f"Error: {str(e)}"

        # Return detailed error
        status_code = getattr(e, "status_code", 500)
        raise HTTPException(status_code=status_code, detail=error_message)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: TokenCountRequest, raw_request: Request):
    try:
        original_model = request.original_model or request.model
        display_model = original_model.split("/")[-1] if "/" in original_model else original_model
        log_request_beautifully(
            "POST", raw_request.url.path, display_model, display_model,
            len(request.messages), len(request.tools) if request.tools else 0, 200,
        )
        token_count = _estimate_messages_tokens(
            request.messages, request.model, system=request.system, tools=request.tools,
        )
        return TokenCountResponse(input_tokens=token_count)
    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        logger.error(f"Error counting tokens: {str(e)}\n{error_traceback}")
        raise HTTPException(status_code=500, detail=f"Error counting tokens: {str(e)}")


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


# ─── 统一管理面板（dashboard 包）─────────────────────────────────────
# CSS/HTML 渲染与全部 /api/* 路由已拆分到 dashboard/routes.py，逻辑原样搬迁。
# 挂载点必须满足两个约束：
#   1. 在上面这些被 dashboard 复用的符号（_humanize_model_name /
#      _scan_dangling_refs / ModelRegistry / _fetch_live_models）定义之后
#      —— dashboard.routes 在模块级 import 它们；
#   2. 在 catch_all 之前 —— 否则 "/{path:path}" 会先匹配掉 /dashboard 与
#      /api/*，与拆分前的注册顺序不一致。
from dashboard.routes import dashboard_router  # noqa: E402

# 管理面路由挂到独立的 dashboard_app（8079），与主 app（8081）分离。
# 挂到 dashboard_app 后，/dashboard 与 /api/* 在 8079 可用，8081 不再提供这些端点。
dashboard_app.include_router(dashboard_router)


# Catch-all route to handle OAuth and other unexpected endpoints
# Note: FastAPI will NOT match "/" to this because root is defined above as exact match
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str):
    """Catch-all for any unhandled endpoints (OAuth, health checks, etc.)"""
    # Skip root path — handled by root() above
    if path == "" or path == "/":
        return {"message": "Anthropic Proxy for LiteLLM"}
    # 新版 Claude Code 启动时 HEAD /api/hello 做连通性检查——返回 200 声明代理健康，
    # 否则客户端判定 base_url 不可用，拒绝后续 /v1/messages 请求。
    if path == "api/hello" or path == "/api/hello":
        return JSONResponse(content={"ok": True}, status_code=200)
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
        print("Backend routing is driven by targets.json models[] (no legacy provider switch).")
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

    # ─── 双 uvicorn 并发启动 ───
    # 主 app（8081，Anthropic 翻译入口）+ dashboard_app（8079，管理面独立端口）。
    # --port 仅控制主 app；dashboard 端口由 targets.json server.dashboardPort 决定（默认 8079）。
    # dashboard_app 复用主 app 全局状态（_TARGETS/_MODELS_CFG/_SECRETS），无需独立 lifespan。
    # 一个 server 崩溃不 zombie 另一个：gather 内 try/except 记录日志后整体退出（systemd 会拉起）。
    dashboard_port = int(_SERVER_CFG.get("dashboardPort", 8079))

    main_cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    dash_cfg = uvicorn.Config(
        dashboard_app, host="0.0.0.0", port=dashboard_port, log_level="error"
    )
    main_server = uvicorn.Server(main_cfg)
    dash_server = uvicorn.Server(dash_cfg)

    async def _serve_both():
        try:
            await asyncio.gather(main_server.serve(), dash_server.serve())
        except Exception as e:  # noqa: BLE001 — 启动期兜底，任一 server 异常都需记录并退出
            logger.error(f"❌ uvicorn server 异常退出: {e}", exc_info=True)
            raise

    asyncio.run(_serve_both())


