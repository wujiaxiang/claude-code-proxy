"""Dashboard REST API 路由：全量配置导出/导入（api_config_export/api_config_import）。

把 targets.json + secrets.json 两个 gitignored 配置源打包成单个 JSON 文件，
实现"从 GitHub 下载代码 → 导入一个完整配置文件 → 达到现有配置效果"的迁移诉求。
物理文件保持分离（热重载粒度 / 私密凭据隔离是刻意设计，见 AGENTS.md §3），
导出/导入仅做快照级打包与还原。

v2：删除 env 段——.env 已废弃，运行配置（端口/日志/缓存等）并入 targets.json 顶层
server 段，随 targets 一起导出/导入。
"""

from datetime import datetime

from fastapi import HTTPException

import config_store as _cfg
import server as _srv
from server import (
    _refresh_secrets,
    _reload_targets,
    logger,
)
from dashboard.routes import dashboard_router

# 导出格式版本：结构变更（新增/删除顶层段）时递增，导入端据此拒绝旧版本。
# v2：移除 env 段，运行配置并入 targets.json server 段。
CONFIG_EXPORT_VERSION = 2


@dashboard_router.get("/api/config/export")
async def api_config_export():
    """全量配置导出：targets.json（含 server 段）+ secrets.json → 单个 JSON。

    返回体含完整私密凭据（secrets），调用方（dashboard 前端）应提示用户妥善保管。
    结构：{version, exportedAt, targets, secrets}
    注：load_targets() 返回完整 dict（含 targets/modelDefaults/models/server），
    server 段随 targets 顶层一起导出，无需额外处理。
    """
    return {
        "version": CONFIG_EXPORT_VERSION,
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "targets": _cfg.load_targets(),
        "secrets": _srv._SECRETS,
    }


@dashboard_router.post("/api/config/import")
async def api_config_import(payload: dict):
    """全量配置导入：校验 version → 写 targets.json/secrets.json → 热重载。

    - targets 段：先 validate_targets（复用配置校验，已扩展覆盖 server 段），
      非法则 422 且不写任何文件（原子性：要么全部生效，要么全部不动）。
    - secrets 段：整段覆盖写 secrets.json（含空值删除）。
    - 热重载：targets 走 _reload_targets（含端口 diff，仅消费 targets/modelDefaults/models），
      secrets 走 _refresh_secrets。server 段（端口/日志/缓存）为启动时读取，改动需重启进程生效。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="导入内容必须是 JSON 对象")
    version = payload.get("version")
    if version != CONFIG_EXPORT_VERSION:
        raise HTTPException(
            status_code=422,
            detail=f"配置文件版本 {version!r} 不受支持（当前支持 v{CONFIG_EXPORT_VERSION}）",
        )
    targets = payload.get("targets")
    secrets = payload.get("secrets")
    if not isinstance(targets, dict):
        raise HTTPException(status_code=422, detail="targets 段缺失或不是对象")
    if not isinstance(secrets, dict):
        raise HTTPException(status_code=422, detail="secrets 段缺失或不是对象")

    # 1. 校验 targets（失败则整体拒绝，不写任何文件；validate_targets 已覆盖 server 段）
    errors = _cfg.validate_targets(targets)
    if errors:
        raise HTTPException(status_code=422, detail={"configErrors": errors})

    # 2. 写 targets.json + secrets.json
    _cfg.save_targets(targets)
    _cfg.save_secrets(secrets)

    # 3. 热重载（targets 含端口 diff；secrets 即时生效；server 段需重启才生效）
    await _reload_targets()
    _refresh_secrets()

    logger.info(
        f"📦 config import: targets={len(targets.get('targets', []))} "
        f"secrets={len(secrets)}"
    )
    return {
        "ok": True,
        "targetsCount": len(targets.get("targets", [])),
        "secretsCount": len(secrets),
        "restartRequired": True,
        "message": "配置已导入并热重载（targets/secrets 已生效）；server 段运行配置（端口/日志/缓存）需重启进程后生效",
    }
