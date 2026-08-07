"""Dashboard REST API 路由：api_get_models/api_get_dangling（从 routes.py 抽出，行为不变）。"""

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

@dashboard_router.get("/api/models")
async def api_get_models():
    """返回全局模型定义（models[] + modelDefaults）。"""
    return {
        "models": _srv._MODELS_CFG.get("models", []),
        "modelDefaults": _srv._MODELS_CFG.get("modelDefaults", {"defaultPort": 8082}),
    }

@dashboard_router.get("/api/config/dangling")
async def api_get_dangling():
    """只读：返回配置中的悬空引用列表，供 dashboard 顶部警示条展示。"""
    return {"items": _scan_dangling_refs()}


