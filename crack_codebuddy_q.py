#!/usr/bin/env python3
"""crack_codebuddy_q.py — CodeBuddy 额度查询 + 签到（成长计划）模块。

数据来源（全部经本机 token 实测通过）：
- 额度：POST /billing/meter/get-user-resource  → 返回各资源包（credits）的容量/已用/剩余/周期
- 用量通知：POST /v2/billing/meter/get-dosage-notify → dosageNotifyCode（0=无告警，无明细字段）
- 请求级用量：POST /billing/meter/get-user-request-usage → 每次请求扣费明细
- 账号：GET /v2/accounts → uid / nickname
- 签到/成长计划（CodeBuddy 无独立"每日签到"按钮接口，打卡体系=成长计划）：
    GET  /v2/activity/growth/profile            → 成长等级/完成数
    GET  /v2/activity/growth/tasks              → 任务列表（reward_credit / accept_status）
    GET  /activity/growth/streak                → 连续打卡天数（7/14/28 天档位）
    GET  /activity/growth/energy                → 能量余额
    POST /activity/growth/tasks/accept          → 接受任务 {task_codes:[...]}
    POST /activity/growth/tasks/{code}/claim    → 领取任务奖励

endpoint：默认 https://copilot.tencent.com（本机 CLI endpoint），失败自动换 https://www.codebuddy.ai。
依赖：标准库 + httpx（trust_env=False）。
"""
from __future__ import annotations

import base64
import datetime
import json
from pathlib import Path

import httpx

# ── endpoint 候选（本机 local_storage 确认 CLI 用 copilot.tencent.com）──
BASE_URLS = ["https://copilot.tencent.com", "https://www.codebuddy.ai"]

# 复用本模块已确认可用的路径
API_ACCOUNTS = "/v2/accounts"
API_DOSAGE_NOTIFY = "/v2/billing/meter/get-dosage-notify"
API_USER_RESOURCE = "/billing/meter/get-user-resource"
API_USER_REQUEST_USAGE = "/billing/meter/get-user-request-usage"
API_GROWTH_PROFILE = "/v2/activity/growth/profile"
API_GROWTH_TASKS = "/v2/activity/growth/tasks"
API_GROWTH_TASKS_ACCEPT = "/activity/growth/tasks/accept"
API_GROWTH_STREAK = "/activity/growth/streak"
API_GROWTH_ENERGY = "/activity/growth/energy"


def _jwt_exp(token: str):
    """解析 JWT payload 的 exp，返回 datetime 或 None（参考 crack_common._jwt_exp）。"""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        exp = json.loads(base64.urlsafe_b64decode(p)).get("exp")
        return datetime.datetime.fromtimestamp(exp) if exp else None
    except Exception:
        return None


