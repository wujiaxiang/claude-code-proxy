#!/usr/bin/env python3
"""crack_daily.py — 破解网关统一每日任务调度器（单一 cron 入口）。

每个网关插件化注册自己的每日任务（签到/领取奖励/刷新 token）：
  - 只有该网关在 secrets.json 里配了 key 才执行（无 key 跳过）
  - 不依赖本机客户端安装（token 已在 secrets.json）
  - 结果统一写入日志（--log 指定，默认 /tmp/crack_daily.log）

注册表说明（label → 任务函数）：
  trae-work  : 每日签到领积分 + access token 剩余<2天时刷新
  codebuddy  : 成长计划任务领取（claim 可领取奖励）+ token 到期前刷新
  qclaw      : 无签到（积分自动发放）；仅校验 jwt 有效期
  copilot    : 企业 seat 无限额度，无签到无刷新

用法:
  python crack_daily.py [--secrets secrets.json] [--log /tmp/crack_daily.log]
由 crontab 每天调用（单一任务，勿新增其他 cron）：
  0 3 * * * /root/shared-workspace/claude-code-proxy/scripts/cron/crack_daily.sh
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# 仓库根 = crack_daily.py 所在目录（脚本在仓库根，不是再上一层）
PROJECT_DIR = SCRIPT_DIR
sys.path.insert(0, str(PROJECT_DIR))


def _log(msg: str, out: list[str]) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    out.append(line)
    print(line)


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 网关任务实现（每个网关一个函数，签名统一 daily(secrets, out) -> dict）──

def daily_traework(secrets: dict, out: list[str], secrets_path: Path | None = None) -> dict:
    """Trae Work：每日签到领积分 + access token 剩余 <2 天时刷新。"""
    import crack_traework
    result = {"checkin": None, "refresh": None}
    token = secrets.get("trae_work_token", "")
    if not token:
        _log("  ⏭️  trae-work: 无 trae_work_token，跳过", out)
        return result
    auth = {"token": token,
            "refreshToken": secrets.get("trae_work_refresh_token", ""),
            "userId": secrets.get("trae_work_user_id", "")}
    try:
        st = crack_traework.checkin_status(auth)
        checked = st.get("checked_in", False)
        if not checked:
            r = crack_traework.checkin_claim(auth)
            ok = r.get("code") == 0 or r.get("success") or not r.get("code")
            result["checkin"] = {"claimed": ok, "resp": str(r)[:200]}
            _log(f"  ✅ trae-work: 执行签到 claim → {str(r)[:120]}" if ok else f"  ⚠️  trae-work: 签到失败 {str(r)[:120]}", out)
        else:
            result["checkin"] = {"claimed": False, "already": True}
            _log("  ✅ trae-work: 今日已签到", out)
    except Exception as e:
        result["checkin"] = {"error": str(e)[:200]}
        _log(f"  ❌ trae-work: 签到异常 {e}", out)
    # 刷新逻辑：JWT exp 剩余 < 2 天
    try:
        import base64
        exp = None
        try:
            p = token.split(".")[1]
            p += "=" * (-len(p) % 4)
            exp = json.loads(base64.urlsafe_b64decode(p)).get("exp")
        except Exception:
            pass
        if exp:
            remain_days = (exp - time.time()) / 86400
            if remain_days < 2:
                import tempfile
                # 复用 crack_traework.cmd_refresh（CLI 命令，签名: cmd_refresh(secrets_path, auth)）
                if hasattr(crack_traework, "cmd_refresh"):
                    tmp = Path(tempfile.gettempdir()) / "crack_daily_tmp_secrets.json"
                    tmp.write_text(json.dumps(secrets, ensure_ascii=False), encoding="utf-8")
                    rc = crack_traework.cmd_refresh(tmp, auth)
                    _log(f"  🔄 trae-work: token 剩 {remain_days:.1f} 天，执行刷新 rc={rc}", out)
                    result["refresh"] = {"done": True, "rc": rc}
                    if rc == 0:
                        refreshed = json.loads(tmp.read_text(encoding="utf-8"))
                        if refreshed.get("trae_work_token") != token:
                            _log("  ✅ trae-work: 刷新成功，token 已更新", out)
                            target = Path(secrets_path) if secrets_path else PROJECT_DIR / "secrets.json"
                            with open(target, "w", encoding="utf-8") as f:
                                json.dump(refreshed, f, ensure_ascii=False, indent=2)
                else:
                    _log("  ⚠️  trae-work: 无 cmd_refresh 函数，跳过刷新", out)
            else:
                _log(f"  ✅ trae-work: token 剩 {remain_days:.1f} 天，无需刷新", out)
        else:
            _log("  ⚠️  trae-work: 无法解析 token exp，跳过刷新", out)
    except Exception as e:
        result["refresh"] = {"error": str(e)[:200]}
        _log(f"  ❌ trae-work: 刷新异常 {e}", out)
    return result


def daily_codebuddy(secrets: dict, out: list[str], secrets_path: Path | None = None) -> dict:
    """CodeBuddy：成长计划可领取任务奖励（每日活跃等效）+ token 到期前刷新。"""
    result = {"claim": None, "refresh": None}
    token = secrets.get("codebuddy_token", "")
    if not token:
        _log("  ⏭️  codebuddy: 无 codebuddy_token，跳过", out)
        return result
    # ── token 刷新：JWT exp 剩余 < 30 天时用 refreshToken 换新（refreshToken 会轮换，需回写）──
    try:
        import base64
        exp = None
        try:
            p = token.split(".")[1]
            p += "=" * (-len(p) % 4)
            exp = json.loads(base64.urlsafe_b64decode(p)).get("exp")
        except Exception:
            pass
        rt = secrets.get("codebuddy_refresh_token", "")
        if exp and rt:
            remain_days = (exp - time.time()) / 86400
            if remain_days < 30:
                ok = _codebuddy_refresh(secrets, out)
                if ok:
                    result["refresh"] = {"done": True}
                    # 写回 secrets.json（refreshToken 轮换必须持久化）
                    target = Path(secrets_path) if secrets_path else PROJECT_DIR / "secrets.json"
                    try:
                        with open(target, "w", encoding="utf-8") as f:
                            json.dump(secrets, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        _log(f"  ⚠️  codebuddy: secrets 写回失败 {e}", out)
            else:
                _log(f"  ✅ codebuddy: token 剩 {remain_days:.0f} 天，无需刷新", out)
        elif not rt:
            _log("  ⚠️  codebuddy: 无 codebuddy_refresh_token，无法自动刷新", out)
    except Exception as e:
        result["refresh"] = {"error": str(e)[:200]}
        _log(f"  ❌ codebuddy: 刷新逻辑异常 {e}", out)
    # ── 成长计划任务领取 ──
    try:
        import crack_codebuddy_q as cb
        st = cb.codebuddy_checkin(token)
        claimable = st.get("claimableTasks") or []
        claimed = []
        for task in claimable:
            r = cb.codebuddy_claim_task(token, task.get("task_code"))
            claimed.append({"task": task.get("task_code"), "code": r.get("code"), "msg": str(r.get("msg"))[:60]})
            _log(f"  ✅ codebuddy: 领取任务 {task.get('task_code')} → {str(r.get('msg'))[:60]}", out)
        result["claim"] = {"claimed": len(claimed), "detail": claimed}
        if not claimed:
            _log("  ✅ codebuddy: 无待领取任务（成长计划）", out)
    except Exception as e:
        result["claim"] = {"error": str(e)[:200]}
        _log(f"  ❌ codebuddy: 成长任务领取异常 {e}", out)
    return result


def _codebuddy_refresh(secrets: dict, out: list[str]) -> bool:
    """调用 codebuddy refresh 端点换新 token（refreshToken 会轮换）。

    端点（已逆向实测 200）：POST https://copilot.tencent.com/v2/plugin/auth/token/refresh
    关键：prefixPath=/plugin（非空），X-Domain 头必须带。
    """
    import httpx
    url = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
    headers = {
        "X-Domain": "copilot.tencent.com",
        "X-Refresh-Token": secrets.get("codebuddy_refresh_token", ""),
        "X-Auth-Refresh-Source": "plugin",
        "Authorization": f"Bearer {secrets.get('codebuddy_token', '')}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(trust_env=False, timeout=20.0) as client:
            resp = client.post(url, json={}, headers=headers)
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 0 and data.get("data", {}).get("accessToken"):
                nd = data["data"]
                secrets["codebuddy_token"] = nd["accessToken"]
                if nd.get("refreshToken"):
                    secrets["codebuddy_refresh_token"] = nd["refreshToken"]
                _log(f"  🔄 codebuddy: 刷新成功（新 accessToken {nd['accessToken'][:10]}...，refreshToken 已轮换）", out)
                return True
            _log(f"  ❌ codebuddy: 刷新失败 HTTP {resp.status_code}: {str(data)[:150]}", out)
            return False
    except Exception as e:
        _log(f"  ❌ codebuddy: 刷新异常 {e}", out)
        return False


def daily_qclaw(secrets: dict, out: list[str]) -> dict:
    """QClaw：无独立签到（积分自动发放）；校验 jwt 有效期。"""
    result = {"status": None}
    jwt = secrets.get("qclaw_openclaw_token", "")
    if not (secrets.get("qclaw_api_key") or jwt):
        _log("  ⏭️  qclaw: 无 qclaw 认证，跳过", out)
        return result
    try:
        import crack_qclaw_q as qc
        st = qc.qclaw_status(secrets)
        exp = st.get("refresh", {}).get("tokenExpireAt")
        result["status"] = {"jwtExpireAt": exp}
        _log(f"  ✅ qclaw: jwt 有效期 {exp or '未知'}（无签到机制，积分自动发放）", out)
    except Exception as e:
        result["status"] = {"error": str(e)[:200]}
        _log(f"  ❌ qclaw: 状态查询异常 {e}", out)
    return result


def daily_copilot(secrets: dict, out: list[str], personal: bool = False) -> dict:
    """Copilot：确认 token 有效（企业版或个人版，无签到无刷新）。"""
    result = {"status": None}
    key = "copilot_personal_token" if personal else "copilot_token"
    token = secrets.get(key, "")
    if not token:
        _log(f"  ⏭️  copilot{'（个人版）' if personal else '（企业版）'}: 无 {key}，跳过", out)
        return result
    try:
        import crack_copilot_q as cp
        fn = cp.copilot_personal_status if personal else cp.copilot_status
        st = fn(token)
        result["status"] = {"quota": len(st.get("quota") or []),
                            "login": st.get("extra", {}).get("login")}
        _log(f"  ✅ copilot{'（个人版）' if personal else '（企业版）'}: token 有效，额度 {result['status']['quota']} 类", out)
    except Exception as e:
        result["status"] = {"error": str(e)[:200]}
        _log(f"  ❌ copilot{'（个人版）' if personal else '（企业版）'}: 状态查询异常 {e}", out)
    return result


# ── 注册表：label → (daily 函数, 所需 secrets key 之一) ──
DAILY_HANDLERS = {
    "trae-work": daily_traework,
    "codebuddy": daily_codebuddy,
    "qclaw":     daily_qclaw,
    "copilot-enterprise": lambda s, o: daily_copilot(s, o, personal=False),
    "copilot":   lambda s, o: daily_copilot(s, o, personal=True),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="破解网关统一每日任务")
    parser.add_argument("--secrets", default=str(PROJECT_DIR / "secrets.json"))
    parser.add_argument("--log", default="/tmp/crack_daily.log")
    parser.add_argument("--only", default="", help="只跑指定网关（逗号分隔，默认全部）")
    args = parser.parse_args()

    out: list[str] = []
    _log("=== 破解网关每日任务开始 ===", out)
    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else []

    for label, fn in DAILY_HANDLERS.items():
        if only and label not in only:
            continue
        _log(f"── {label} ──", out)
        try:
            if label in ("trae-work", "codebuddy"):
                fn(secrets, out, secrets_path)
            else:
                fn(secrets, out)
        except Exception as e:
            _log(f"  ❌ {label}: 任务异常 {e}", out)

    _log("=== 破解网关每日任务完成 ===", out)
    # 写最后运行时间戳（dashboard 状态区展示"最后定时刷新"）。
    # 放仓库内 .cache/（不用 /tmp：代理进程 mount namespace 隔离，读不到系统 /tmp）。
    try:
        last_run = PROJECT_DIR / ".cache" / "crack_daily_last_run"
        last_run.parent.mkdir(parents=True, exist_ok=True)
        last_run.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")
    except Exception:
        pass
    try:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"⚠️  写日志失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
