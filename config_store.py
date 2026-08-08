"""配置存储层：targets.json / secrets.json 加载、迁移、校验、原子写。

独立于 server.py，供透传引擎、dashboard API、破解工具共用。
优先级：secrets.json > 环境变量(apikeyEnv) > 客户端透传。
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("config_store")

TARGETS_PATH = Path(__file__).parent / "targets.json"
SECRETS_PATH = Path(__file__).parent / "secrets.json"

VALID_CATEGORIES = ("crack", "free", "paid", "aggregate")
VALID_HANDLERS = ("passthrough", "copilot", "qclaw", "gemini-native", "trae-work", "aggregator", "deepseek", "anthropic")

_REQUIRED_FIELDS = ("label", "listenPort", "category", "handler", "targetHost")

# 顶层 server 段默认配置（.env 并入 targets.json 的单一事实源）。
# 用户 server 段与此做一层深合并：顶层子键缺失补默认，嵌套 dict 的键缺失补默认。
# 8081 legacy 清理：preferredProvider/legacyModels/copilot/qclaw 已移除（旧键静默忽略），
# 只剩 listenPort/log/cache 三个运行时键。
DEFAULT_SERVER_CONFIG = {
    "listenPort": 8081,               # 原 ANTHROPIC_PORT env
    "dashboardPort": 8079,            # dashboard 独立端口（与 8081 入口分离，架构统一）
    "log": {                          # 原 DEBUG/LOG_FILE/LOG_RETENTION_DAYS/LOG_ROTATE_WHEN/LOG_ROTATE_INTERVAL
        "debug": False,
        "file": "",
        "retentionDays": 7,
        "rotateWhen": "midnight",
        "rotateInterval": 1,
    },
    "cache": {                        # 原 CACHE_ENABLED/CACHE_MAX_SIZE/CACHE_TTL_SECONDS/CACHE_MAX_ITEM_SIZE_KB
        "enabled": True,
        "maxSize": 500,
        "ttlSeconds": 3600,
        "maxItemSizeKb": 100,
    },
}

# anthropic-compatible 入口端口：与 server.py 的 _ANTHROPIC_PORT 默认值同源，
# 统一从 DEFAULT_SERVER_CONFIG 派生（单一事实源，勿再硬编码 8081）。
ANTHROPIC_PORT = DEFAULT_SERVER_CONFIG["listenPort"]


def _merge_server_config(user_server: dict) -> dict:
    """把用户 server 段深合并到 DEFAULT_SERVER_CONFIG 上（一层深合并，不递归）。

    顶层子键缺失用默认；嵌套 dict（log/cache）键缺失补默认。
    已删除的旧键（preferredProvider/legacyModels/copilot/qclaw）不在
    DEFAULT_SERVER_CONFIG 中，合并后被自然丢弃（静默忽略）。
    """
    merged = {}
    for key, default_val in DEFAULT_SERVER_CONFIG.items():
        if isinstance(default_val, dict):
            user_sub = user_server.get(key)
            sub = dict(default_val)
            if isinstance(user_sub, dict):
                sub.update(user_sub)
            merged[key] = sub
        else:
            merged[key] = user_server.get(key, default_val)
    return merged


def load_targets(path: Path = TARGETS_PATH) -> dict:
    """加载 targets.json，兼容旧数组格式，自动迁移为新对象格式。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"targets.json not found: {path}, using empty config")
        return {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": [],
                "server": _merge_server_config({})}
    except json.JSONDecodeError as e:
        logger.error(f"targets.json invalid JSON: {e}")
        return {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": [],
                "server": _merge_server_config({})}

    if isinstance(raw, list):
        # 旧格式：数组 → 迁移
        logger.warning("targets.json 是旧数组格式，自动迁移为新对象格式")
        migrated = []
        for t in raw:
            category = t.get("category", "free")
            migrated.append({
                **t,
                "category": category,
                "handler": t.get("handler", "passthrough"),
                "isFree": t.get("isFree", category == "free"),
                "enabled": t.get("enabled", True),
            })
        cfg = {"targets": migrated, "modelDefaults": {"defaultPort": 8082}, "models": [],
               "server": _merge_server_config({})}
        try:
            save_targets(cfg, path)  # 回写迁移结果
            logger.info(f"targets.json migrated to new format: {path}")
        except Exception as e:
            logger.warning(f"failed to write migrated targets.json: {e}")
        return cfg

    # 新格式
    targets = raw.get("targets", [])
    normalized = []
    for t in targets:
        category = t.get("category", "free")
        normalized.append({
            **t,
            "category": category,
            "handler": t.get("handler", "passthrough"),
            "isFree": t.get("isFree", category == "free"),
            "enabled": t.get("enabled", True),
        })
    user_server = raw.get("server")
    result = {
        "targets": normalized,
        "modelDefaults": raw.get("modelDefaults", {"defaultPort": 8082}),
        "models": raw.get("models", []),
        "server": _merge_server_config(user_server) if isinstance(user_server, dict)
                  else _merge_server_config({}),
    }
    # T3 架构统一：顶层 modelDefaults/models 迁移进 anthropic target（内存，不写盘）
    _migrate_top_level_models_to_anthropic(result)
    return result