def _fmt(dt) -> str | None:
    """datetime -> 'YYYY-MM-DD HH:MM:SS'（与后端字段格式一致），None 保持 None。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _new_client() -> httpx.Client:
    return httpx.Client(trust_env=False, timeout=20.0)


def _call(method: str, path: str, token: str, body: dict | None = None,
          params: dict | None = None) -> tuple[str, dict, int]:
    """按 BASE_URLS 顺序尝试调用，返回 (base_url, json, http_status)。

    首 base 若连接失败/401/404/超时则换下一个；两套都失败抛异常。
    """
    last_err: Exception | None = None
    for base in BASE_URLS:
        try:
            headers = {"Authorization": f"Bearer {token}"}
            kwargs = {"params": params, "headers": headers}
            if body is not None:
                headers["Content-Type"] = "application/json"
                kwargs["json"] = body
            resp = getattr(_new_client(), method.lower())(base + path, **kwargs)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:200]}
            if resp.status_code in (401, 403, 404) or isinstance(data, dict) and data.get("code") in (401, 404, 12403):
                # 该 base 不认此 token（如 www.codebuddy.ai 对 IOA token 直接 401）
                last_err = RuntimeError(f"{base}{path} -> HTTP {resp.status_code}: {str(data)[:120]}")
                continue
            return base, data, resp.status_code
        except Exception as e:  # 网络/DNS/超时等
            last_err = e
            continue
    raise RuntimeError(f"all endpoints failed for {path}: {last_err}")


# ── 额度 ──
def codebuddy_quota(token: str) -> list:
    """CodeBuddy 资源包额度明细（credits）。

    响应结构：{data:{Response:{Data:{TotalCount, TotalDosage, Accounts:[...]}}}}
    每个 Account 字段：PackageName / CapacitySize(limit) / CapacityUsed(used)
        / CapacityRemain / CycleStartTime / CycleEndTime(expireAt) / CapacityUnit / ProductName。
    """
    _, data, _ = _call("POST", API_USER_RESOURCE, token, body={})
    if not data or data.get("code") != 0:
        return [{"error": str(data)[:200]}]
    accounts = (data.get("data", {}).get("Response", {}).get("Data", {})).get("Accounts", []) or []
    packs = []
    for a in accounts:
        used = a.get("CycleCapacityUsed") if a.get("CycleCapacityUsed") is not None else a.get("CapacityUsed")
        packs.append({
            "name": a.get("PackageName") or a.get("ProductName") or "积分包",
            "limit": a.get("CapacitySize"),
            "used": used,
            "remain": a.get("CapacityRemain"),
            "unit": a.get("CapacityUnit") or "credits",
            "cycleStart": a.get("CycleStartTime"),
            "expireAt": a.get("CycleEndTime"),
            "product": a.get("SubProductName") or a.get("ProductName"),
            "dealName": a.get("DealName"),
        })
    return packs


# ── 签到 / 成长计划 ──
def codebuddy_checkin(token: str) -> dict:
    """CodeBuddy 打卡/成长状态。

    CodeBuddy 无独立"每日签到"按钮式接口；积分靠每日自动发放 + 成长计划任务领取。
    '打卡' 体系 = 成长计划：连续天数 streak + 任务奖励（credit）。此处如实上报。
    """
    result = {"enabled": True, "checkedIn": False, "credits": None, "message": ""}
    errors = []

    # 成长 profile
    try:
        _, profile, _ = _call("GET", API_GROWTH_PROFILE, token)
        p = (profile or {}).get("data", {}) or {}
        result["level"] = p.get("level_name") or p.get("level")
        result["completedTasks"] = p.get("completed")
        result["totalTasks"] = p.get("total")
    except Exception as e:
        errors.append(f"profile: {e}")

    # streak（连续打卡）
    try:
        _, streak, _ = _call("GET", API_GROWTH_STREAK, token)
        s = ((streak or {}).get("data", {}) or {}).get("streak", {}) or {}
        result["streakDays"] = s.get("days")
        result["streakNextTier"] = s.get("next_tier")
        result["streakNextTierRemaining"] = s.get("next_tier_remaining")
    except Exception as e:
        errors.append(f"streak: {e}")

    # 能量余额 + 可领取任务奖励汇总
    try:
        _, energy, _ = _call("GET", API_GROWTH_ENERGY, token)
        en = (energy or {}).get("data", {}) or {}
        result["energy"] = en.get("balance")
        result["credits"] = en.get("balance")  # 能量余额作为 credits 字段（成长体系内等价激励）
    except Exception as e:
        errors.append(f"energy: {e}")

    # 任务列表（可领取积分）
    claimable = []
    try:
        _, tasks, _ = _call("GET", API_GROWTH_TASKS, token)
        for t in ((tasks or {}).get("data", {}) or {}).get("tasks", []) or []:
            if t.get("accept_status") in ("accepted", "in_progress", "completed") and t.get("has_reward"):
                claimable.append({"task_code": t.get("task_code"),
                                  "title": t.get("title"),
                                  "reward_credit": t.get("reward_credit"),
                                  "status": t.get("accept_status")})
        result["claimableTasks"] = claimable
        result["claimableCredit"] = sum((c.get("reward_credit") or 0) for c in claimable)
    except Exception as e:
        errors.append(f"tasks: {e}")

    if claimable:
        result["checkedIn"] = False  # 有可领取奖励 ≠ 已签到；如实标注
    result["message"] = ("CodeBuddy 无独立每日签到接口；积分由每日自动发放 + 成长计划任务领取构成，"
                         "可领取任务奖励见 claimableTasks。" + ("（部分接口异常：" + "; ".join(errors) + "）" if errors else ""))
    return result


# ── 领取任务奖励（签到动作，非 status 必需）──
def codebuddy_claim_task(token: str, task_code: str) -> dict:
    """领取成长计划某个任务的积分奖励。task_code 需先处于可领取状态。"""
    _, data, status = _call("POST", f"/activity/growth/tasks/{task_code}/claim", token, body={})
    return {"task_code": task_code, "http": status, "code": data.get("code"),
            "msg": data.get("msg"), "data": data.get("data")}


# ── 统一状态入口 ──
def codebuddy_status(token: str, refresh_token: str = "") -> dict:
    """CodeBuddy 完整状态：额度 + 签到 + token 有效期 + 账号信息。

    返回结构（与 crack_common.CRACK_STATUS_HANDLERS 兼容）：
      {
        "quota":   [ {"name", "limit", "used", "expireAt", ...} ],
        "checkin": {"enabled", "checkedIn", "credits", "message", ...},
        "refresh": {"tokenExpireAt", "refreshExpireAt"},
        "extra":   {"nickname", "uid", "requestUsageDays", ...}
      }
    """
    result = {"quota": [], "checkin": {}, "refresh": {}, "extra": {}}
    if not token:
        return result

    try:
        result["quota"] = codebuddy_quota(token)
    except Exception as e:
        result["quota"] = [{"error": str(e)[:200]}]

    try:
        result["checkin"] = codebuddy_checkin(token)
    except Exception as e:
        result["checkin"] = {"enabled": False, "checkedIn": False,
                             "message": f"CodeBuddy 签到状态查询失败: {str(e)[:200]}"}

    # token 有效期（JWT exp；本机 auth 文件另有 expiresAt=exp*1000 对齐）
    exp = _jwt_exp(token)
    result["refresh"] = {
        "tokenExpireAt": _fmt(exp),
        "refreshExpireAt": None,   # 刷新接口未实测（避免轮换 token 影响现有登录态）
    }

    # 账号信息
    try:
        _, acct, _ = _call("GET", API_ACCOUNTS, token)
        accts = ((acct or {}).get("data", {}) or {}).get("accounts", []) or []
        if accts:
            a = accts[0]
            result["extra"].update({
                "nickname": a.get("nickname"),
                "uid": a.get("uid"),
                "uin": a.get("uin"),
                "accountType": a.get("type"),
            })
        else:
            result["extra"]["accounts"] = []
    except Exception as e:
        result["extra"]["accountsError"] = str(e)[:200]

    # 用量通知状态（get-dosage-notify：dosageNotifyCode=0 表示无用量告警）
    try:
        _, dn, _ = _call("POST", API_DOSAGE_NOTIFY, token, body={})
        d = (dn or {}).get("data", {}) or {}
        result["extra"]["dosageNotifyCode"] = d.get("dosageNotifyCode")
    except Exception:
        pass

    return result


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent
    secrets_path = root / "secrets.json"
    if not secrets_path.exists():
        print("secrets.json not found:", secrets_path)
        sys.exit(1)
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    tk = secrets.get("codebuddy_token", "")
    rk = secrets.get("codebuddy_refresh_token", "")
    if not tk:
        print("codebuddy_token 未配置")
        sys.exit(1)
    print(f"token 前 8 位: {tk[:8]}... (len={len(tk)})")
    out = codebuddy_status(tk, rk)
    print(json.dumps(out, ensure_ascii=False, indent=2))
