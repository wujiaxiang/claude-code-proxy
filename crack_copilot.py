"""crack_copilot.py — 提取 GitHub Copilot GHE token，写入 secrets.json。

用法:
  python crack_copilot.py [--secrets secrets.json] [--force]

独立脚本。优先尝试本机 gh CLI（gh auth token），失败则引导手工获取。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _try_gh_cli() -> str:
    """尝试 gh auth token（需要已登录 GitHub Enterprise）。"""
    try:
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _find_copilot_token() -> str:
    """探测本机 Copilot 客户端安装目录（骨架）。优先 gh CLI。"""
    token = _try_gh_cli()
    if token:
        return token
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Copilot GHE token 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("copilot_token"):
        print("✅ Copilot token 已存在，跳过提取（用 --force 强制重新提取）")
        return 0

    token = _find_copilot_token()
    if not token:
        print("❌ 无法本地提取 Copilot token")
        print("   引导：在已登录 GitHub CLI 的机器上运行 `gh auth token` 获取，")
        print("        或手工获取 token 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["copilot_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Copilot token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