def _migrate_top_level_models_to_anthropic(cfg: dict) -> None:
    """把顶层 modelDefaults/models 迁移进 targets[] 的 anthropic target（原地修改，内存不写盘）。

    迁移条件：顶层 modelDefaults 或 models 非空，且 targets[] 中尚无 handler=="anthropic" 的 target。
    幂等：已有 anthropic target 则跳过（不覆盖其嵌套字段，不删顶层键）。

    迁移后：创建 anthropic target（label="anthropic", listenPort=ANTHROPIC_PORT,
    handler="anthropic", category="free", models=顶层models, modelDefaults=顶层modelDefaults），
    并从 cfg 顶层移除 models 键（modelDefaults 保留顶层兼容旧读取路径，置空 dict）。
    下次 save_targets 时自然落盘。
    """
    top_models = cfg.get("models")
    top_md = cfg.get("modelDefaults")
    has_top_models = isinstance(top_models, list) and len(top_models) > 0
    has_top_md = isinstance(top_md, dict) and bool(top_md)
    if not (has_top_models or has_top_md):
        return  # 顶层无旧格式数据，无需迁移
    if _get_anthropic_target(cfg) is not None:
        return  # 已有 anthropic target，幂等跳过

    anth_target: dict = {
        "label": "anthropic",
        "listenPort": ANTHROPIC_PORT,
        "category": "free",
        "handler": "anthropic",
        "enabled": True,
    }
    if has_top_models:
        anth_target["models"] = top_models
    if has_top_md:
        anth_target["modelDefaults"] = top_md
    cfg.setdefault("targets", []).append(anth_target)
    # 移除顶层旧键（已迁入嵌套）；modelDefaults 保留空 dict 维持结构兼容
    if has_top_models:
        cfg["models"] = []
    if has_top_md:
        cfg["modelDefaults"] = {}
    logger.info("load_targets: 顶层 modelDefaults/models 已迁移进 anthropic target（内存，下次 save 落盘）")


def _err(errors: list, path: str, msg: str) -> None:
    """追加一条结构化错误：{"path": 点号寻址, "msg": 人类可读消息}。"""
    errors.append({"path": path, "msg": msg})


