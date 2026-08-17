"""crack_traework.py — 提取 Trae Work（TRAE SOLO CN）认证信息，写入 secrets.json。

用法:
  python crack_traework.py [--secrets secrets.json] [--force]
  python crack_traework.py --checkin        # 查询今日签到状态
  python crack_traework.py --quota          # 查询剩余额度

破解要点（2026-08 实测）：
1. 认证数据存储：%APPDATA%\\TRAE SOLO CN\\User\\globalStorage\\storage.json
   - key: iCubeAuthInfo://icube.cloudide
   - CN 版是 "tc" 加密格式：Base64 -> [6B header][32B random][AES-128-CBC 密文]
   - 密钥派生：SHA-512(SHA-512(random) + salt)，salt = SALT_A XOR SALT_B（AES 类型）
   - 解密后：[64B SHA-512 hash][明文 JSON]
2. 明文 JSON 字段：token(Cloud-IDE-JWT access), refreshToken, userId, host, ...
3. access token 有效期 14 天，refresh token 约半年（refreshExpiredAt）
4. 签到接口：POST {host}/trae/api/v2/ug/checkin_credits/status|claim
5. 额度接口：POST {host}/trae/api/v2/pay/ide_user_ent_usage
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# ── tc 加密格式（来自 Trae CN 逆向，与 TRAE SOLO CN 通用）──
SALT_A = bytes([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37])
SALT_B = bytes([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125])
SALT_C = bytes([191,192,216,250,122,246,220,97,31,254,98,27,8,72,71,176,135,99,96,18,127,101,203,104,211,102,191,125,37,72,150,156,51,229,121,35,17,153,141,177,110,131,150,128,172,255,254,6,18,140,55,62,236,249,135,64,135,12,117,4,89,149,168,209])
SALT_D = bytes([246,204,26,232,232,70,129,109,223,146,169,242,23,241,105,145,50,196,165,42,254,120,3,54,244,207,209,85,53,6,138,106,175,148,31,204,186,186,165,182,87,142,49,10,39,110,26,154,86,56,173,125,18,64,198,225,99,99,83,82,191,134,76,170])
HEADER_SIZE = 6
RANDOM_BYTES_LEN = 32
HASH_SIZE = 64

# 请求头固定值（设备指纹）
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.51"
IDE_VERSION_CODE = "20260814"


def _xor_salts(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _detect_enc_type(header: bytes) -> str:
    if header[:6] == bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00]):
        return "AES"
    if header[:6] == bytes([18, 57, 32, 32, 2, 3]):
        return "AES_PRIVATE"
    return "UNKNOWN"


def _decrypt_tc(encrypted: bytes) -> str:
    """解密 tc 格式。返回明文 JSON 字符串。"""
    enc_type = _detect_enc_type(encrypted[:HEADER_SIZE])
    if enc_type == "UNKNOWN":
        raise ValueError(f"未知加密类型: {encrypted[:HEADER_SIZE].hex()}")
    random_bytes = encrypted[HEADER_SIZE:HEADER_SIZE + RANDOM_BYTES_LEN]
    enc_data = encrypted[HEADER_SIZE + RANDOM_BYTES_LEN:]
    salt = _xor_salts(SALT_C, SALT_D) if enc_type == "AES_PRIVATE" else _xor_salts(SALT_A, SALT_B)
    final_hash = hashlib.sha512(hashlib.sha512(random_bytes).digest() + salt).digest()
    key, iv = final_hash[:16], final_hash[16:32]

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(enc_data) + dec.finalize()
    plaintext = padded[:-padded[-1]]  # 去 PKCS7 padding

    stored_hash, payload = plaintext[:HASH_SIZE], plaintext[HASH_SIZE:]
    if hashlib.sha512(payload).digest() != stored_hash:
        raise ValueError("哈希校验失败，数据可能损坏或密钥不匹配")
    return payload.decode("utf-8")


def _find_work_storage_json() -> Path:
    """定位 TRAE SOLO CN 的 storage.json（Windows / macOS / 自定义）。"""
    candidates = []
    env = os.environ.get("TRAE_WORK_DATA_DIR")
    if env:
        candidates.append(Path(env))
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", ""))
        candidates.append(base / "TRAE SOLO CN")
        candidates.append(base / "Trae CN")  # 兼容
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "TRAE SOLO CN")
    for d in candidates:
        p = d / "User" / "globalStorage" / "storage.json"
        if p.exists():
            return p
    raise FileNotFoundError("未找到 Trae Work storage.json（请确认已登录 TRAE SOLO CN）")


def _read_work_auth() -> dict:
    """读取并解密 Trae Work 认证数据。"""
    storage_path = _find_work_storage_json()
    storage = json.loads(storage_path.read_text(encoding="utf-8"))
    raw = storage.get("iCubeAuthInfo://icube.cloudide")
    if not raw:
        raise KeyError("storage.json 中缺少 iCubeAuthInfo://icube.cloudide")
    # 明文直接解析
    if raw.strip().startswith("{") or raw.strip().startswith('"'):
        auth = json.loads(raw)
    else:
        auth = json.loads(_decrypt_tc(base64.b64decode(raw)))
    return auth


def _build_headers(auth: dict) -> dict:
    """构造 Trae API 请求头（设备指纹 + Cloud-IDE-JWT）。"""
    return {
        "Authorization": f"Cloud-IDE-JWT {auth['token']}",
        "Content-Type": "application/json",
        "X-Trae-Client-Type": "lite",
        "X-Trae-Authorized-Services": "feishu",
        "x-app-id": APP_ID,
        "x-app-version": "default",
        "x-ide-version-code": IDE_VERSION_CODE,
        "x-app-version-code": IDE_VERSION_CODE,
        "x-device-id": "199444637423849",
        "x-machine-id": "d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428",
        "x-device-type": "windows",
        "x-ide-version": IDE_VERSION,
        "x-ide-version-type": "stable",
        "request-traffic-type": "prod",
        "x-uid": str(auth.get("userId", "")),
    }


def _post(host: str, path: str, auth: dict, body: dict) -> dict:
    req = urllib.request.Request(
        f"{host}{path}", data=json.dumps(body).encode(), headers=_build_headers(auth), method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def checkin_status(auth: dict, host: str = "https://api.trae.cn") -> dict:
    """查询今日签到状态。"""
    return _post(host, "/trae/api/v2/ug/checkin_credits/status", auth, {})


def checkin_claim(auth: dict, host: str = "https://api.trae.cn") -> dict:
    """执行每日签到（领取 200 Work 专属积分）。"""
    return _post(host, "/trae/api/v2/ug/checkin_credits/claim", auth, {})


def query_quota(auth: dict, host: str = "https://api.trae.cn") -> dict:
    """查询剩余额度（权益包列表）。"""
    return _post(host, "/trae/api/v2/pay/ide_user_ent_usage", auth, {"require_usage": True, "req_source": 2})


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_token(secrets_path: Path, secrets: dict) -> dict:
    """获取认证数据：优先 secrets.json 里已存的 token，其次本地 storage.json 解密。

    secrets.json 是跨机器场景的主来源（token 已搬过来）；
    storage.json 是 Windows 本地首次提取场景的备用来源。
    """
    if secrets.get("trae_work_token"):
        return {
            "token": secrets["trae_work_token"],
            "refreshToken": secrets.get("trae_work_refresh_token", ""),
            "userId": secrets.get("trae_work_user_id", ""),
        }
    return _read_work_auth()


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Trae Work 认证信息并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    parser.add_argument("--checkin", action="store_true", help="查询今日签到状态")
    parser.add_argument("--quota", action="store_true", help="查询剩余额度")
    parser.add_argument("--claim", action="store_true", help="执行每日签到")
    parser.add_argument("--refresh", action="store_true", help="用 refreshToken 刷新 access token")
    parser.add_argument("--export", action="store_true", help="导出私密数据 JSON（供 dashboard 粘贴）")
    parser.add_argument("--import-json", metavar="FILE", help="从 JSON 文件/粘贴内容导入私密数据")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)

    # ── 导入模式：从 JSON 文件/粘贴内容导入私密数据（无需本地安装）──
    if args.import_json:
        import pathlib
        p = pathlib.Path(args.import_json)
        try:
            raw = p.read_text(encoding="utf-8") if p.exists() else args.import_json
            imported = json.loads(raw) if raw.strip().startswith("{") else json.loads(args.import_json)
        except Exception as e:
            print(f"❌ 解析 JSON 失败: {e}")
            return 1
        # 兼容两种命名：原始字段(traeauth) 与 secrets 字段
        t = imported.get("trae_work_token") or imported.get("token")
        rt = imported.get("trae_work_refresh_token") or imported.get("refreshToken")
        uid = imported.get("trae_work_user_id") or imported.get("userId") or ""
        if not t:
            print("❌ JSON 中缺少 token（字段名: trae_work_token 或 token）")
            return 1
        secrets["trae_work_token"] = t
        if rt:
            secrets["trae_work_refresh_token"] = rt
        if uid:
            secrets["trae_work_user_id"] = str(uid)
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 已导入: token={t[:14]}... refreshToken={'有' if rt else '无'} userId={uid}")
        return 0

    # --refresh 只需要 secrets.json 里的 refreshToken，无需本地 storage.json
    if args.refresh:
        return cmd_refresh(secrets_path, secrets)

    # 工具模式：签到 / 额度（优先 secrets.json 的 token，无需 Windows storage）
    try:
        auth = _get_token(secrets_path, secrets)
    except Exception as e:
        print(f"❌ 读取 Trae Work 认证失败: {e}")
        print("   请确认本机已安装并登录 TRAE SOLO CN（Trae Work），")
        print("   或先在 secrets.json 填写 trae_work_token，或设置 TRAE_WORK_DATA_DIR。")
        return 1

    if args.checkin:
        r = checkin_status(auth)
        print(f"签到状态: enabled={r.get('enable')} checked_in={r.get('checked_in')} 每日积分={r.get('credits')}")
        return 0
    if args.quota:
        r = query_quota(auth)
        print(f"积分计费: {r.get('is_credits_billing')} 美元计费: {r.get('is_dollar_usage_billing')}")
        for p in r.get("user_entitlement_pack_list", []):
            base = p.get("entitlement_base_info", {})
            pkg = base.get("product_extra", {}).get("package_extra", {})
            quota = pkg.get("quota", {})
            usage = p.get("usage", {})
            print(f"  - {p.get('display_desc')}: limit={quota.get('credits_limit')} used={usage.get('credits_amount', 0)}")
        return 0
    if args.claim:
        r = checkin_claim(auth)
        print(f"签到结果: code={r.get('code')} message={r.get('message')} credits={r.get('credits')}")
        return 0

    # ── 导出模式：输出私密数据 JSON（供另一台机器 dashboard 粘贴导入）──
    if args.export:
        try:
            auth = _read_work_auth()
        except Exception as e:
            print(f"❌ 读取本地认证失败: {e}")
            return 1
        out = {
            "trae_work_token": auth.get("token", ""),
            "trae_work_refresh_token": auth.get("refreshToken", ""),
            "trae_work_user_id": auth.get("userId", ""),
            "expired_at": auth.get("expiredAt", ""),
            "refresh_expired_at": auth.get("refreshExpiredAt", ""),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 默认模式：提取并写入 secrets.json
    if not args.force and secrets.get("trae_work_token"):
        print("✅ Trae Work token 已存在，跳过提取（用 --force 强制重新提取）")
        return 0

    secrets["trae_work_token"] = auth["token"]
    secrets["trae_work_refresh_token"] = auth.get("refreshToken", "")
    secrets["trae_work_user_id"] = auth.get("userId", "")
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Trae Work 认证已写入: token={auth['token'][:12]}... userId={auth.get('userId')}")
    print(f"   refreshToken 有效期至: {auth.get('refreshExpiredAt', '未知')}")
    return 0


def cmd_refresh(secrets_path: Path, auth: dict) -> int:
    """用 refreshToken 刷新 access token（POST cloudide ExchangeToken）。

    实测（2026-08-01）：设备已 BOUND 时无需 DeviceProof 签名即可刷新，
    返回新 Token + 新 RefreshToken。刷新成功后写回 secrets.json。
    """
    import urllib.error
    import urllib.request
    rt = auth.get("refreshToken", "") or _load_secrets(secrets_path).get("trae_work_refresh_token", "")
    if not rt:
        print("❌ 无 refreshToken，无法刷新")
        return 1
    body = json.dumps({
        "ClientID": "en1oxy7wnw8j9n",
        "RefreshToken": rt,
        "ClientSecret": "-",
        "UserID": "",
    }).encode()
    req = urllib.request.Request(
        "https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"❌ 刷新失败 HTTP {e.code}: {e.read().decode()[:300]}")
        return 1
    except Exception as e:
        print(f"❌ 刷新异常: {e}")
        return 1

    result = data.get("Result") or {}
    new_token = result.get("Token")
    if not new_token:
        print(f"❌ 刷新未返回 Token: {data.get('ResponseMetadata', {}).get('Error') or data}")
        return 1

    # 写回 secrets.json（token + 可能轮换的 refreshToken）
    secrets = _load_secrets(secrets_path)
    secrets["trae_work_token"] = new_token
    if result.get("RefreshToken"):
        secrets["trae_work_refresh_token"] = result["RefreshToken"]
    secrets["trae_work_bound_device_id"] = result.get("BoundDeviceID", "")
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")

    # 打印新 token 有效期
    try:
        p = new_token.split(".")[1]
        p += "=" * (-len(p) % 4)
        import base64
        exp = json.loads(base64.urlsafe_b64decode(p))["exp"]
        import datetime
        print(f"✅ access token 已刷新，新到期: {datetime.datetime.fromtimestamp(exp).isoformat()}")
    except Exception:
        print("✅ access token 已刷新")
    print(f"   已写回: {secrets_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
