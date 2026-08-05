#!/usr/bin/env bash
# crack_daily.sh — 破解网关统一每日任务入口（签到/领取奖励/刷新 token）
# 由 crontab 每天调用，是所有 crack 网关的单一调度入口。
#
# 约定与扩展方式见 crack_daily.py 的 docstring（新增网关只需注册 DAILY_HANDLERS，
# 不要新增其他 cron 条目）。
#
# 退出码：0=全部成功或跳过；非 0=至少一个网关最终失败（crontab 侧据此告警）。
set -u
cd "$(dirname "$0")/../.."
PY=".venv/bin/python"

# timeout 300：防止某网关上游卡死拖垮整个 daily 任务（签到/刷新都是轻量 HTTP，
# 5 分钟足够）。超时返回 124，同样触发 crontab 告警钩子。
# 日志写仓库内 logs/（非 /tmp：容器重启清空，且代理 PrivateTmp=true 读不到）。
exec timeout 300 "$PY" crack_daily.py --secrets secrets.json --log logs/crack_daily.log