def validate_targets(cfg: dict) -> list:
    """校验配置，返回结构化错误列表 [{"path", "msg"}]（空 = 通过）。

    path 用点号 + 下标寻址（如 targets[2].label / models[1].target.port），
    下标一律取 enumerate 序号，缺 label 时也能精确定位。
    """
    errors = []
    targets = cfg.get("targets", [])
    labels = {}
    ports = {}

    for i, t in enumerate(targets):
        label = t.get("label", "")
        base = f"targets[{i}]"
        disabled = t.get("enabled") is False
        if not disabled:
            for field in _REQUIRED_FIELDS:
                # aggregator 与 anthropic 均无真实上游 host：
                # aggregator 仅由 virtualModels 池定义；anthropic 是 8081 翻译入口自身
                if field == "targetHost" and t.get("handler") in ("aggregator", "anthropic"):
                    continue
                if not t.get(field):
                    _err(errors, f"{base}.{field}", f"{base} 缺少必需字段: {field}")
            if t.get("category") not in VALID_CATEGORIES:
                _err(errors, f"{base}.category",
                     f"{base} category 非法: {t.get('category')}（合法: {VALID_CATEGORIES}）")
            if t.get("handler") not in VALID_HANDLERS:
                _err(errors, f"{base}.handler",
                     f"{base} handler 非法: {t.get('handler')}（合法: {VALID_HANDLERS}）")
            if t.get("category") == "crack" and not t.get("crackTool"):
                _err(errors, f"{base}.crackTool", f"crack {base} 缺少 crackTool")
            if t.get("handler") == "aggregator":
                _validate_aggregator_target(t, label, base, errors)
        if label in labels:
            _err(errors, f"{base}.label", f"重复 label: '{label}'")
        labels[label] = True
        port = t.get("listenPort")
        if port in ports:
            _err(errors, f"{base}.listenPort",
                 f"端口 {port} 被多个 target 占用 ({ports[port]}, {label})")
        ports[port] = label

    # modelDefaults.defaultPort 校验（顶层旧格式）
    model_defaults = cfg.get("modelDefaults", {})
    default_port = model_defaults.get("defaultPort")
    if default_port is not None:
        if not isinstance(default_port, int) or default_port < 0:
            _err(errors, "modelDefaults.defaultPort",
                 "modelDefaults.defaultPort must be a non-negative integer")

    # models 结构校验与全局唯一性（顶层旧格式）
    # 顶层与 anthropic target 的嵌套 models 共享同一个 seen set，
    # 避免迁移期间两处出现同名模型时漏报重复。
    global_seen: set = set()
    _validate_models(cfg.get("models", []), errors, seen=global_seen)

    # anthropic target 的嵌套 models / modelDefaults 校验（T3 架构统一）
    for i, t in enumerate(targets):
        if t.get("handler") != "anthropic":
            continue
        base = f"targets[{i}]"
        # 嵌套 modelDefaults.defaultPort
        nested_md = t.get("modelDefaults")
        if nested_md is not None:
            if not isinstance(nested_md, dict):
                _err(errors, f"{base}.modelDefaults", f"{base}.modelDefaults must be an object")
            else:
                ndp = nested_md.get("defaultPort")
                if ndp is not None and (isinstance(ndp, bool) or not isinstance(ndp, int) or ndp < 0):
                    _err(errors, f"{base}.modelDefaults.defaultPort",
                         f"{base}.modelDefaults.defaultPort must be a non-negative integer")
        # 嵌套 models（复用 _validate_models，共享 global_seen）
        if "models" in t:
            _validate_models(t.get("models", []), errors, path_prefix=base, seen=global_seen)

    # server 段校验（缺失/空 dict 不报错，默认值兜底）
    _validate_server_config(cfg.get("server"), errors)

    return errors


