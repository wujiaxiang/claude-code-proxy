"""配置存储层：targets.json / secrets.json 加载、迁移、校验、原子写。

独立于 server.py，供透传引擎、dashboard API、破解工具共用。
优先级：secrets.json > 环境变量(apikeyEnv) > 客户端透传。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("config_store")

TARGETS_PATH = Path(__file__).parent / "targets.json"
SECRETS_PATH = Path(__file__).parent / "secrets.json"

VALID_CATEGORIES = ("crack", "free", "paid")
VALID_HANDLERS = ("passthrough", "copilot", "qclaw")

_REQUIRED_FIELDS = ("label", "listenPort", "category", "handler", "targetHost")


def load_targets(path: Path = TARGETS_PATH) -> dict:
    """加载 targets.json，兼容旧数组格式，自动迁移为新对象格式。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"targets.json not found: {path}, using empty config")
        return {"anthropicForwardPort": 8082, "targets": []}
    except json.JSONDecodeError as e:
        logger.error(f"targets.json invalid JSON: {e}")
        return {"anthropicForwardPort": 8082, "targets": []}

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
        cfg = {"anthropicForwardPort": 8082, "targets": migrated}
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
    return {
        "anthropicForwardPort": raw.get("anthropicForwardPort", 8082),
        "targets": normalized,
    }


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
                if not t.get(field):
                    errors.append(f"target '{label}' 缺少必需字段: {field}")
            if t.get("category") not in VALID_CATEGORIES:
                errors.append(f"target '{label}' category 非法: {t.get('category')}（合法: {VALID_CATEGORIES}）")
            if t.get("handler") not in VALID_HANDLERS:
                errors.append(f"target '{label}' handler 非法: {t.get('handler')}（合法: {VALID_HANDLERS}）")
            if t.get("category") == "crack" and not t.get("crackTool"):
                errors.append(f"crack target '{label}' 缺少 crackTool")
        if label in labels:
            errors.append(f"重复 label: '{label}'")
        labels[label] = True
        port = t.get("listenPort")
        if port in ports:
            errors.append(f"端口 {port} 被多个 target 占用 ({ports[port]}, {label})")
        ports[port] = label
    return errors


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
