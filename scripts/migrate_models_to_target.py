#!/usr/bin/env python3
"""一次性迁移脚本：把 targets.json 顶层 modelDefaults/models 迁进 8081 anthropic target。

背景：架构统一——模型路由表不再挂在 targets.json 顶层，而是作为 targets[] 中
handler=="anthropic" 的 8081 target 的嵌套子属性（models / modelDefaults）。
config_store.load_targets() 已做**内存态**自动迁移（见 _migrate_top_level_models_to_anthropic），
运行时行为早已是新格式；本脚本只负责把**磁盘文件**也显式改成新格式，
让 targets.json 读起来与运行时一致（顶层只剩 targets + server）。

用法：
    python scripts/migrate_models_to_target.py [--dry-run]
    python scripts/migrate_models_to_target.py --targets /path/targets.json

行为约定：
  - 只动顶层 models / modelDefaults 与新增的 anthropic target；
    targets[] 现有条目、server 段、其余顶层键一字不改（按原始 JSON 原地改，不走
    load_targets 的规范化，避免给现有 target 补写 isFree/enabled 等派生字段）
  - anthropic target 追加到 targets[] **末尾**（与现有结构一致，diff 最小）
  - 写回前用 config_store.validate_targets 校验，不通过则不写任何文件
  - 写回前把原文件备份为 targets.json.bak（shutil.copy2 保留 mtime），**不自动删除备份**
  - 幂等：已存在 handler=="anthropic" 的 target → nothing to migrate，退出 0

退出码：0 成功（含 nothing to migrate）/ 1 失败
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import config_store  # noqa: E402


def _has_anthropic_target(targets: list) -> bool:
    return any(isinstance(t, dict) and t.get("handler") == "anthropic" for t in targets)


def _listen_port(raw: dict) -> int:
    """anthropic target 的监听端口：优先 server.listenPort，缺失回落 ANTHROPIC_PORT。"""
    server = raw.get("server")
    if isinstance(server, dict):
        port = server.get("listenPort")
        if isinstance(port, int) and not isinstance(port, bool):
            return port
    return config_store.ANTHROPIC_PORT


def _build_anthropic_target(raw: dict, models: list, model_defaults: dict) -> dict:
    target = {
        "label": "anthropic",
        "listenPort": _listen_port(raw),
        "category": "free",
        "handler": "anthropic",
        "enabled": True,
    }
    if models:
        target["models"] = models
    if model_defaults:
        target["modelDefaults"] = model_defaults
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一次性迁移：targets.json 顶层 models/modelDefaults → 8081 anthropic target",
    )
    parser.add_argument("--targets", type=Path, default=_REPO_ROOT / "targets.json",
                        help="targets.json 路径（默认：仓库根 targets.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将迁移什么，不写任何文件")
    args = parser.parse_args()

    targets_path: Path = args.targets
    backup_path = targets_path.with_name(targets_path.name + ".bak")

    if not targets_path.exists():
        print(f"[跳过] targets.json 不存在：{targets_path}")
        print("nothing to migrate（新部署无需运行本脚本）")
        return 0

    try:
        with open(targets_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[失败] targets.json 不是合法 JSON：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[失败] 读取失败：{e}", file=sys.stderr)
        return 1

    if not isinstance(raw, dict):
        print("[失败] targets.json 是旧数组格式，请先启动一次代理让 config_store 迁成对象格式",
              file=sys.stderr)
        return 1

    targets = raw.get("targets")
    if not isinstance(targets, list):
        print("[失败] targets.json 缺少 targets 数组", file=sys.stderr)
        return 1

    top_models = raw.get("models")
    top_md = raw.get("modelDefaults")
    models = top_models if isinstance(top_models, list) else []
    model_defaults = top_md if isinstance(top_md, dict) else {}

    print(f"目标 targets ：{targets_path}")
    print(f"现有 targets[]：{len(targets)} 条")
    print(f"顶层 models   ：{len(models)} 条")
    print(f"顶层 modelDefaults：{model_defaults or '（空）'}")

    if _has_anthropic_target(targets):
        print("\n[跳过] targets[] 中已存在 handler=\"anthropic\" 的 target。")
        print("nothing to migrate（磁盘文件已是新格式）")
        return 0

    if not models and not model_defaults:
        print("\n[跳过] 顶层 models / modelDefaults 均为空，无可迁移内容。")
        print("nothing to migrate")
        return 0

    anth_target = _build_anthropic_target(raw, models, model_defaults)

    print(f"\n将创建 anthropic target（追加到 targets[] 末尾，第 {len(targets) + 1} 条）：")
    print(json.dumps(anth_target, ensure_ascii=False, indent=2))
    print("\n顶层将变更：")
    print(f"  models        : {len(models)} 条 → []（已迁入嵌套）")
    print(f"  modelDefaults : {model_defaults or '{}'} → {{}}（已迁入嵌套）")

    # 在候选结果上校验（不落盘）
    candidate = dict(raw)
    candidate["targets"] = [*targets, anth_target]
    candidate["models"] = []
    candidate["modelDefaults"] = {}
    errors = config_store.validate_targets(candidate)
    if errors:
        for e in errors:
            print(f"[失败] 校验不通过 {e['path']}: {e['msg']}", file=sys.stderr)
        print("未写任何文件。", file=sys.stderr)
        return 1
    print("\n[校验] validate_targets 通过。")

    if args.dry_run:
        print("[dry-run] 未写任何文件。去掉 --dry-run 执行真实迁移。")
        return 0

    if backup_path.exists():
        print(f"[提示] 备份已存在：{backup_path}，本次将覆盖。")

    try:
        shutil.copy2(targets_path, backup_path)
        config_store.save_targets(candidate, targets_path)
    except OSError as e:
        print(f"[失败] 文件操作出错：{e}", file=sys.stderr)
        return 1

    print(f"\n[完成] 已写入 {targets_path}")
    print(f"[完成] 原文件已备份为 {backup_path}（备份保留，删除由你决定）")
    print("提示：确认代理重启后行为正常，再删除备份。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