def _validate_server_config(server, errors: list) -> None:
    """校验顶层 server 段（缺失或空 dict 不报错，默认值兜底；未知旧键静默忽略）。"""
    if server is None:
        return
    if not isinstance(server, dict):
        _err(errors, "server", "server must be an object")
        return
    if not server:
        return

    port = server.get("listenPort")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or port < 0):
        _err(errors, "server.listenPort", "server.listenPort must be a non-negative integer")

    dport = server.get("dashboardPort")
    if dport is not None and (isinstance(dport, bool) or not isinstance(dport, int) or dport < 0):
        _err(errors, "server.dashboardPort", "server.dashboardPort must be a non-negative integer")

    log = server.get("log")
    if log is not None:
        if not isinstance(log, dict):
            _err(errors, "server.log", "server.log must be an object")
        else:
            dbg = log.get("debug")
            if dbg is not None and not isinstance(dbg, bool):
                _err(errors, "server.log.debug", "server.log.debug must be a boolean")
            f = log.get("file")
            if f is not None and not isinstance(f, str):
                _err(errors, "server.log.file", "server.log.file must be a string")
            rd = log.get("retentionDays")
            if rd is not None and (isinstance(rd, bool) or not isinstance(rd, int) or rd < 0):
                _err(errors, "server.log.retentionDays",
                     "server.log.retentionDays must be a non-negative integer")
            rw = log.get("rotateWhen")
            if rw is not None and not isinstance(rw, str):
                _err(errors, "server.log.rotateWhen", "server.log.rotateWhen must be a string")
            ri = log.get("rotateInterval")
            if ri is not None and (isinstance(ri, bool) or not isinstance(ri, int) or ri <= 0):
                _err(errors, "server.log.rotateInterval",
                     "server.log.rotateInterval must be a positive integer")

    cache = server.get("cache")
    if cache is not None:
        if not isinstance(cache, dict):
            _err(errors, "server.cache", "server.cache must be an object")
        else:
            en = cache.get("enabled")
            if en is not None and not isinstance(en, bool):
                _err(errors, "server.cache.enabled", "server.cache.enabled must be a boolean")
            for k in ("maxSize", "ttlSeconds", "maxItemSizeKb"):
                v = cache.get(k)
                if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                    _err(errors, f"server.cache.{k}",
                         f"server.cache.{k} must be a non-negative integer")


def _validate_models(models: list, errors: list, path_prefix: str = "",
                     seen: Optional[set] = None) -> None:
    """校验 models 结构和全局 name/alias 唯一性（追加结构化 {path,msg}）。

    path_prefix 非空时用于嵌套 models（如 anthropic target 的 targets[i].models），
    此时 path 前缀为 f"{path_prefix}.models[j]"；默认空 → 顶层 "models[j]"。
    seen 可传入共享 set 做跨命名空间唯一性检查（默认 None → 新建局部 set）。
    """
    if not isinstance(models, list):
        base_key = f"{path_prefix}.models" if path_prefix else "models"
        _err(errors, base_key, f"{base_key} must be a list")
        return

    if seen is None:
        seen = set()
    base_list = f"{path_prefix}.models" if path_prefix else "models"
    for i, m in enumerate(models):
        base = f"{base_list}[{i}]"
        if not isinstance(m, dict):
            _err(errors, base, f"{base} must be a dict")
            continue

        # name
        name = m.get("name")
        if not isinstance(name, str) or not name:
            _err(errors, f"{base}.name", f"{base} missing or invalid 'name' (non-empty string)")
        else:
            if name in seen:
                _err(errors, f"{base}.name", f"duplicate model name: '{name}'")
            seen.add(name)

        # aliases
        aliases = m.get("aliases", [])
        if not isinstance(aliases, list):
            _err(errors, f"{base}.aliases", f"{base} 'aliases' must be a list")
        else:
            for j, a in enumerate(aliases):
                if not isinstance(a, str):
                    _err(errors, f"{base}.aliases[{j}]", f"{base}.aliases[{j}] must be a string")
                else:
                    if a in seen:
                        _err(errors, f"{base}.aliases[{j}]",
                             f"duplicate alias '{a}' (used by name '{name}' or another alias)")
                    seen.add(a)

        # target
        target = m.get("target")
        if not isinstance(target, dict):
            _err(errors, f"{base}.target", f"{base} missing or invalid 'target' (must be dict)")
        else:
            port = target.get("port")
            if not isinstance(port, int):
                _err(errors, f"{base}.target.port", f"{base}.target.port must be an integer")
            elif port == ANTHROPIC_PORT:
                _err(errors, f"{base}.target.port",
                     f"{base}.target.port must not be {ANTHROPIC_PORT} (anthropic-compatible self-port, would create routing loop)")
            model_name = target.get("model")
            if not isinstance(model_name, str) or not model_name:
                _err(errors, f"{base}.target.model", f"{base}.target.model must be a non-empty string")


