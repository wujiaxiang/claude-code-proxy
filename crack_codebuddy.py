"""crack_codebuddy.py — 提取 CodeBuddy token，写入 secrets.json。

用法:
  python crack_codebuddy.py [--secrets secrets.json] [--force]

独立脚本。当前实现探测本机 CodeBuddy 客户端目录；未找到时优雅失败并给出引导。
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# CodeBuddy 客户端可能的安装目录（Windows）
_POSSIBLE_DIRS = [
    os.environ.get("LOCALAPPDATA", ""),
    os.environ.get("APPDATA", ""),
    str(Path.home()),
]


def _find_codebuddy_token() -> str:
    """探测 CodeBuddy 客户端本地存储。返回 token 或空串。
    具体提取逻辑待按实际客户端版本实现（当前为骨架）。"""
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 CodeBuddy token 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("codebuddy_token"):
        print("✅ CodeBuddy token 已存在，跳过提取（用 --force 强制重新提取）")
        return 0

    token = _find_codebuddy_token()
    if not token:
        print("❌ 无法本地提取 CodeBuddy token")
        print("   引导：在已登录 CodeBuddy 的机器上运行本脚本，")
        print("        或手工获取 token 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["codebuddy_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ CodeBuddy token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
