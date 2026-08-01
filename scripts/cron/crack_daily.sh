#!/usr/bin/env bash
# crack_daily.sh — 破解网关统一每日任务入口（签到/领取奖励/刷新 token）
# 由 crontab 每天调用，是所有 crack 网关的单一调度入口。
set -u
cd "$(dirname "$0")/../.."
PY=".venv/bin/python"
exec "$PY" crack_daily.py --secrets secrets.json --log /tmp/crack_daily.log
