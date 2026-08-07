"""Dashboard REST API 路由：api_update_secret/api_update_secret_bulk（从 routes.py 抽出，行为不变）。"""

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

@dashboard_router.put("/api/secrets/{label}")
async def api_update_secret(label: str, update: SecretUpdate):
    """更新 target 的私密 key/token，写 secrets.json 并热加载。"""
    cfg = _cfg.load_targets()
    target = next((t for t in cfg["targets"] if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    # 无 secretRef 的直连网关（free/paid）统一落到约定 key f"{label}_token"，
    # 与 config_store.resolve_secret 的读取约定一致（存得进也读得出）
    ref = _cfg.secret_key_for(target)
    if not ref:
        raise HTTPException(status_code=422, detail=f"target '{label}' 无法确定 secrets key")
    secrets = _cfg.load_secrets()
    if update.value:
        secrets[ref] = update.value
    else:
        secrets.pop(ref, None)
    _cfg.save_secrets(secrets)
    _refresh_secrets()
    return {"ok": True, "label": label, "secretRef": ref, "secretSet": bool(update.value)}


@dashboard_router.put("/api/secrets/{label}/bulk")
async def api_update_secret_bulk(label: str, update: SecretBulkUpdate):
    """批量导入破解网关凭据（dashboard 表单/JSON 双模式提交），按 schema 校验。

    校验规则（来自 crack_common.CREDENTIAL_SCHEMAS）：
      - 字段映射：原始名（token/refreshToken/...）→ secrets 名，或直接 secrets 名
      - pattern 校验：字段定义了正则则必须匹配，否则 422
      - 未知字段：报错（避免"保存了但没生效"的困惑）
      - 只读字段（readonlyFields）：忽略不写入
    """
    import re as _re
    target = next((t for t in _srv._TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    schema = crack_common.CREDENTIAL_SCHEMAS.get(label) if crack_common else None
    if schema is None:
        raise HTTPException(status_code=422, detail=f"网关 '{label}' 无凭据 schema")

    field_keys = {f["key"] for f in schema["fields"]}
    import_mapping = schema.get("jsonImportMapping", {})
    readonly = set(schema.get("readonlyFields", []))
    patterns = {f["key"]: f.get("pattern") for f in schema["fields"]}
    required_keys = {f["key"] for f in schema["fields"] if f.get("required")}

    secrets = _cfg.load_secrets()
    errors: list[str] = []
    count = 0
    for k, v in update.data.items():
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        # 字段映射：直接 secrets 名，或原始名 → secrets 名
        secret_key = k if k in field_keys else import_mapping.get(k)
        if secret_key is None:
            errors.append(f"未知字段 '{k}'（该网关 schema 无此字段）")
            continue
        if secret_key in readonly:
            continue  # 只读字段（查询结果）忽略
        pat = patterns.get(secret_key)
        if pat:
            try:
                if not _re.match(pat, v):
                    errors.append(f"字段 '{secret_key}' 格式不符")
                    continue
            except _re.error:
                pass  # pattern 非法则跳过校验
        secrets[secret_key] = v
        count += 1

    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    if count == 0:
        raise HTTPException(status_code=422, detail="未识别到有效字段（token/refreshToken 等）")
    _cfg.save_secrets(secrets)
    _refresh_secrets()
    # 判定主 token 是否已配置（优先必填字段第一个）
    main_key = next(iter(sorted(required_keys)), f"{label.replace('-', '_')}_token")
    return {"ok": True, "label": label, "imported": count, "secretSet": bool(secrets.get(main_key))}


