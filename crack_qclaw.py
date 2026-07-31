"""crack_qclaw.py — 提取 QClaw API Key（Windows DPAPI 解密），写入 secrets.json。

用法:
  python crack_qclaw.py [--secrets secrets.json] [--force]

独立脚本，不 import server.py。仅依赖标准库 + cryptography（venv 已有）。
成功退出码 0；失败退出码 1 + 引导文案。
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
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
    env_key = os.environ.get("QCLAW_API_KEY", "").strip()
    if env_key:
        return env_key
    if sys.platform != "win32":
        return ""  # DPAPI 仅 Windows
    try:
        appdata = os.environ.get("APPDATA", "")
        app_store = os.path.join(appdata, "QClaw", "app-store.json")
        local_state = os.path.join(appdata, "QClaw", "Local State")
        if not os.path.exists(app_store):
            return ""
        with open(app_store, "r", encoding="utf-8") as f:
            store = json.load(f)
        entry = store.get("authGateway.providers.qclaw.apiKey")
        if entry is None:
            return ""
        cipher_b64 = entry["cipherText"] if isinstance(entry, dict) else entry
        raw = base64.b64decode(cipher_b64)
        if raw[:3] != b"v10" or not os.path.exists(local_state):
            return ""
        with open(local_state, "r", encoding="utf-8") as f:
            ls = json.load(f)
        enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
        if enc_key[:5] != b"DPAPI":
            return ""
        aes_key = _dpapi_unprotect(enc_key[5:])
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        encrypted = raw[3:]
        nonce = encrypted[:12]
        ct_and_tag = encrypted[12:]
        return AESGCM(aes_key).decrypt(nonce, ct_and_tag, None).decode("utf-8").strip()
    except Exception:
        return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 QClaw API Key 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("qclaw_api_key"):
        print(f"✅ QClaw API Key 已存在（{secrets['qclaw_api_key'][:6]}...），跳过提取（用 --force 强制重新提取）")
        return 0

    key = _decrypt_qclaw_api_key()
    if not key:
        print("❌ 无法本地提取 QClaw API Key")
        print("   引导：在已登录 QClaw 的 Windows 机器上运行本脚本，")
        print("        或手工获取 key 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["qclaw_api_key"] = key
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ QClaw API Key 已更新: {key[:8]}...{key[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
