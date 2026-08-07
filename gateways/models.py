"""模型注册表：统一模型列表接口、来源推导、悬空引用扫描、下游模型拉取。

从 server.py 拆分而来（Todo 7 模块拆分），逻辑逐字节不变。
依赖方向：models → server（框架层），不反向。

对 server 的两类引用方式（务必区分，改动时勿混用）：
  1. **热重载可变全局** —— _TARGETS / _SECRETS / _MODELS_CFG /
     _MODEL_REGISTRY 会在 _load_vendor_targets / _reload_targets / _refresh_secrets 里被
     `global` 重新赋值。一律写 `_srv.<NAME>` 属性访问，每次读到的都是当前对象；
     若写成模块级 `from server import X` 值拷贝，热重载后会读到旧配置。
  2. **稳定符号**（函数 / logger / PREFERRED_PROVIDER / COPILOT_* / OPENAI_* 等）——
     在各自使用函数体内 `from server import X` 延迟导入（模块级 import 会撞上
     server.py 顶部 re-export 的循环导入，且值拷贝在热重载后读到旧值）。
"""

import asyncio
import inspect
import os
import time
from typing import Dict, List, Optional

import httpx

# 跨模块共享状态（务必区分，改动时勿混用）：
#   1. **热重载可变全局** —— _TARGETS / _SECRETS / _MODELS_CFG / _MODEL_REGISTRY /
#      _cfg 会在 _load_vendor_targets / _reload_targets / _refresh_secrets 里被
#      `global` 重新赋值或随配置更新。一律写 `_srv.<NAME>` 属性访问，每次读到的
#      都是当前对象；若写成模块级 `from server import X` 值拷贝，热重载后会读到
#      旧快照。
#   2. **稳定符号**（函数 / logger / PREFERRED_PROVIDER / COPILOT_* / OPENAI_* 等）——
#      在各自使用函数体内 `from server import X` 延迟导入。
#   为什么一律延迟：models 被 server.py 顶部 re-export import（from gateways.models
#   import ...），若此处模块级 `from server import X`，会在 server 尚在初始化、X 尚未
#   定义时触发循环导入 ImportError（COPILOT_API_BASE 等定义在 server.py ~650 行，
#   晚于 models 的 import 点）。延迟到函数体后，调用时 server 已完成加载，且每次读到
#   当前值。
import server as _srv


# ═══════════════════════════════════════════════════════════════════════════════
# 常量定义（原 server.py 内部常量，仅被模型层使用）
# ═══════════════════════════════════════════════════════════════════════════════

_STATIC_SOURCE_BY_LABEL = {
    "codebuddy": "codebuddy",
    "qclaw": "qclaw",
    "trae-work": "trae-work",
}

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

# 下游模型列表缓存（避免每次 /v1/models 都请求上游）
_DOWNSTREAM_MODELS_CACHE: Optional[List[dict]] = None
_DOWNSTREAM_MODELS_CACHE_TIME: float = 0
_MODELS_CACHE_TTL: float = 300.0  # 5 分钟

