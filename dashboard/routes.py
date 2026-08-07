"""Dashboard 管理面板：CSS/HTML 渲染 + 全部 /api/* REST 路由。

从 server.py 原样搬迁（Todo 7 模块拆分），逻辑逐字节不变，仅把
`@app.<method>` 装饰器换成 `@dashboard_router.<method>`，由 server.py
通过 `app.include_router(dashboard_router)` 挂载，路径/方法/响应完全一致。

依赖方向：dashboard → server（框架层），不反向。以下符号按分层约定
保留在 server.py，此处 import 取用：
  _fetch_live_models / ModelRegistry / _scan_dangling_refs /
  _humanize_model_name / catch_all / Colors / log_request_beautifully

对 server 的两类引用方式（务必区分，改动时勿混用）：
  1. **热重载可变全局** —— _TARGETS / _SECRETS / _MODELS_CFG /
     _MODEL_REGISTRY / _AGGREGATOR_ENGINE 会在 _load_vendor_targets /
     _reload_targets / _refresh_secrets 里被 `global` 重新赋值。
     一律写 `_srv.<NAME>` 属性访问，每次读到的都是当前对象；若写成
     `from server import X`，绑定的是 import 时的旧快照，热重载后
     dashboard 会一直显示旧配置。
  2. **稳定符号**（函数 / 就地 mutate 的统计字典 / 模块）—— 直接
     from server import，绑定不会失效。

server.py 侧在模块尾部（catch_all 之前）才 `from dashboard.routes import
dashboard_router`，此时 server 的定义已全部就绪，故无循环导入问题。
"""

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

dashboard_router = APIRouter()


# ─── 子模块装配（业务已拆分到 dashboard/* 子模块）────────────────────────────
# 以下 import 触发各子模块的 @dashboard_router.<method> 装饰，自动注册路由。
# server.py 仍通过 `from dashboard.routes import dashboard_router` 挂载本 router，
# 调用点零改动。
from dashboard import (
    api_targets,
    api_models,
    api_aggregate,
    api_secrets,
    api_crack,
    api_config,
    frontend,
)

__all__ = ["dashboard_router"]
