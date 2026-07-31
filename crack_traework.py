"""crack_traework.py — 提取 Trae Work token，写入 secrets.json（预留骨架）。

用法:
  python crack_traework.py [--secrets secrets.json] [--force]

Trae Work 破解逻辑尚未实现，当前始终优雅失败并给出引导。
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _find_traework_token() -> str:
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Trae Work token 并写入 secrets.json（预留）")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("trae_work_token"):
        print("✅ Trae Work token 已存在，跳过提取")
        return 0

    token = _find_traework_token()
    if not token:
        print("❌ Trae Work 破解逻辑尚未实现（预留）")
        print("   请后续补充 crack_traework.py 的提取逻辑。")
        return 1

    secrets["trae_work_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Trae Work token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
