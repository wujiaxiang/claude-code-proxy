"""Dashboard REST API 路由：全量配置导出/导入（api_config_export/api_config_import）。

把 targets.json + secrets.json + .env 三个 gitignored 配置源打包成单个 JSON 文件，
实现"从 GitHub 下载代码 → 导入一个完整配置文件 → 达到现有配置效果"的迁移诉求。
物理文件保持分离（热重载粒度 / 私密凭据隔离是刻意设计，见 AGENTS.md §3），
导出/导入仅做快照级打包与还原。
"""

import json
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

# 导出格式版本：结构变更（新增/删除顶层段）时递增，导入端据此拒绝旧版本
CONFIG_EXPORT_VERSION = 1

# .env 中值得随配置导出的运行配置键（白名单，避免把任意环境变量泄入导出文件）。
# 与 AGENTS.md §3 的 .env 约定保持一致：仅运行配置（非私密），私密凭据一律在 secrets.json。
ENV_EXPORT_KEYS = (
    "DEBUG",
    "LOG_FILE",
    "LOG_RETENTION_DAYS",
    "LOG_ROTATE_WHEN",
    "LOG_ROTATE_INTERVAL",
    "CACHE_ENABLED",
    "CACHE_MAX_SIZE",
    "COPILOT_GHE_HOST",
    "COPILOT_INTEGRATION_ID",
    "COPILOT_BIG_MODEL",
    "COPILOT_MEDIUM_MODEL",
    "COPILOT_SMALL_MODEL",
)


def _env_file() -> str:
    """.env 绝对路径（与 server.py load_dotenv() 的默认搜索路径一致）。"""
    return str(_srv.__file__ and __import__("pathlib").Path(_srv.__file__).parent / ".env")


def _read_env_keys() -> dict:
    """读取 .env 白名单键（dotenv_values，不存在则空 dict）。"""
    from dotenv import dotenv_values
    try:
        vals = dotenv_values(_env_file()) or {}
    except Exception as e:
        logger.warning(f"config export: 读取 .env 失败: {e}")
        return {}
    return {k: vals[k] for k in ENV_EXPORT_KEYS if vals.get(k) is not None}


@dashboard_router.get("/api/config/export")
async def api_config_export():
    """全量配置导出：targets.json + secrets.json + .env 白名单键 → 单个 JSON。

    返回体含完整私密凭据（secrets），调用方（dashboard 前端）应提示用户妥善保管。
    结构：{version, exportedAt, targets, secrets, env}
    """
    return {
        "version": CONFIG_EXPORT_VERSION,
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "targets": _cfg.load_targets(),
        "secrets": _srv._SECRETS,
        "env": _read_env_keys(),
    }


@dashboard_router.post("/api/config/import")
async def api_config_import(payload: dict):
    """全量配置导入：校验 version → 写 targets.json/secrets.json/.env → 热重载。

    - targets 段：先 validate_targets（复用配置校验），非法则 422 且不写任何文件
      （原子性：要么全部生效，要么全部不动）。
    - secrets 段：整段覆盖写 secrets.json（含空值删除）。
    - env 段：仅写入白名单键（dotenv set_key 逐键更新，保留 .env 其他键），
      导入后需重启进程才完全生效（LOG_*/DEBUG/COPILOT_* 为启动时读取）。
    - 热重载：targets 走 _reload_targets（含端口 diff），secrets 走 _refresh_secrets。
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
    env = payload.get("env")
    if not isinstance(targets, dict):
        raise HTTPException(status_code=422, detail="targets 段缺失或不是对象")
    if not isinstance(secrets, dict):
        raise HTTPException(status_code=422, detail="secrets 段缺失或不是对象")
    if not isinstance(env, dict):
        raise HTTPException(status_code=422, detail="env 段缺失或不是对象")

    # 1. 校验 targets（失败则整体拒绝，不写任何文件）
    errors = _cfg.validate_targets(targets)
    if errors:
        raise HTTPException(status_code=422, detail={"configErrors": errors})

    # 2. 写 targets.json + secrets.json
    _cfg.save_targets(targets)
    _cfg.save_secrets(secrets)

    # 3. 写 .env（白名单键，逐键更新保留其他键）
    from dotenv import set_key
    env_written = 0
    for k, v in env.items():
        if k not in ENV_EXPORT_KEYS:
            continue
        try:
            set_key(_env_file(), k, str(v))
            env_written += 1
        except Exception as e:
            logger.warning(f"config import: 写入 .env {k} 失败: {e}")

    # 4. 热重载（targets 含端口 diff；secrets 即时生效）
    await _reload_targets()
    _refresh_secrets()

    logger.info(
        f"📦 config import: targets={len(targets.get('targets', []))} "
        f"secrets={len(secrets)} env={env_written} keys"
    )
    return {
        "ok": True,
        "targetsCount": len(targets.get("targets", [])),
        "secretsCount": len(secrets),
        "envWritten": env_written,
        "restartRequired": True,
        "message": "配置已导入并热重载（targets/secrets 已生效）；.env 运行配置（DEBUG/LOG_*/COPILOT_*）需重启进程后完全生效",
    }