# 人类可读名修正表
_WORD_FIXES = {
    # Model families / brands
    "glm": "GLM",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
    "kimi": "Kimi",
    "hunyuan": "Hunyuan",
    "qwen": "Qwen",
    "nemotron": "Nemotron",
    "llama": "Llama",
    "gpt": "GPT",
    "claude": "Claude",
    "mai": "Mai",
    "hy3": "Hy3",
    # Descriptors / suffixes
    "codex": "Codex",
    "pro": "Pro",
    "flash": "Flash",
    "mini": "Mini",
    "ultra": "Ultra",
    "super": "Super",
    "turbo": "Turbo",
    "coder": "Coder",
    "thinking": "Thinking",
    "instruct": "Instruct",
    "chat": "Chat",
    "modelroute": "Model Route",
    "default": "Default",
    "image": "Image",
    "art": "Art",
    "text": "Text",
    "embedding": "Embedding",
    "small": "Small",
    "large": "Large",
    "picker": "Picker",
    "compaction": "Compaction",
    "trajectory": "Trajectory",
    "maverick": "Maverick",
    "oss": "OSS",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "opus": "Opus",
    "night": "Night",
    "volc": "Volc",
    "highspeed": "HighSpeed",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

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
    result = _srv._fetch_live_models(target)
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


# ═══════════════════════════════════════════════════════════════════════════════
# 公共导出函数
# ═══════════════════════════════════════════════════════════════════════════════

def _get_target_models(label: str) -> List[dict]:
    """统一接口（同步）：返回某 target 的模型列表，收敛四种 modelsSource。

    返回 [{id, display_name, aliases, enabled, target, source}]，source 标记来源：
    anthropic（8081 顶层 models[]）/ aggregator（virtualModels）/
    copilot（上游实时拉取）/ codebuddy|qclaw|trae-work|<handler>（静态 models[]）。
    label 不存在时返回 []。

    注意：copilot target 会同步拉取上游（asyncio.run），故不可在运行中的事件
    循环里调用——async 路径请用 _get_target_models_async。
    """
    target = next((t for t in _srv._TARGETS if t.get("label") == label), None)
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
    target = next((t for t in _srv._TARGETS if t.get("label") == label), None)
    if target is None:
        return []
    source = _target_model_source(target)
    live_ids: List[str] = []
    if source == "copilot":
        live_ids = [str(m) for m in (await _srv._fetch_live_models(target) or [])]
    return _build_target_models(target, source, live_ids)


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
    for m in _srv._MODELS_CFG.get("models", []):
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


def _humanize_model_name(mid) -> str:
    """模型 id → 人类可读名。

    规则：去 'pool-' / 'provider/' 前缀、':free' 尾缀转 '(free)'，
    '-'/'_' 转空格，已知品牌词做专名修正，版本号保持原样。
    """
    s = str(mid)
    # Strip provider/ prefix (e.g., nvidia/nemotron → nemotron)
    if "/" in s:
        s = s.split("/")[-1]
    # Strip pool- prefix (e.g., pool-deepseek-v4-pro → deepseek-v4-pro)
    if s.startswith("pool-"):
        s = s[5:]
    # Handle :free suffix
    free_note = ""
    if s.endswith(":free"):
        s = s[:-5]
        free_note = " (free)"
    # Replace separators with space
    s = s.replace("-", " ").replace("_", " ")
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
    return " ".join(words) + free_note


async def _fetch_downstream_models() -> List[dict]:
    """从下游网关拉取模型列表，按下游 endpoint 区分 provider 拉取方式。

    - copilot / openai / qclaw：直连下游 /models（OpenAI 格式）
    - 返回统一格式的 model dict 列表
    """
    from server import (
        COPILOT_API_BASE,
        COPILOT_GHE_TOKEN,
        COPILOT_INTEGRATION_ID,
        OPENAI_API_KEY,
        OPENAI_BASE_URL,
        PREFERRED_PROVIDER,
        logger,
    )
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
    from server import (
        COPILOT_BIG_MODEL,
        COPILOT_MEDIUM_MODEL,
        COPILOT_SMALL_MODEL,
        PREFERRED_PROVIDER,
    )
    models: List[dict] = []

    # ── 翻译链路别名（仅 8081 Anthropic 端口需要）──
    # 动态来自 targets.json 顶层 models[]（name + aliases 均可被 _resolve_model_alias 命中），
    # 与 dashboard「模型定义」编辑视图同源；不再硬编码（曾含 claude-*-4-20250514 死名单）。
    if include_aliases:
        for _m in _srv._MODELS_CFG.get("models", []):
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


async def _fetch_live_models(target: dict):
    """从下游网关拉取真实模型列表（OpenAI 格式，data[].id）。

    编辑弹框用：与 copilot 一致，展示下游真实可用模型。
    返回模型 id 列表；拉取失败（无 key/超时/非 200）返回 None，调用方降级。
    gemini-native handler：走 Google 原生 /v1beta/models，解析 models[].name。
    """
    from server import _GEMINI_NATIVE_BASE, logger
    host = target.get("targetHost") or ""
    if not host:
        return None
    protocol = target.get("targetProtocol", "https")
    port = target.get("targetPort", 443)
    prefix = target.get("routePrefix", "")
    url = f"{protocol}://{host}:{port}{prefix}/models"
    headers = {}
    secret = _srv._cfg.resolve_secret(target, _srv._SECRETS)
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


def _derive_models_source(target: dict) -> str:
    """推导 target 的模型来源枚举值。

    优先级：handler 直映射 > Anthropic 入口（8081 / anthropic* label）> label 细分 > passthrough。
    """
    handler = target.get("handler") or ""  # 缺 handler 时用 "" 查表，与 None 同样查不到，结果等价
    direct = _MODELS_SOURCE_BY_HANDLER.get(handler)
    if direct:
        return direct
    label = target.get("label") or ""
    if target.get("listenPort") == _ANTHROPIC_ENTRY_PORT:
        return "anthropic"
    return _MODELS_SOURCE_BY_LABEL.get(label, "passthrough")


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
        "targets": _srv._TARGETS,
        "models": _srv._MODELS_CFG.get("models", []) or [],
    })


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