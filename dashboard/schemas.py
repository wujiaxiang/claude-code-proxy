"""Dashboard Pydantic 请求体模型（从 routes.py 抽出，逻辑不变）。"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ModelsUpdate(BaseModel):
    models: Optional[List] = None
    modelDefaults: Optional[Dict] = None



class AggregateConfigUpdate(BaseModel):
    name: Optional[str] = None
    virtualModels: Optional[Dict[str, dict]] = None
    poolDefaults: Optional[Dict[str, object]] = None
    quotaErrorPatterns: Optional[List[str]] = None



class TargetUpdate(BaseModel):
    label: Optional[str] = None
    listenPort: Optional[int] = None
    category: Optional[str] = None
    handler: Optional[str] = None
    isFree: Optional[bool] = None
    enabled: Optional[bool] = None
    targetHost: Optional[str] = None
    targetPort: Optional[int] = None
    targetProtocol: Optional[str] = None
    routePrefix: Optional[str] = None
    models: Optional[List] = None
    crackTool: Optional[str] = None
    secretRef: Optional[str] = None
    apikeyEnv: Optional[str] = None



class SecretUpdate(BaseModel):
    value: str = ""



class SecretBulkUpdate(BaseModel):
    """批量导入私密数据（破解网关多字段：token/refreshToken/userId 等）。"""
    data: Dict[str, str] = Field(default_factory=dict)


