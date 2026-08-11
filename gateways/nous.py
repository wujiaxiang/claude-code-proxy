"""Nous Portal 网关凭据同步（crack 类）。

凭据来源：hermes 容器（nousresearch/hermes-agent）的 auth.json，
宿主机挂载路径 /data/docker/hermes/data/auth.json（容器内 /opt/data/auth.json）。

职责划分（刻意设计）：
- hermes 容器负责 OAuth token 完整生命周期（登录 / 刷新 / 回写 auth.json）——
  auth.json 的**唯一写入者**是 hermes 进程（hermes 用户）。
- 代理只做"只读同步"：把 auth.json 里最新的 access_token 拷贝到 secrets.json
  （nous_access_token 字段），请求时由 crack 类通用注入逻辑
  （_handler_prepare_headers / _resolve_auth）自动加 Authorization: Bearer。

同步策略（只读，代理永不触发刷新）：
1. 每 60s 只读 auth.json；
2. access_token 剩余寿命 < REFRESH_MARGIN_SECONDS（10 分钟）、已过期或缺失时，
   仅告警（日志）——刷新/重新登录完全由 hermes 自身生命周期负责；
3. token 变化才写 secrets.json（避免无谓热重载）。

设计铁律（2026-08 踩坑教训，勿改回"触发刷新"）：
- Nous Portal 的 refresh_token 是**单次使用**的，只有 hermes 进程可以调刷新端点。
  外部进程触发刷新若不持久化旋转后的新 refresh_token，必然触发
  refresh-token reuse → 被 Nous Portal revoke 整个 session（本次事故根因，
  last_auth_error 原文已明示 "only Hermes may call the refresh endpoint"）。
- docker exec 默认以容器 root 执行，任何经 docker exec 的回写都会把 auth.json
  属主改成 root:root，hermes 主进程（hermes 用户）随即失去写权限无法刷 token。
- 因此 auth.json 的唯一写入者必须是 hermes 进程；代理只读同步，跨容器零写操作。

注：代理进程跑在宿主机 systemd 命名空间，可直接读 /data 挂载路径
（/tmp 之类隔离路径不可用，勿改路径）。
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("gateway.nous")

NOUS_AUTH_FILE = "/data/docker/hermes/data/auth.json"
REFRESH_MARGIN_SECONDS = 600   # token 剩余 < 10 分钟 → 仅告警（刷新归 hermes）
SYNC_INTERVAL_SECONDS = 60     # 同步周期


def _read_auth_state() -> dict:
    """读 hermes auth.json 的 providers.nous state；失败返回 {}。"""
    try:
        with open(NOUS_AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("providers") or {}).get("nous") or {}
    except FileNotFoundError:
        logger.warning(f"nous: auth.json 不存在（{NOUS_AUTH_FILE}，hermes 容器未挂载?）")
        return {}
    except Exception as e:
        logger.warning(f"nous: 读 auth.json 失败: {e}")
        return {}


def _ttl_seconds(state: dict) -> float:
    """access_token 剩余寿命（秒）；无法解析返回 -1。"""
    exp = state.get("expires_at") or ""
    try:
        exp_dt = datetime.fromisoformat(exp)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        return (exp_dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return -1.0


def sync_nous_token() -> bool:
    """只读同步一次：读 auth.json → 更新 secrets.json。不触发任何刷新。

    返回 True 表示 secrets 中已写入可用 token；False 表示无可用 token。
    """
    from config_store import load_secrets, save_secrets  # 延迟导入，避免启动期循环

    state = _read_auth_state()
    token = state.get("access_token") or ""
    if not token:
        logger.warning("nous: auth.json 无 access_token（hermes 未登录或 session 已被 revoke，"
                       "需在 hermes 中重新登录 Nous Portal）")
        return False

    ttl = _ttl_seconds(state)
    if ttl < 0:
        logger.warning("nous: access_token 已过期或无法解析过期时间——刷新/登录由 hermes 负责，"
                       "secrets 暂不更新（保留最后有效 token）")
        return False
    if 0 <= ttl < REFRESH_MARGIN_SECONDS:
        logger.warning(f"nous: access_token 剩余 {int(ttl)}s，即将过期——刷新由 hermes 自身负责，"
                       f"代理不介入；若持续过期请检查 hermes 的刷新/登录状态")

    if state.get("last_auth_error"):
        logger.warning(f"nous: hermes 上次登录/刷新失败: {str(state['last_auth_error'])[:200]}")

    secrets = load_secrets()
    if secrets.get("nous_access_token") != token:
        secrets["nous_access_token"] = token
        secrets["nous_expires_at"] = state.get("expires_at") or ""
        save_secrets(secrets)
        logger.info("nous: secrets.json nous_access_token 已更新")
    return True


async def nous_sync_loop() -> None:
    """后台定时同步循环（lifespan 注册，60s 周期）。"""
    while True:
        try:
            sync_nous_token()
        except Exception as e:
            logger.warning(f"nous: 同步异常: {e}")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
