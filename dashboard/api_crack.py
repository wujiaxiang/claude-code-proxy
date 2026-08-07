"""Dashboard REST API 路由：api_crack_status/api_crack_schema/api_recrack/api_reload（从 routes.py 抽出，行为不变）。"""

import asyncio
import json
import socket
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import config_store as _cfg
import server as _srv
from server import (
    _ANTHROPIC_STATS,
    _MODEL_STATS,
    _TARGET_STATS,
    _anthropic_port_models,
    _build_models_list,
    _crack_env_check,
    _fetch_live_models,
    _get_target_models_async,
    _humanize_model_name,
    _refresh_secrets,
    _reload_targets,
    _run_crack_tool,
    _scan_dangling_refs,
    _target_model_source,
    crack_common,
    logger,
)
from dashboard.routes import dashboard_router
from dashboard.schemas import (
    ModelsUpdate, AggregateConfigUpdate, TargetUpdate, SecretUpdate, SecretBulkUpdate,
)
from dashboard.frontend import (
    _html_escape, _get_lan_ip, _format_uptime, _model_details_html, _build_card_html,
)

@dashboard_router.get("/api/crack/{label}/status")
async def api_crack_status(label: str):
    """破解网关状态查询：额度明细（含过期时间）+ 签到状态 + token 有效期。

    由 crack_common.CRACK_STATUS_HANDLERS 按 label 分发（trae-work 已实现，
    codebuddy/qclaw 待接入）。
    """
    if crack_common is None:
        raise HTTPException(status_code=503, detail="crack_common 模块不可用")
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    return crack_common.get_crack_status(label, _srv._SECRETS)



@dashboard_router.get("/api/crack/{label}/schema")
async def api_crack_schema(label: str):
    """返回该网关的凭据 schema（供 dashboard 凭据弹窗动态渲染表单）。"""
    if crack_common is None:
        raise HTTPException(status_code=503, detail="crack_common 模块不可用")
    schema = crack_common.CREDENTIAL_SCHEMAS.get(label)
    if schema is None:
        raise HTTPException(status_code=404, detail=f"网关 '{label}' 无凭据 schema")
    return schema



@dashboard_router.post("/api/targets/{label}/recrack")
async def api_recrack(label: str):
    """触发破解工具重新提取 token。"""
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    tool = target.get("crackTool")
    if not tool:
        raise HTTPException(status_code=422, detail=f"target '{label}' 无 crackTool")
    ok = _run_crack_tool(tool)
    if not ok:
        return {"ok": False, "label": label, "message": "破解工具执行失败，请查看日志或手工填写"}
    return {"ok": True, "label": label, "message": "破解工具执行成功"}



@dashboard_router.post("/api/reload")
async def api_reload():
    changes = await _reload_targets()
    return {"ok": True, "changes": changes}



