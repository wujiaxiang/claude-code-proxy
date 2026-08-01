"""crack_qclaw_q.py — QClaw 积分额度查询模块（独立脚本，只读 secrets.json）。

逆向自 QClaw v0.2.35.624 客户端（app.asar）与 jprx 业务网关实测：

- 积分网关: https://jprx.m.qq.com/data/<cmd>/forward（POST，JSON）
- 认证头来自 app-store.json 解密的 secure.userInfo / secure.jwtToken：
  - X-Guid / X-Account / X-Qclaw-DeviceToken：userInfo.guid / userInfo.userId / device-id
  - X-Token：userInfo.loginKey（新版 QClaw 已无 loginKey 字段 → 空串）
  - X-OpenClaw-Token：secure.jwtToken（JWT，HS256，30 天有效）
- body 需带 `web_version` / `web_env`（客户端 commonFetch 自动追加）

用法:
  python crack_qclaw_q.py                # 从 secrets.json 读 qclaw_* 字段查询并打印
  python crack_qclaw_q.py --secrets x.json
  python -c "from crack_qclaw_q import qclaw_status; print(qclaw_status({...}))"

仅依赖标准库 + httpx（venv 已有）。所有 httpx 客户端 trust_env=False（绕过系统代理）。
"""
import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

JPRX_BASE = "https://jprx.m.qq.com"
WEB_VERSION = "1.4.0"   # 客户端 API_VERSION
WEB_ENV = "release"     # getWebEnv()


def _jwt_exp(openclaw_token: str):
    """解析 JWT payload 的 exp，返回 ISO 时间字符串；失败返回 None。"""
    try:
        payload_b64 = openclaw_token.split(".")[1]
        pad = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        exp = payload.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _build_headers(secrets: dict) -> dict:
    """构造 jprx 网关通用请求头（对齐客户端 getCommonHeaders）。"""
    return {
        "Content-Type": "application/json",
        "X-Version": "1",
        "X-Token": secrets.get("qclaw_login_key", "") or "",
        "X-Guid": secrets.get("qclaw_guid", "") or "1",
        "X-Account": str(secrets.get("qclaw_user_id", "") or "1"),
        "X-Session": "",
        "X-OpenClaw-Token": secrets.get("qclaw_openclaw_token", "") or "",
        "X-Qclaw-DeviceToken": secrets.get("qclaw_device_token", "") or "",
    }


def _post(path: str, headers: dict, body: dict):
    """POST jprx 端点，返回 (http_ok, json_obj_or_text, resp)。"""
    import httpx
    payload = {**body, "web_version": WEB_VERSION, "web_env": WEB_ENV}
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(f"{JPRX_BASE}{path}", headers=headers, json=payload)
    try:
        return resp.status_code == 200, resp.json(), resp
    except Exception:
        return False, resp.text, resp


def _unwrap(data):
    """兼容 4110/4075 的 `data.resp.data` 与 4222 的 `resp.data` 两种响应壳。"""
    if isinstance(data, dict):
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("resp"), dict):
            return data["data"]["resp"].get("data")
        if isinstance(data.get("resp"), dict):
            return data["resp"].get("data")
    return None


