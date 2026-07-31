"""
破解工具统一 CLI 测试（无真实环境时优雅失败 + --force/--secrets 参数行为）。
用法: python test_crack_tools.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
passed = 0
failed = 0


def _run_tool(name, *args, env=None):
    return subprocess.run(
        [sys.executable, str(ROOT / name), *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )


def test_crack_qclaw_fails_gracefully_linux():
    """Linux 无 QClaw 环境：应优雅失败，退出码非 0，stdout 有引导文案。"""
    if sys.platform == "win32":
        return  # Windows 上可能真实解密，跳过
    with tempfile.TemporaryDirectory() as d:
        secrets_path = Path(d) / "secrets.json"
        r = _run_tool("crack_qclaw.py", "--secrets", str(secrets_path),
                      env={"APPDATA": str(Path(d) / "no-qclaw")})
        assert r.returncode != 0, f"应失败退出，实际 rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "❌" in r.stdout or "无法" in r.stdout, f"应有失败提示，实际: {r.stdout}"
        # 不应写入 secrets.json
        assert not secrets_path.exists() or "qclaw_api_key" not in (secrets_path.read_text() if secrets_path.exists() else "")


def test_crack_tools_all_exist():
    for name in ("crack_qclaw.py", "crack_codebuddy.py", "crack_copilot.py", "crack_traework.py"):
        assert (ROOT / name).exists(), f"缺少 {name}"


def test_crack_tool_help_or_usage():
    r = _run_tool("crack_qclaw.py", "--help")
    assert r.returncode == 0 or "用法" in r.stdout or "--secrets" in r.stdout


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            globals()["passed"] += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            globals()["failed"] += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            globals()["failed"] += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