def _get_anthropic_target(targets_or_cfg) -> Optional[dict]:
    """从配置中找到 handler=="anthropic" 的 target（8081 翻译入口）。

    接受两种输入：
      - 完整 cfg dict（含 "targets" 键）→ 从 cfg["targets"] 中查找
      - targets list → 直接遍历查找

    返回该 target dict（浅引用，调用方可读取嵌套 models/modelDefaults），
    未找到返回 None。幂等：多个 anthropic target 时返回第一个。
    """
    if isinstance(targets_or_cfg, dict):
        targets = targets_or_cfg.get("targets", [])
    elif isinstance(targets_or_cfg, list):
        targets = targets_or_cfg
    else:
        return None
    for t in targets:
        if isinstance(t, dict) and t.get("handler") == "anthropic":
            return t
    return None


def _resolve_model_alias(models, requested_model: str) -> Optional[dict]:
    """
    统一别名解析纯函数：遍历 models 列表，若 requested_model == m["name"]
    或 requested_model in m["aliases"]，返回 m["target"]（{"port": int, "model": str}）。
    均未命中返回 None。

    models 参数支持三种输入（优先级从前到后）：
      1. 完整 cfg dict（含 "targets" 键）→ 优先从 handler=="anthropic" 的 target
         的嵌套 models[] 解析；若该 target 不存在或未命中，回退到顶层 models[]
      2. dict 含 "models" 键（顶层旧格式或 _MODELS_CFG 结构）→ 取 models["models"]
      3. models list → 直接遍历

    函数内对缺失字段容错（缺 name/aliases 时跳过该条，不抛异常）。
    """
    # 输入是完整 cfg dict（含 targets）→ 优先从 anthropic target 的嵌套 models 解析
    if isinstance(models, dict) and isinstance(models.get("targets"), list):
        anth_t = _get_anthropic_target(models)
        if anth_t is not None:
            nested = anth_t.get("models")
            if isinstance(nested, list):
                hit = _match_in_models_list(nested, requested_model)
                if hit is not None:
                    return hit
        # 回退到顶层 models（兼容旧格式 / 混合配置）
        models = models.get("models", [])
    elif isinstance(models, dict):
        models = models.get("models", [])
    elif not isinstance(models, list):
        models = []

    return _match_in_models_list(models, requested_model)


def _match_in_models_list(models: list, requested_model: str) -> Optional[dict]:
    """在 models list 中按 name/aliases 匹配，命中返回 target dict，否则 None。"""
    for m in models:
        if not isinstance(m, dict):
            continue
        # 检查 name 匹配
        if m.get("name") == requested_model:
            return m.get("target")
        # 检查 aliases 匹配
        aliases = m.get("aliases")
        if isinstance(aliases, list) and requested_model in aliases:
            return m.get("target")
    return None