def qclaw_status(secrets: dict) -> dict:
    """查询 QClaw 积分额度。secrets 需含 qclaw_guid/qclaw_user_id/qclaw_openclaw_token 等 qclaw_* 字段。

    返回统一结构:
    {
      "quota":   [ {"name","limit","used","expireAt"}, ... ],
      "checkin": {"enabled": False, "checkedIn": False},
      "refresh": {"tokenExpireAt": ..., "refreshExpireAt": None},
      "extra":   {"userId": ..., "guid前8位": ...}
    }
    最小原则：仅配置 api_key 时 LLM 代理可用；积分查询缺业务字段则降级提示。
    """
    # 业务网关（jprx 积分查询）所需字段缺失时降级
    biz_fields = ("qclaw_openclaw_token", "qclaw_guid", "qclaw_user_id")
    biz_missing = [f for f in biz_fields if not secrets.get(f)]
    if biz_missing:
        return {
            "quota": [{"error": "未配置业务字段（%s），仅 LLM 代理可用；填齐后显示积分额度" % "、".join(biz_missing)}],
            "checkin": {"enabled": False, "checkedIn": False},
            "refresh": {"tokenExpireAt": _jwt_exp(secrets.get("qclaw_openclaw_token", "")),
                        "refreshExpireAt": None},
            "extra": {"nickname": secrets.get("qclaw_nickname", "") or "",
                      "userId": str(secrets.get("qclaw_user_id", "") or "")},
        }
    headers = _build_headers(secrets)
    quota = []
    errors = {}
    flow_total = None
    flow_latest = None

    # 1) 积分余额 data/4110/forward（getQPointAccount）
    ok, data, resp = _post("/data/4110/forward", headers, {})
    if ok and isinstance(data, dict) and data.get("ret") == 0:
        biz = _unwrap(data) or {}
        balance = float(biz.get("balance") or 0)
        granted = float(biz.get("total_daily_free_granted") or 0)
        detail = biz.get("balance_detail") or {}
        items = detail.get("items") or []
        expire_at = None
        if items and isinstance(items[0], dict):
            expire_at = items[0].get("expire_time")
        quota.append({
            "name": "积分余额",
            "limit": granted or balance,
            "used": max(0.0, (granted or balance) - balance),
            "expireAt": expire_at,
        })
    else:
        errors["4110积分余额"] = data if not isinstance(data, str) else data[:300]

    # 2) 今日剩余 token data/4075/forward（getTodayRemainingTokens）
    ok, data, resp = _post("/data/4075/forward", headers, {})
    if ok and isinstance(data, dict) and data.get("ret") == 0:
        biz = _unwrap(data) or {}
        quota.append({
            "name": "今日剩余token",
            "limit": biz.get("daily_token_limit"),
            "used": biz.get("daily_token_used"),
            "expireAt": None,  # 当日额度
        })
    else:
        errors["4075今日token"] = data if not isinstance(data, str) else data[:300]

    # 3) 积分流水 data/4222/forward（queryQPointFlow）
    ok, data, resp = _post("/data/4222/forward", headers, {"page": 1})
    if ok and isinstance(data, dict) and data.get("ret") == 0:
        biz = _unwrap(data) or {}
        flow_total = biz.get("total")
        flows = biz.get("flows") or []
        if flows:
            flow_latest = {
                "model_name": flows[0].get("model_name"),
                "amount": flows[0].get("amount"),
                "created_at": flows[0].get("created_at"),
            }
    else:
        errors["4222流水"] = data if not isinstance(data, str) else data[:300]

    guid = str(secrets.get("qclaw_guid", "") or "")
    return {
        "quota": quota,
        "checkin": {"enabled": False, "checkedIn": False},
        "refresh": {
            "tokenExpireAt": _jwt_exp(secrets.get("qclaw_openclaw_token", "")),
            "refreshExpireAt": None,
        },
        "extra": {
            "nickname": secrets.get("qclaw_nickname", "") or "",
            "userId": str(secrets.get("qclaw_user_id", "") or ""),
            "guid前8位": guid[:8],
            "flowTotal": flow_total,
            "flowLatest": flow_latest,
        },
        "errors": errors or None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="查询 QClaw 积分额度")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    args = parser.parse_args()

    try:
        secrets = json.loads(Path(args.secrets).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ 读取 {args.secrets} 失败: {e}")
        return 1

    if not secrets.get("qclaw_openclaw_token"):
        print("❌ secrets.json 缺少 qclaw_openclaw_token（先跑 177 机提取脚本回传合并）")
        return 1

    result = qclaw_status(secrets)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
