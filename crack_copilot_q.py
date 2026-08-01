"""crack_copilot_q.py — GitHub Copilot 剩余额度查询（企业版 GHE + 个人版）。

dashboard 通过 `GET /api/crack/{label}/status` 调用各破解网关的状态查询，
本模块实现 Copilot 的额度/签到/刷新状态，返回统一结构（与 crack_common.py
中 `trae_status` 保持一致）：
  {
    "quota":   [ {"name": ..., "limit": ..., "used": ..., "expireAt": ...}, ... ],
    "checkin": {"enabled": bool, "checkedIn": bool, "credits": ...},
    "refresh": {"tokenExpireAt": ..., "refreshExpireAt": ..., "boundDevice": ...},
    "extra":   {...}   # 各网关自定义
  }

两个模式（label 区分）：
  1. copilot-enterprise（8082）：企业版 GHE
     - 认证/用量端点：`GET https://api.{ghe-host}/copilot_internal/user`
       （默认 api.bmw.ghe.com，可用环境变量 COPILOT_GHE_API_HOST 覆盖）
     - 认证：secrets.json 的 copilot_token（github_pat_ 前缀 GHE PAT），
       Header `Authorization: token <github_pat_xxx>`
     - 企业 seat 通常 unlimited=true（chat/completions 无限额度）
  2. copilot（8083）：个人版 github.com
     - 用量端点：`GET https://api.github.com/copilot_internal/user`
     - 认证：secrets.json 的 copilot_personal_token（gho_ 前缀 OAuth token）
Copilot 无签到机制，checkin 固定 disabled；token 非 JWT，refresh 解析为 None。

依赖仅标准库（urllib.request），不引入第三方包。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

# 企业版 GHE 认证/用量端点（api 子域；inference 走 copilot-api 子域）
# 可由环境变量 COPILOT_GHE_API_HOST 覆盖（默认 api.bmw.ghe.com）
COPILOT_API_HOST = __import__("os").environ.get("COPILOT_GHE_API_HOST", "api.bmw.ghe.com")
COPILOT_API_USER = f"https://{COPILOT_API_HOST}/copilot_internal/user"
# 个人版 github.com 用量端点
COPILOT_PERSONAL_API_USER = "https://api.github.com/copilot_internal/user"
# 默认超时（秒）
_TIMEOUT = 20

# ── Copilot 固定请求头 ──
_COPILOT_HEADERS = {
    "Accept": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "GitHubCopilot/1.0.0",
}


def _copilot_headers(token: str) -> dict:
    """构造带认证的请求头（Authorization 用 `token` 前缀，实测兼容）。"""
    return {**_COPILOT_HEADERS, "Authorization": f"token {token}"}


def _get_json(url: str, token: str, timeout: int = _TIMEOUT) -> dict:
    """GET JSON（参考 crack_common.py 的 _post_json 风格，仅标准库）。

    企业版 GHE 是内网自签名证书，用未验证的 HTTPS context 请求。
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=_copilot_headers(token), method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_quota(data: dict) -> list:
    """把 quota_snapshots 映射为 dashboard 统一 quota 数组。

    企业版（enterprise seat）的 chat/completions 通常 unlimited=true、
    entitlement=0、remaining=0——此时 limit 显示为 ∞（unlimited），
    used 取 credits_used（若存在）否则 0。
    """
    snapshots = data.get("quota_snapshots", {}) or {}
    reset = data.get("quota_reset_date") or data.get("quota_reset_date_utc")
    rows = []
    for name, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        entitlement = snap.get("entitlement")
        remaining = snap.get("remaining")
        unlimited = bool(snap.get("unlimited", False))
        if unlimited:
            # 企业 seat 无限额度：limit 显示 ∞，used 取 credits_used 兜底
            used = snap.get("credits_used") or 0
            rows.append({
                "name": name,
                "limit": None,
                "used": used,
                "unlimited": True,
                "expireAt": reset,
            })
            continue
        if entitlement is not None and remaining is not None:
            used = entitlement - remaining
        else:
            used = remaining
        rows.append({
            "name": name,
            "limit": entitlement,
            "used": used,
            "unlimited": False,
            "expireAt": reset,
        })
    return rows


def copilot_status(token: str, refresh_token: str = "", personal: bool = False) -> dict:
    """GitHub Copilot 完整状态（额度 + 签到 + token 有效期）。

    personal=False（默认）：企业版 GHE（api.{ghe-host}/copilot_internal/user）
    personal=True：个人版 github.com（api.github.com/copilot_internal/user）
    返回统一结构（见模块 docstring）。任何异常被捕获，quota 置错误条目，
    保证 dashboard 调用不会抛错。
    """
    url = COPILOT_PERSONAL_API_USER if personal else COPILOT_API_USER
    result = {"quota": [], "checkin": {}, "refresh": {}, "extra": {}}
    if not token:
        return result
    try:
        data = _get_json(url, token)
        result["quota"] = _build_quota(data)
        result["extra"] = {
            "login": data.get("login"),
            "copilot_plan": data.get("copilot_plan"),
            "accessType": data.get("access_type_sku"),
            "quotaResetDate": data.get("quota_reset_date") or data.get("quota_reset_date_utc"),
            "orgs": [o.get("login") for o in (data.get("organization_list") or [])],
        }
    except Exception as e:
        result["quota"] = [{"error": str(e)[:200]}]
    # Copilot 无签到机制
    result["checkin"] = {"enabled": False, "checkedIn": False}
    # token 非 JWT，无法解析 exp；Copilot 无 refresh token
    result["refresh"] = {"tokenExpireAt": None, "refreshExpireAt": None}
    return result


def copilot_personal_status(token: str, refresh_token: str = "") -> dict:
    """个人版 Copilot 状态查询（api.github.com）。"""
    return copilot_status(token, refresh_token, personal=True)


def _load_secrets(path: str | None = None) -> dict:
    p = Path(path or (Path(__file__).parent / "secrets.json"))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    secrets = _load_secrets()
    token = secrets.get("copilot_token", "")
    if not token:
        print("secrets.json 无 copilot_token，无法查询")
        return 1

    status = copilot_status(token)
    print(f"来源: secrets.json（token 前缀 {token[:8]}...）")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    quota = status.get("quota") or []
    if quota and quota[0].get("error"):
        print(f"❌ 查询失败: {quota[0]['error']}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
