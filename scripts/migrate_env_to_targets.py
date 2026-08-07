#!/usr/bin/env python3
"""一次性迁移脚本：把旧 .env 的运行配置并入 targets.json 顶层 server 段。

背景：配置架构整合——.env 废弃，运行配置统一到 targets.json 的 server 段
（server.py / config_store.py 已支持，见 config_store.DEFAULT_SERVER_CONFIG）。
本脚本只给**有旧 .env 的现有部署**（含 Windows Server 生产环境）用，新部署无需运行。

用法：
    python scripts/migrate_env_to_targets.py [--dry-run]
    python scripts/migrate_env_to_targets.py --targets /path/targets.json --env /path/.env

行为约定：
  - 只迁移 .env 中**存在且在映射表内**的键；其余键不动（server 段保留现有值/默认值）
  - 私密凭据（COPILOT_GHE_TOKEN / QCLAW_API_KEY 等）不迁移——归 secrets.json
  - 写回前把 .env 备份为 .env.bak（shutil.copy2 保留 mtime）
  - **不删除 .env**，最终删除由用户/部署脚本决定
  - 幂等：.env 不存在 → 提示 nothing to migrate 退出 0；重复运行只是把相同值再写一遍

退出码：0 成功（含 nothing to migrate）/ 1 失败
"""
import argparse
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import config_store  # noqa: E402

_TRUTHY = frozenset({"true", "1", "yes", "on"})