def _validate_aggregator_target(t: dict, label: str, base: str, errors: list) -> None:
    """校验聚合网关 target：virtualModels / poolDefaults / quotaErrorPatterns。

    base 为该 target 的点号寻址前缀（如 targets[0]），错误以 {"path","msg"} 结构追加。
    """
    vm = t.get("virtualModels")
    if not isinstance(vm, dict) or not vm:
        _err(errors, f"{base}.virtualModels", f"aggregator target '{label}' 缺少 virtualModels")
        return
    for vmid, entry in vm.items():
        vbase = f"{base}.virtualModels.{vmid}"
        if not isinstance(entry, dict):
            _err(errors, vbase, f"aggregator target '{label}' 虚拟模型 '{vmid}' 配置必须为对象")
            continue
        default_pool = entry.get("defaultPool")
        if not isinstance(default_pool, list) or not default_pool:
            _err(errors, f"{vbase}.defaultPool",
                 f"aggregator target '{label}' 虚拟模型 '{vmid}' 缺少非空 defaultPool")
        else:
            for i, m in enumerate(default_pool):
                _validate_pool_member(label, vmid, "defaultPool", i, m, vbase, errors)
        fallback_pool = entry.get("fallbackPool")
        if fallback_pool is not None and not isinstance(fallback_pool, list):
            _err(errors, f"{vbase}.fallbackPool",
                 f"aggregator target '{label}' 虚拟模型 '{vmid}' 的 fallbackPool 必须为列表")
        elif isinstance(fallback_pool, list):
            for i, m in enumerate(fallback_pool):
                _validate_pool_member(label, vmid, "fallbackPool", i, m, vbase, errors)
        for key in ("defaultRetries", "fallbackRetries"):
            r = entry.get(key)
            if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
                _err(errors, f"{vbase}.{key}",
                     f"aggregator target '{label}' 虚拟模型 '{vmid}' 的 {key} 必须为非负整数")
    pd = t.get("poolDefaults")
    if pd is not None:
        if not isinstance(pd, dict):
            _err(errors, f"{base}.poolDefaults", f"aggregator target '{label}' 的 poolDefaults 必须为对象")
        else:
            for key in ("defaultRetries", "fallbackRetries", "sessionAffinityTtlSeconds",
                        "probeIntervalSeconds", "weight"):
                v = pd.get(key)
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                    _err(errors, f"{base}.poolDefaults.{key}",
                         f"aggregator target '{label}' 的 poolDefaults.{key} 必须为非负数字")
    qep = t.get("quotaErrorPatterns")
    if qep is not None and not isinstance(qep, list):
        _err(errors, f"{base}.quotaErrorPatterns", f"aggregator target '{label}' 的 quotaErrorPatterns 必须为列表")


def _validate_pool_member(label: str, vmid: str, pool_key: str, idx: int, m: object,
                          vbase: str, errors: list) -> None:
    """校验池成员：必须为 dict 且含 port(int)/model(str)；weight 可选，若非 None 必须为非负数字。"""
    mbase = f"{vbase}.{pool_key}[{idx}]"
    if not isinstance(m, dict):
        _err(errors, mbase, f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 必须为对象")
        return
    port = m.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        _err(errors, f"{mbase}.port",
             f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 port 必须为整数")
    elif port == ANTHROPIC_PORT:
        _err(errors, f"{mbase}.port",
             f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 port 不得为 {ANTHROPIC_PORT}（anthropic-compatible 自身端口，会形成路由死循环）")
    model = m.get("model")
    if not isinstance(model, str) or not model:
        _err(errors, f"{mbase}.model",
             f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 model 必须为非空字符串")
    weight = m.get("weight")
    if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0):
        _err(errors, f"{mbase}.weight",
             f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 weight 必须为非负数字")


def save_targets(cfg: dict, path: Path = TARGETS_PATH) -> None:
    """原子写 targets.json（临时文件 + rename，避免写一半）。"""
    _atomic_write_json(cfg, path)


def load_secrets(path: Path = SECRETS_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"secrets.json invalid JSON: {e}")
        return {}


def save_secrets(secrets: dict, path: Path = SECRETS_PATH) -> None:
    _atomic_write_json(secrets, path)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return value
    return f"{value[:5]}...{value[-3:]}"


def secret_key_for(target: dict) -> str:
    """
    target 在 secrets.json 中的存储 key。

    有 secretRef 用之；无 secretRef（直连网关 free/paid）统一约定 f"{label}_token"，
    使 dashboard 保存的兜底 token 也能被 resolve_secret 读回（历史上这类 target
    只能读 apikeyEnv 环境变量，存了读不出）。
    """
    ref = target.get("secretRef")
    if ref:
        return ref
    label = target.get("label") or ""
    return f"{label}_token" if label else ""


def resolve_secret(target: dict, secrets: dict) -> str:
    """解析 target 的私密 token：secretRef（或直连网关 f"{label}_token" 约定）
    → secrets.json → apikeyEnv 环境变量。"""
    key = secret_key_for(target)
    if key and secrets.get(key):
        return secrets[key]
    env_key = target.get("apikeyEnv")
    if env_key:
        return os.environ.get(env_key, "")
    return ""


def _atomic_write_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
