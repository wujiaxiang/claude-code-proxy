"""Dashboard REST API 路由：api_targets/api_update_target/api_prune_models/api_target_mapping/api_target_models_html（从 routes.py 抽出，行为不变）。"""

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

@dashboard_router.get("/api/targets")
async def api_targets():
    """返回全部 target 配置 + secrets 元信息（key 打码）+ 统计 + 破解环境检测。"""
    result = []
    for t in _srv._TARGETS:
        secret = _cfg.resolve_secret(t, _srv._SECRETS)
        item = {
            **t,
            "secretSet": bool(secret),
            "secretMasked": _cfg.mask_secret(secret),
            "stats": _TARGET_STATS.get(t["label"], {}),
        }
        if t.get("category") == "crack" and t.get("crackTool"):
            item["crackEnv"] = _crack_env_check(t)
        result.append(item)
    return {
        "anthropicForwardPort": _srv._MODELS_CFG["modelDefaults"].get("defaultPort", 8082),
        "targets": result,
    }


@dashboard_router.put("/api/models")
async def api_update_models(update: ModelsUpdate):
    """更新模型定义，写 targets.json 的 anthropic target（8081）嵌套 models[]/modelDefaults 并热重载。"""
    cfg = _cfg.load_targets()
    anth = _cfg._get_anthropic_target(cfg)
    if anth is None:
        raise HTTPException(status_code=422, detail="targets.json 缺少 handler=anthropic 的 8081 target（模型定义归属）")
    if update.models is not None:
        anth["models"] = update.models
    if update.modelDefaults is not None:
        anth["modelDefaults"] = update.modelDefaults
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True}




@dashboard_router.put("/api/targets/{label}")
async def api_update_target(label: str, update: TargetUpdate):
    """更新 target 非私密字段，写 targets.json 并热重载。"""
    cfg = _cfg.load_targets()
    for t in cfg["targets"]:
        if t["label"] == label:
            payload = update.model_dump(exclude_none=True)
            payload.pop("label", None)
            # ── 防御：过滤总开关等 UI 辅助行（旧版 bug 会混入 id="全部模型"）──
            if "models" in payload and isinstance(payload["models"], list):
                payload["models"] = [
                    m for m in payload["models"]
                    if not (isinstance(m, dict) and m.get("id") == "全部模型")
                    and not (isinstance(m, str) and m == "全部模型")
                ]
            t.update(payload)
            break
    else:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True, "label": label}



@dashboard_router.post("/api/targets/{label}/prune-models")
async def api_prune_models(label: str):
    """清理过期模型：拉取下游最新模型列表，删除 targets.json 中不在最新列表的模型。

    对照最新模型列表（_fetch_live_models 优先，失败则返回 422），把配置中
    「最新列表不存在」的模型从 targets.json 移除并热重载（含内存 _srv._TARGETS）。
    返回删除的模型列表。
    """
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    live = await _fetch_live_models(target)
    if not live:
        raise HTTPException(status_code=422, detail="无法拉取下游最新模型列表（上游不可达），无法清理")
    live_set = set(live)
    cfg = _cfg.load_targets()
    cfg_target = next((t for t in cfg["targets"] if t["label"] == label), None)
    if cfg_target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    # 模型定义保护：遍历全局 models[] 中 target.port 落在本 target 的记录，
    # 其 target.model 若在上游不存在则修正为同族可用模型（agg: 开头的聚合虚拟
    # 模型跳过，非上游模型）。保护后的 target.model 集合用于 kept/removed 判定，
    # 避免把仍被模型定义引用的模型误删。不落盘 cfg 内的修正，仅用于本次判定。
    cfg_models = cfg.get("models", [])
    protected_set = set()
    for rec in cfg_models:
        if not (isinstance(rec, dict) and isinstance(rec.get("target"), dict)):
            continue
        if int(rec["target"].get("port", -1)) != cfg_target["listenPort"]:
            continue
        tm = rec["target"].get("model")
        if not tm or str(tm).startswith("agg:"):
            continue
        if tm in live_set:
            protected_set.add(tm)
        else:
            fallback = None
            if any("haiku" in mm for mm in live_set):
                fallback = next((mm for mm in live_set if "haiku" in mm), None)
            elif any("sonnet" in mm for mm in live_set):
                fallback = next((mm for mm in live_set if "sonnet" in mm), None)
            if fallback:
                protected_set.add(fallback)
    removed = []
    kept = []
    for m in cfg_target.get("models", []):
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if mid and mid in live_set:
            kept.append(m)
        elif mid and mid in protected_set:
            kept.append(m)
        else:
            removed.append(mid)
    if removed:
        cfg_target["models"] = kept
        errors = _cfg.validate_targets(cfg)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        _cfg.save_targets(cfg)
        await _reload_targets()
    return {"ok": True, "label": label, "removed": removed, "keptCount": len(kept)}



@dashboard_router.get("/api/targets/{label}/mapping")
async def api_target_mapping(label: str):
    """返回下拉数据源候选：本 target 模型列表 + 聚合虚拟模型列表。"""
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    model_ids = []
    for m in target.get("models", []) or []:
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if mid:
            model_ids.append(mid)
    agg_models = []
    for t in _srv._TARGETS:
        if t.get("handler") == "aggregator" and t.get("enabled", True):
            agg_models.extend(sorted((t.get("virtualModels") or {}).keys()))
    return {
        "label": label,
        "models": model_ids,
        "aggModels": agg_models,
    }



@dashboard_router.get("/api/targets/{label}/models", response_class=HTMLResponse)
async def api_target_models_html(label: str, edit: int = 0):
    """返回单个 target 的模型区 HTML（edit=1 时渲染编辑态：全部模型 + 展示开关）。

    供 dashboard 前端「编辑模型」切换时无整页刷新重渲染。
    非 edit 态走统一接口 _get_target_models(label)（收敛四种 modelsSource）。
    edit=1 时优先从下游 /models 拉取真实模型列表（与 copilot 一致），
    拉取失败则降级为 targets.json 配置的 models。
    """
    from fastapi.responses import HTMLResponse as _HR
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    target_idx = next((i for i, t in enumerate(_srv._TARGETS) if t.get("label") == label), -1)
    if edit:
        # 编辑态以 targets.json 原始 models[] 为本地事实源做合并（上游优先）。
        models = target.get("models", [])
        live = await _fetch_live_models(target)
        if live:
            # 合并：以下游为准，保留 targets.json 中已存在的 enabled 状态
            local = {}
            for m in models:
                if isinstance(m, dict):
                    local[m.get("id", "")] = m.get("enabled", True)
                else:
                    local[str(m)] = True
            merged, seen = [], set()
            for mid in live:
                merged.append({"id": mid, "enabled": local.get(mid, True)})
                seen.add(mid)
            for mid, en in local.items():
                if mid and mid not in seen:
                    merged.append({"id": mid, "enabled": en})
            models = merged
    else:
        # aggregator 的模型是 virtualModels（由聚合网关卡片单独渲染，且不属于本端点
        # 的白名单编辑语义），此处沿用旧行为只看 models[]，避免凭空多出 10 个虚拟模型。
        models = ([] if _target_model_source(target) == "aggregator"
                  else await _get_target_models_async(label))
    html = _model_details_html(
        models,
        model_stats=_MODEL_STATS.get(label, {}),
        label=label,
        edit_mode=bool(edit),
        target_index=target_idx,
    )
    return _HR(html)


# ══════════════════════════════════════════════════════════════════════════════

