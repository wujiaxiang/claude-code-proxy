"""Dashboard REST API 路由：api_get_aggregate_config/api_update_aggregate_config/api_aggregate_status（从 routes.py 抽出，行为不变）。"""

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

@dashboard_router.get("/api/aggregate/config")
async def api_get_aggregate_config():
    """返回聚合网关（handler=aggregator）target 的可编辑配置。"""
    target = next((t for t in _srv._TARGETS if t.get("handler") == "aggregator"), None)
    if target is None:
        return {"configured": False}
    # 聚合可用端口列表（用于前端下拉选择 + 联动模型过滤）
    available_ports = {}
    for t in _srv._TARGETS:
        port_num = t.get("listenPort")
        if port_num is None:
            continue
        if t.get("handler") == "aggregator":
            # 聚合网关：模型 = virtualModels 的 key（agg:xxx 虚拟模型 id），
            # 供模型定义/聚合配置编辑里"选 8080 → 联动列出虚拟模型"使用。
            vm_models = list((t.get("virtualModels") or {}).keys())
            available_ports[str(port_num)] = {
                "label": t.get("name") or t.get("label") or str(port_num),
                "handler": "aggregator",
                "models": vm_models,
            }
            continue
        models = []
        for m in (t.get("models") or []):
            if isinstance(m, dict):
                if m.get("enabled", True):
                    models.append(m.get("id") or m.get("name", ""))
            elif isinstance(m, str):
                models.append(m)
        available_ports[str(port_num)] = {
            "label": t.get("label") or t.get("name") or str(port_num),
            "handler": t.get("handler"),
            "models": models,
        }
    return {
        "configured": True,
        "name": target.get("name") or target.get("label"),
        "virtualModels": target.get("virtualModels", {}),
        "poolDefaults": target.get("poolDefaults", {}),
        "quotaErrorPatterns": target.get("quotaErrorPatterns", []),
        "availablePorts": available_ports,
    }



@dashboard_router.put("/api/aggregate/config")
async def api_update_aggregate_config(update: AggregateConfigUpdate):
    """更新聚合网关虚拟模型/池默认值/配额熔断特征，写 targets.json 并热重载（引擎自动 reload）。"""
    cfg = _cfg.load_targets()
    target = next((t for t in cfg["targets"] if t.get("handler") == "aggregator"), None)
    if target is None:
        raise HTTPException(status_code=404, detail="未配置聚合网关 target（handler=aggregator）")
    if update.name is not None:
        target["name"] = update.name
    if update.virtualModels is not None:
        target["virtualModels"] = update.virtualModels
    if update.poolDefaults is not None:
        target["poolDefaults"] = update.poolDefaults
    if update.quotaErrorPatterns is not None:
        target["quotaErrorPatterns"] = update.quotaErrorPatterns
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True}



@dashboard_router.get("/api/aggregate/status")
async def api_aggregate_status():
    """聚合网关运行时状态：虚拟模型 per-member 统计、会话粘性命中率、熔断端口。不含任何密钥。"""
    engine = _srv._AGGREGATOR_ENGINE
    if engine is None:
        return {"configured": False}
    return {"configured": True, **engine.get_stats()}