def _as_str(raw: str) -> str:
    """去掉手写残留的成对引号（dotenv 通常已剥离，这里是防御）。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _as_bool(raw: str) -> bool:
    return _as_str(raw).lower() in _TRUTHY


def _as_int(raw: str) -> int:
    return int(_as_str(raw))


# 映射表：.env 键 → (server 段路径, 转换器)。路径为 server 段内的点号寻址。
# 未列出的键一律忽略：私密凭据归 secrets.json，路径探测/运行时透传保持 env。
_MAPPINGS = (
    ("PREFERRED_PROVIDER", ("preferredProvider",), _as_str),
    ("ANTHROPIC_PORT", ("listenPort",), _as_int),
    ("COPILOT_GHE_HOST", ("copilot", "gheHost"), _as_str),
    ("COPILOT_INTEGRATION_ID", ("copilot", "integrationId"), _as_str),
    ("COPILOT_BIG_MODEL", ("copilot", "bigModel"), _as_str),
    ("COPILOT_MEDIUM_MODEL", ("copilot", "mediumModel"), _as_str),
    ("COPILOT_SMALL_MODEL", ("copilot", "smallModel"), _as_str),
    ("DEBUG", ("log", "debug"), _as_bool),
    ("LOG_FILE", ("log", "file"), _as_str),
    ("LOG_RETENTION_DAYS", ("log", "retentionDays"), _as_int),
    ("LOG_ROTATE_WHEN", ("log", "rotateWhen"), _as_str),
    ("LOG_ROTATE_INTERVAL", ("log", "rotateInterval"), _as_int),
    ("CACHE_ENABLED", ("cache", "enabled"), _as_bool),
    ("CACHE_MAX_SIZE", ("cache", "maxSize"), _as_int),
    ("CACHE_TTL_SECONDS", ("cache", "ttlSeconds"), _as_int),
    ("CACHE_MAX_ITEM_SIZE_KB", ("cache", "maxItemSizeKb"), _as_int),
    ("BIG_MODEL", ("legacyModels", "big"), _as_str),
    ("MEDIUM_MODEL", ("legacyModels", "medium"), _as_str),
    ("SMALL_MODEL", ("legacyModels", "small"), _as_str),
    ("QCLAW_BASE_URL", ("qclaw", "baseUrl"), _as_str),
)


def _plan(env_values: dict) -> tuple[list, list]:
    """按映射表解析 .env，返回 (待写入项, 转换失败项)。

    待写入项为 [(env_key, path 元组, 转换后的值)]；
    转换失败项为 [(env_key, 原始值, 错误说明)]——例如 LOG_RETENTION_DAYS=abc。
    """
    planned, failed = [], []
    for env_key, path, convert in _MAPPINGS:
        raw = env_values.get(env_key)
        if raw is None or _as_str(raw) == "":
            continue  # .env 没有该键（或空值）：不动 server 段现有值
        try:
            planned.append((env_key, path, convert(raw)))
        except ValueError as e:
            failed.append((env_key, raw, str(e)))
    return planned, failed


def _apply(server: dict, planned: list) -> None:
    """把待写入项写进 server 段（就地修改，嵌套 dict 缺失时补建）。"""
    for _env_key, path, value in planned:
        section = server
        for key in path[:-1]:
            sub = section.get(key)
            if not isinstance(sub, dict):
                sub = {}
                section[key] = sub
            section = sub
        section[path[-1]] = value


def _fmt_path(path: tuple) -> str:
    return "server." + ".".join(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一次性迁移：.env 运行配置 → targets.json server 段",
    )
    parser.add_argument("--targets", type=Path, default=_REPO_ROOT / "targets.json",
                        help="targets.json 路径（默认：仓库根 targets.json）")
    parser.add_argument("--env", type=Path, default=_REPO_ROOT / ".env",
                        help=".env 路径（默认：仓库根 .env）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将迁移什么，不写任何文件")
    args = parser.parse_args()

    env_path: Path = args.env
    targets_path: Path = args.targets
    backup_path = env_path.with_name(env_path.name + ".bak")

    if not env_path.exists():
        print(f"[跳过] .env 不存在：{env_path}")
        print("nothing to migrate（可能已迁移完成并删除，或本就是新部署）")
        return 0

    if backup_path.exists():
        print(f"[提示] 备份已存在：{backup_path}")
        print("       说明上次迁移后 .env 未被删除，本次将覆盖备份并继续。")

    try:
        from dotenv import dotenv_values
    except ImportError:
        print("[失败] 缺少 python-dotenv 依赖，请先 uv sync（或 pip install python-dotenv）",
              file=sys.stderr)
        return 1

    env_values = dotenv_values(env_path)
    planned, failed = _plan(env_values)

    for env_key, raw, reason in failed:
        print(f"[失败] {env_key}={raw!r} 类型转换失败：{reason}", file=sys.stderr)
    if failed:
        print("请修正 .env 中上述键的值后重试；未写入任何文件。", file=sys.stderr)
        return 1

    ignored = sorted(set(env_values) - {k for k, _p, _c in _MAPPINGS})

    print(f"源 .env      ：{env_path}")
    print(f"目标 targets ：{targets_path}")
    print(f"命中映射 {len(planned)} 个键：")
    for env_key, path, value in planned:
        print(f"  {env_key:<24} → {_fmt_path(path):<32} = {value!r}")
    if ignored:
        print(f"忽略 {len(ignored)} 个键（私密凭据/路径探测/运行时透传，保持原样）：")
        print("  " + ", ".join(ignored))

    if not planned:
        print("无可迁移的键，未写任何文件。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 未写任何文件。去掉 --dry-run 执行真实迁移。")
        return 0

    try:
        cfg = config_store.load_targets(targets_path)
        _apply(cfg["server"], planned)
        server_errors = [e for e in config_store.validate_targets(cfg)
                         if e["path"].startswith("server")]
        if server_errors:
            for e in server_errors:
                print(f"[失败] 校验不通过 {e['path']}: {e['msg']}", file=sys.stderr)
            print("未写任何文件。", file=sys.stderr)
            return 1

        shutil.copy2(env_path, backup_path)
        config_store.save_targets(cfg, targets_path)
    except OSError as e:
        print(f"[失败] 文件操作出错：{e}", file=sys.stderr)
        return 1

    print(f"\n[完成] 已写入 {targets_path}")
    print(f"[完成] .env 已备份为 {backup_path}（原 .env 保留，删除由你决定）")
    print("提示：确认代理重启后行为正常，再删除 .env。")
    return 0


if __name__ == "__main__":
    sys.exit(main())