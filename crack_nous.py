#!/usr/bin/env python3
"""提取 Nous Portal 凭据（来自 hermes 容器 auth.json）并写入 secrets.json。

与 codebuddy/copilot 的 crack 脚本不同：Nous Portal 凭据由 hermes 容器
（nousresearch/hermes-agent）统一管理（OAuth 登录 + 自动刷新回写 auth.json），
本脚本只做"只读提取拷贝"——把 providers.nous 的 access_token / expires_at 同步到
secrets.json（nous_access_token / nous_expires_at）。

⚠️ 本脚本**不触发刷新**：Nous Portal 的 refresh_token 是单次使用的，只有 hermes
进程可以调刷新端点；外部触发刷新若不持久化旋转后的 token，会导致 session 被
revoke（详见 gateways/nous.py docstring 踩坑记录）。刷新/重新登录在 hermes 内完成。

用法:
  python3 crack_nous.py [--secrets /path/secrets.json] [--force]
    --force  即使已有 key 也强制重新同步
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
AUTH_FILE = Path("/data/docker/hermes/data/auth.json")


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_auth_state() -> dict:
    """读 hermes auth.json 的 providers.nous state。"""
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"❌ 未找到 hermes auth.json: {AUTH_FILE}（hermes 容器未挂载或未登录）")
        return {}
    except Exception as e:
        print(f"❌ 读取 auth.json 失败: {e}")
        return {}
    return (data.get("providers") or {}).get("nous") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 Nous Portal 凭据（hermes auth.json → secrets.json）")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"))
    parser.add_argument("--force", action="store_true", help="即使已有 key 也强制重新同步")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("nous_access_token"):
        print("✅ nous_access_token 已存在，跳过同步（用 --force 强制重新同步）")
        return 0

    state = _read_auth_state()
    token = state.get("access_token") or ""
    if not token:
        print("❌ hermes auth.json 中无 access_token（请先在 hermes 中登录 Nous Portal）")
        return 1

    secrets["nous_access_token"] = token
    secrets["nous_expires_at"] = state.get("expires_at") or ""
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    exp = state.get("expires_at", "")
    print(f"✅ nous_access_token 已同步（expires_at: {exp}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
