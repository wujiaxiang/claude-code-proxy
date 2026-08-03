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
VALID_HANDLERS = ("passthrough", "copilot", "qclaw", "gemini-native", "trae-work", "aggregator")

_REQUIRED_FIELDS = ("label", "listenPort", "category", "handler", "targetHost")


def load_targets(path: Path = TARGETS_PATH) -> dict:
    """加载 targets.json，兼容旧数组格式，自动迁移为新对象格式。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"targets.json not found: {path}, using empty config")
        return {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": []}
    except json.JSONDecodeError as e:
        logger.error(f"targets.json invalid JSON: {e}")
        return {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": []}

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
        cfg = {"targets": migrated, "modelDefaults": {"defaultPort": 8082}, "models": []}
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
    result = {
        "targets": normalized,
        "modelDefaults": raw.get("modelDefaults", {"defaultPort": 8082}),
        "models": raw.get("models", []),
    }
    return result


def validate_targets(cfg: dict) -> list:
    """校验配置，返回错误消息列表（空 = 通过）。"""
    errors = []
    targets = cfg.get("targets", [])
    labels = {}
    ports = {}

    for t in targets:
        label = t.get("label", "")
        disabled = t.get("enabled") is False
        if not disabled:
            for field in _REQUIRED_FIELDS:
                if field == "targetHost" and t.get("handler") == "aggregator":
                    continue  # 聚合 target 无真实上游 host，仅由 virtualModels 池定义
                if not t.get(field):
                    errors.append(f"target '{label}' 缺少必需字段: {field}")
            if t.get("category") not in VALID_CATEGORIES:
                errors.append(f"target '{label}' category 非法: {t.get('category')}（合法: {VALID_CATEGORIES}）")
            if t.get("handler") not in VALID_HANDLERS:
                errors.append(f"target '{label}' handler 非法: {t.get('handler')}（合法: {VALID_HANDLERS}）")
            if t.get("category") == "crack" and not t.get("crackTool"):
                errors.append(f"crack target '{label}' 缺少 crackTool")
            if t.get("handler") == "aggregator":
                _validate_aggregator_target(t, label, errors)
        if label in labels:
            errors.append(f"重复 label: '{label}'")
        labels[label] = True
        port = t.get("listenPort")
        if port in ports:
            errors.append(f"端口 {port} 被多个 target 占用 ({ports[port]}, {label})")
        ports[port] = label

    # modelDefaults.defaultPort 校验
    model_defaults = cfg.get("modelDefaults", {})
    default_port = model_defaults.get("defaultPort")
    if default_port is not None:
        if not isinstance(default_port, int) or default_port < 0:
            errors.append("modelDefaults.defaultPort must be a non-negative integer")

    # models 结构校验与全局唯一性
    _validate_models(cfg.get("models", []), errors)

    return errors


def _validate_models(models: list, errors: list) -> None:
    """校验 models 结构和全局 name/alias 唯一性。"""
    if not isinstance(models, list):
        errors.append("models must be a list")
        return

    seen = set()
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            errors.append(f"models[{i}] must be a dict")
            continue

        # name
        name = m.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"models[{i}] missing or invalid 'name' (non-empty string)")
        else:
            if name in seen:
                errors.append(f"duplicate model name: '{name}'")
            seen.add(name)

        # aliases
        aliases = m.get("aliases", [])
        if not isinstance(aliases, list):
            errors.append(f"models[{i}] 'aliases' must be a list")
        else:
            for j, a in enumerate(aliases):
                if not isinstance(a, str):
                    errors.append(f"models[{i}].aliases[{j}] must be a string")
                else:
                    if a in seen:
                        errors.append(f"duplicate alias '{a}' (used by name '{name}' or another alias)")
                    seen.add(a)

        # target
        target = m.get("target")
        if not isinstance(target, dict):
            errors.append(f"models[{i}] missing or invalid 'target' (must be dict)")
        else:
            port = target.get("port")
            if not isinstance(port, int):
                errors.append(f"models[{i}].target.port must be an integer")
            elif port == ANTHROPIC_PORT:
                errors.append(f"models[{i}].target.port must not be {ANTHROPIC_PORT} (anthropic-compatible self-port, would create routing loop)")
            model_name = target.get("model")
            if not isinstance(model_name, str) or not model_name:
                errors.append(f"models[{i}].target.model must be a non-empty string")


def _resolve_model_alias(models, requested_model: str) -> Optional[dict]:
    """
    统一别名解析纯函数：遍历 models 列表，若 requested_model == m["name"]
    或 requested_model in m["aliases"]，返回 m["target"]（{"port": int, "model": str}）。
    均未命中返回 None。models 参数允许传 list 或 dict（dict 时取 models["models"]，缺 key 视为空列表）。
    函数内对缺失字段容错（缺 name/aliases 时跳过该条，不抛异常）。
    """
    if isinstance(models, dict):
        models = models.get("models", [])
    elif not isinstance(models, list):
        models = []

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





ANTHROPIC_PORT = 8081  # 从 server.py:2891 的 _ANTHROPIC_PORT = int(os.environ.get("ANTHROPIC_PORT", "8081")) 同源默认值

def _validate_aggregator_target(t: dict, label: str, errors: list) -> None:
    """校验聚合网关 target：virtualModels / poolDefaults / quotaErrorPatterns。"""
    vm = t.get("virtualModels")
    if not isinstance(vm, dict) or not vm:
        errors.append(f"aggregator target '{label}' 缺少 virtualModels")
        return
    for vmid, entry in vm.items():
        if not isinstance(entry, dict):
            errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' 配置必须为对象")
            continue
        default_pool = entry.get("defaultPool")
        if not isinstance(default_pool, list) or not default_pool:
            errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' 缺少非空 defaultPool")
        else:
            for i, m in enumerate(default_pool):
                _validate_pool_member(label, vmid, "defaultPool", i, m, errors)
        fallback_pool = entry.get("fallbackPool")
        if fallback_pool is not None and not isinstance(fallback_pool, list):
            errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' 的 fallbackPool 必须为列表")
        elif isinstance(fallback_pool, list):
            for i, m in enumerate(fallback_pool):
                _validate_pool_member(label, vmid, "fallbackPool", i, m, errors)
        for key in ("defaultRetries", "fallbackRetries"):
            r = entry.get(key)
            if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
                errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' 的 {key} 必须为非负整数")
    pd = t.get("poolDefaults")
    if pd is not None:
        if not isinstance(pd, dict):
            errors.append(f"aggregator target '{label}' 的 poolDefaults 必须为对象")
        else:
            for key in ("defaultRetries", "fallbackRetries", "sessionAffinityTtlSeconds",
                        "probeIntervalSeconds", "weight"):
                v = pd.get(key)
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                    errors.append(f"aggregator target '{label}' 的 poolDefaults.{key} 必须为非负数字")
    qep = t.get("quotaErrorPatterns")
    if qep is not None and not isinstance(qep, list):
        errors.append(f"aggregator target '{label}' 的 quotaErrorPatterns 必须为列表")


def _validate_pool_member(label: str, vmid: str, pool_key: str, idx: int, m: object, errors: list) -> None:
    """校验池成员：必须为 dict 且含 port(int)/model(str)；weight 可选，若非 None 必须为非负数字。"""
    if not isinstance(m, dict):
        errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 必须为对象")
        return
    port = m.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 port 必须为整数")
    elif port == ANTHROPIC_PORT:
        errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 port 不得为 {ANTHROPIC_PORT}（anthropic-compatible 自身端口，会形成路由死循环）")
    model = m.get("model")
    if not isinstance(model, str) or not model:
        errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 model 必须为非空字符串")
    weight = m.get("weight")
    if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0):
        errors.append(f"aggregator target '{label}' 虚拟模型 '{vmid}' {pool_key}[{idx}] 的 weight 必须为非负数字")


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


def resolve_secret(target: dict, secrets: dict) -> str:
    """解析 target 的私密 token：secretRef → secrets.json → apikeyEnv 环境变量。"""
    ref = target.get("secretRef")
    if ref and secrets.get(ref):
        return secrets[ref]
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
