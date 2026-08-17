"""crack_common.py — 破解网关公共能力：额度/签到/刷新状态查询。

每个 crack 网关注册一个查询函数到 `CRACK_STATUS_HANDLERS`，
dashboard 通过 `GET /api/crack/{label}/status` 调用，返回：
  {
    "quota":   [ {"name": ..., "limit": ..., "used": ..., "expireAt": ...}, ... ],
    "checkin": {"enabled": bool, "checkedIn": bool, "credits": ...},
    "refresh": {"tokenExpireAt": ..., "refreshExpireAt": ..., "boundDevice": ...},
    "extra":   {...}   # 各网关自定义（如账户名/region）
  }

设计目标：未来 codebuddy / qclaw 的签到、额度查询也挂到这里，统一在 dashboard 展示。
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import urllib.request
from pathlib import Path

# ── Trae Work 固定参数 ──
TRAE_API_HOST = "https://api.trae.cn"
TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
TRAE_IDE_VERSION = "0.1.51"
TRAE_IDE_VERSION_CODE = "20260814"
TRAE_DEVICE_ID = "199444637423849"
TRAE_MACHINE_ID = "d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428"
CLIENT_ID_SOLO = "en1oxy7wnw8j9n"


# ── tc 解密（Trae Work 本地存储 iCubeAuthInfo 的加密格式）──
_SALT_A = bytes([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37])
_SALT_B = bytes([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def decrypt_tc(encrypted: bytes) -> str:
    """解密 Trae Work 的 'tc' 加密认证数据，返回明文 JSON 字符串。"""
    import hashlib
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if len(encrypted) < 6 + 32 + 64 + 16:
        raise ValueError("buffer too short")
    header = encrypted[:6]
    enc_type = "AES" if header == bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00]) else (
        "AES_PRIVATE" if header == bytes([18, 57, 32, 32, 2, 3]) else "UNKNOWN")
    if enc_type == "UNKNOWN":
        raise ValueError(f"unknown enc type: {header.hex()}")
    random_bytes = encrypted[6:6 + 32]
    enc_data = encrypted[6 + 32:]
    salt = _xor(_SALT_A, _SALT_B) if enc_type == "AES" else _xor(bytes(64), bytes(64))
    final_hash = hashlib.sha512(hashlib.sha512(random_bytes).digest() + salt).digest()
    key, iv = final_hash[:16], final_hash[16:32]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(enc_data) + dec.finalize()
    plaintext = padded[:-padded[-1]]
    if hashlib.sha512(plaintext[64:]).digest() != plaintext[:64]:
        raise ValueError("hash verification failed")
    return plaintext[64:].decode("utf-8")


def trae_data_dir() -> Path:
    env = os.environ.get("TRAE_WORK_DATA_DIR", "")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "TRAE SOLO CN"
    return Path.home() / "AppData" / "Roaming" / "TRAE SOLO CN"


def trae_read_local_auth() -> dict:
    """从本地 storage.json 解密 Trae Work 认证数据（Windows 客户端已登录场景）。"""
    p = trae_data_dir() / "User" / "globalStorage" / "storage.json"
    if not p.exists():
        raise FileNotFoundError(f"Trae Work storage.json not found: {p}")
    storage = json.loads(p.read_text(encoding="utf-8"))
    raw = storage.get("iCubeAuthInfo://icube.cloudide")
    if not raw:
        raise KeyError("iCubeAuthInfo://icube.cloudide not found")
    if raw.strip().startswith("{") or raw.strip().startswith('"'):
        return json.loads(raw)
    return json.loads(decrypt_tc(base64.b64decode(raw)))


def _trae_headers(token: str) -> dict:
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-app-id": TRAE_APP_ID,
        "x-app-version": "default",
        "x-app-version-code": TRAE_IDE_VERSION_CODE,
        "x-ide-version-code": TRAE_IDE_VERSION_CODE,
        "x-ide-version": TRAE_IDE_VERSION,
        "x-ide-version-type": "stable",
        "x-device-id": TRAE_DEVICE_ID,
        "x-machine-id": TRAE_MACHINE_ID,
        "x-device-type": "windows",
        "x-os-version": "Windows 10",
        "x-device-brand": "Standard PC (Q35 + ICH9, 2009)",
        "x-device-cpu": "KVM",
        "x-trae-authorized-services": "feishu",
        "request-traffic-type": "prod",
        "X-Trae-Client-Type": "lite",
    }


def _post_json(url: str, body: dict, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers=_trae_headers(token), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _jwt_exp(token: str) -> datetime.datetime | None:
    """解析 JWT exp，返回 (datetime|None)。"""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        exp = json.loads(base64.urlsafe_b64decode(p)).get("exp")
        return datetime.datetime.fromtimestamp(exp) if exp else None
    except Exception:
        return None


def trae_quota(token: str) -> list:
    """Trae Work 剩余额度（权益包明细，含过期时间）。"""
    data = _post_json(f"{TRAE_API_HOST}/trae/api/v2/pay/ide_user_ent_usage",
                      {"require_usage": True, "req_source": 2}, token)
    packs = []
    for p in data.get("user_entitlement_pack_list", []):
        base = p.get("entitlement_base_info", {})
        pkg = base.get("product_extra", {}).get("package_extra", {})
        quota = pkg.get("quota", {}) or {}
        usage = p.get("usage", {}) or {}
        end = base.get("end_time") or p.get("expire_time")
        packs.append({
            "name": p.get("display_desc") or pkg.get("package_name") or "权益",
            "limit": quota.get("credits_limit"),
            "used": usage.get("credits_amount", 0),
            "status": p.get("status"),
            "expireAt": datetime.datetime.fromtimestamp(end).isoformat() if end else None,
        })
    return packs


def trae_checkin(token: str) -> dict:
    """Trae Work 今日签到状态。"""
    data = _post_json(f"{TRAE_API_HOST}/trae/api/v2/ug/checkin_credits/status", {}, token)
    return {
        "enabled": bool(data.get("enable", False)),
        "checkedIn": bool(data.get("checked_in", False)),
        "credits": data.get("credits"),
        "message": data.get("message", ""),
    }


def trae_status(token: str, refresh_token: str = "") -> dict:
    """Trae Work 完整状态（额度 + 签到 + token 有效期）。"""
    result = {"quota": [], "checkin": {}, "refresh": {}, "extra": {}}
    if not token:
        return result
    try:
        result["quota"] = trae_quota(token)
    except Exception as e:
        result["quota"] = [{"error": str(e)[:200]}]
    try:
        result["checkin"] = trae_checkin(token)
    except Exception as e:
        result["checkin"] = {"error": str(e)[:200]}
    _exp = _jwt_exp(token)
    result["refresh"] = {
        "tokenExpireAt": _exp.isoformat() if _exp else None,
        "refreshExpireAt": None,
    }
    # 从额度响应提取 userId 作为账号显示（Trae 无公开昵称端点）
    try:
        packs = result.get("quota") or []
        uid = None
        for p in packs:
            uid = None
            # quota 项里不直接带 userId；重新调一次额度接口拿顶层字段
            break
        data = _post_json(f"{TRAE_API_HOST}/trae/api/v2/pay/ide_user_ent_usage",
                          {"require_usage": True, "req_source": 2}, token)
        packs2 = data.get("user_entitlement_pack_list") or []
        if packs2:
            uid = (packs2[0].get("entitlement_base_info") or {}).get("user_id")
        if uid:
            result["extra"]["userId"] = str(uid)
    except Exception:
        pass
    return result


# ── 破解网关状态查询注册表 ──
# handler 签名约定：
#   标准：handler(token: str, refresh_token: str = "") -> dict
#   多字段（qclaw 需要 guid/userId/jwt/device 等多个 secrets）：handler(secrets: dict) -> dict
# 通过 HANDLER_TAKES_SECRETS 标记。
CRACK_STATUS_HANDLERS = {
    "trae-work": trae_status,
    "codebuddy": None,   # 延迟导入（crack_codebuddy_q 依赖 httpx）
    "copilot":   None,   # 延迟导入（个人版，api.github.com）
    "copilot-enterprise": None,  # 延迟导入（企业版 GHE，api.bmw.ghe.com）
    "qclaw":     None,   # 延迟导入
}

# 这些 handler 接收完整 secrets dict（而非单一 token）
HANDLER_TAKES_SECRETS = {"qclaw"}

# label → 状态查询函数名（延迟导入后映射到模块属性）
_HANDLER_FUNC = {
    "codebuddy": "codebuddy_status",
    "copilot": "copilot_personal_status",
    "copilot-enterprise": "copilot_status",
    "qclaw": "qclaw_status",
}


def _import_handler(label: str):
    """延迟导入各网关 handler（避免 crack_common 顶部依赖 httpx 等）。"""
    if CRACK_STATUS_HANDLERS.get(label) is not None:
        return
    mod_name, func_name = {
        "codebuddy": ("crack_codebuddy_q", "codebuddy_status"),
        "copilot": ("crack_copilot_q", "copilot_personal_status"),
        "copilot-enterprise": ("crack_copilot_q", "copilot_status"),
        "qclaw": ("crack_qclaw_q", "qclaw_status"),
    }.get(label, (None, None))
    if mod_name:
        if not func_name:
            return
        import importlib
        mod = importlib.import_module(mod_name)
        CRACK_STATUS_HANDLERS[label] = getattr(mod, func_name)


# ── 破解网关凭据 schema 注册表 ──
# 供 dashboard 凭据弹窗动态渲染表单 + bulk API 校验。
# 字段: key(secrets 名) / label(显示名) / type(password|text|number) / required / hint(提示语)
#       pattern(正则, None 不校验) / placeholder
# jsonImportMapping: 原始字段名 → secrets 字段名（兼容 --export 输出与客户端原始命名）
# readonlyFields: 只读字段（查询结果，不需手动填写，JSON 导入时忽略）
CREDENTIAL_SCHEMAS = {
    "copilot-enterprise": {
        "displayName": "Copilot 企业版 (GHE)",
        "fields": [
            {"key": "copilot_token", "label": "GitHub PAT", "type": "password", "required": True,
             "hint": "GHE 企业版 fine-grained PAT，前缀 github_pat_，需 Copilot 权限",
             "pattern": r"^github_pat_[A-Za-z0-9_]{20,}$", "placeholder": "github_pat_xxxx..."},
        ],
        "jsonImportMapping": {"token": "copilot_token"},
        "readonlyFields": [],
    },
    "copilot": {
        "displayName": "Copilot 个人版",
        "fields": [
            {"key": "copilot_personal_token", "label": "GitHub OAuth Token", "type": "password", "required": True,
             "hint": "gho_ 前缀的 OAuth token，从本机 ~/.copilot/config.json 破解（crack_copilot.py）",
             "pattern": r"^gho_[A-Za-z0-9]{20,}$", "placeholder": "gho_xxxx..."},
        ],
        "jsonImportMapping": {"token": "copilot_personal_token"},
        "readonlyFields": [],
    },
    "qclaw": {
        "displayName": "QClaw",
        # 最小原则：LLM 转发只需 api_key；其余字段为积分/额度查询增强，缺省时状态区显示降级提示
        "minimumNote": "仅填 API Key 即可正常代理；其余字段用于积分/额度查询（可从 QClaw 客户端自动提取）",
        "fields": [
            {"key": "qclaw_api_key", "label": "API Key", "type": "password", "required": True,
             "hint": "sk- 前缀，LLM 网关认证（必填，仅此字段即可正常代理）",
             "pattern": r"^sk-[A-Za-z0-9]+$", "placeholder": "sk-xxx..."},
            {"key": "qclaw_openclaw_token", "label": "OpenClaw JWT（可选）", "type": "password", "required": False,
             "hint": "jprx 业务网关 X-OpenClaw-Token，HS256 JWT，用于积分查询",
             "pattern": r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", "placeholder": ""},
            {"key": "qclaw_guid", "label": "GUID（可选）", "type": "text", "required": False,
             "hint": "设备 GUID（X-Guid 头），用于积分查询",
             "pattern": r"^[0-9a-fA-F-]{8,}$", "placeholder": ""},
            {"key": "qclaw_user_id", "label": "User ID（可选）", "type": "text", "required": False,
             "hint": "QClaw 账号 ID（X-Account 头），用于积分查询",
             "pattern": r"^\d+$", "placeholder": ""},
            {"key": "qclaw_device_token", "label": "Device Token（可选）", "type": "password", "required": False,
             "hint": "X-Qclaw-DeviceToken，设备绑定令牌，用于积分查询",
             "pattern": None, "placeholder": ""},
            {"key": "qclaw_login_key", "label": "Login Key（可选）", "type": "password", "required": False,
             "hint": "X-Token 头，新版 QClaw 已无此字段，留空即可",
             "pattern": None, "placeholder": ""},
        ],
        "jsonImportMapping": {
            "api_key": "qclaw_api_key", "openclaw_token": "qclaw_openclaw_token",
            "guid": "qclaw_guid", "userId": "qclaw_user_id",
            "device_token": "qclaw_device_token", "login_key": "qclaw_login_key",
        },
        "readonlyFields": [],
    },
    "codebuddy": {
        "displayName": "CodeBuddy",
        "fields": [
            {"key": "codebuddy_token", "label": "Access Token (JWT)", "type": "password", "required": True,
             "hint": "JWT 格式，从 CodeBuddy 客户端本地存储提取",
             "pattern": r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", "placeholder": "eyJhbGciOi..."},
            {"key": "codebuddy_refresh_token", "label": "Refresh Token", "type": "password", "required": False,
             "hint": "用于刷新 access token（可空）",
             "pattern": None, "placeholder": ""},
            {"key": "codebuddy_uid", "label": "UID", "type": "text", "required": False,
             "hint": "CodeBuddy 账号 UID（数字）",
             "pattern": r"^\d+$", "placeholder": ""},
        ],
        "jsonImportMapping": {
            "token": "codebuddy_token", "refreshToken": "codebuddy_refresh_token", "uid": "codebuddy_uid",
        },
        "readonlyFields": ["codebuddy_nickname"],
    },
    "trae-work": {
        "displayName": "Trae Work",
        "fields": [
            {"key": "trae_work_token", "label": "Access Token", "type": "password", "required": True,
             "hint": "JWT 格式，从 TRAE SOLO CN storage.json 提取（crack_traework.py --export 导出）",
             "pattern": r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", "placeholder": "eyJhbGciOi..."},
            {"key": "trae_work_refresh_token", "label": "Refresh Token", "type": "password", "required": True,
             "hint": "用于自动刷新 access token（剩 <2 天时 crack_daily 触发刷新）",
             "pattern": None, "placeholder": ""},
            {"key": "trae_work_user_id", "label": "User ID", "type": "text", "required": False,
             "hint": "Trae 账号 ID（数字），请求头 x-uid 需要",
             "pattern": r"^\d+$", "placeholder": "123456789"},
        ],
        "jsonImportMapping": {
            "token": "trae_work_token", "refreshToken": "trae_work_refresh_token", "userId": "trae_work_user_id",
        },
        "readonlyFields": [],
    },
}

# 网关能力声明（前端据此隐藏签到/刷新行；静态注册表，不从 checkin.enabled 推导）
_GATEWAY_CAPABILITIES = {
    "trae-work":          {"hasCheckin": True,  "hasRefresh": True},
    "codebuddy":          {"hasCheckin": True,  "hasRefresh": True},
    "qclaw":              {"hasCheckin": False, "hasRefresh": False},
    "copilot-enterprise": {"hasCheckin": False, "hasRefresh": False},
    "copilot":            {"hasCheckin": False, "hasRefresh": False},
}

# 账号显示名提取（优先名称，回退 id；实在没有显示 "—"）
_ACCOUNT_EXTRACTORS = {
    "copilot-enterprise": lambda s: s.get("extra", {}).get("login", ""),
    "copilot":           lambda s: s.get("extra", {}).get("login", ""),
    "codebuddy":         lambda s: s.get("extra", {}).get("nickname", ""),
    "qclaw":             lambda s: s.get("extra", {}).get("nickname", "") or s.get("extra", {}).get("userId", ""),
    "trae-work":         lambda s: s.get("extra", {}).get("nickname", "") or s.get("extra", {}).get("userId", ""),
}

# 网关显示名（前端标题用）
_GATEWAY_DISPLAY_NAMES = {
    "copilot-enterprise": "Copilot 企业版",
    "copilot": "Copilot 个人版",
    "qclaw": "QClaw",
    "codebuddy": "CodeBuddy",
    "trae-work": "Trae Work",
}


_LAST_RUN_FILE = Path(__file__).parent / ".cache" / "crack_daily_last_run"


def _read_last_daily_run() -> str | None:
    """读取 crack_daily.py 每次运行后写的时间戳文件。

    用仓库内 .cache/ 路径（不用 /tmp：代理进程可能跑在独立 mount namespace，
    私有 /tmp 与 cron 进程的 /tmp 不同，读不到）。
    """
    try:
        v = _LAST_RUN_FILE.read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def get_crack_status(label: str, secrets: dict) -> dict:
    """dashboard 统一入口：按 label 查额度/签到状态。"""
    handler = CRACK_STATUS_HANDLERS.get(label)
    if handler is None:
        _import_handler(label)
        handler = CRACK_STATUS_HANDLERS.get(label)
    if not handler:
        return {"label": label, "supported": False, "message": "该破解网关暂未接入状态查询"}
    token = secrets.get(f"{label.replace('-', '_')}_token", "")
    refresh = secrets.get(f"{label.replace('-', '_')}_refresh_token", "")
    # copilot-enterprise 的 token 存在 copilot_token（非 copilot_enterprise_token）
    if label == "copilot-enterprise":
        token = secrets.get("copilot_token", "")
    # copilot（个人版）的 token 存在 copilot_personal_token
    elif label == "copilot":
        token = secrets.get("copilot_personal_token", "")
    # qclaw 需要多个 secrets 字段，token 判定用 qclaw_api_key
    if label == "qclaw":
        if not (secrets.get("qclaw_api_key") or secrets.get("qclaw_openclaw_token")):
            return {"label": label, "supported": True, "configured": False, "message": "qclaw 认证未配置"}
    elif not token:
        return {"label": label, "supported": True, "configured": False, "message": "token 未配置"}
    try:
        if label in HANDLER_TAKES_SECRETS:
            status = handler(secrets)
        else:
            status = handler(token, refresh)
        status.update({"label": label, "supported": True, "configured": True})
    except Exception as e:
        return {"label": label, "supported": True, "configured": True,
                "error": str(e)[:300]}
    # 统一装配：displayName / account / capabilities / lastDailyRun
    status["displayName"] = _GATEWAY_DISPLAY_NAMES.get(label, label)
    status["account"] = _ACCOUNT_EXTRACTORS.get(label, lambda s: "")(status) or "—"
    status["capabilities"] = _GATEWAY_CAPABILITIES.get(label, {"hasCheckin": False, "hasRefresh": False})
    status["lastDailyRun"] = _read_last_daily_run()
    return status
