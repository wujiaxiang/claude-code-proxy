#!/usr/bin/env bash
# trae_work_daily.sh — Trae Work 每日任务：签到 + 按需刷新 token
# 由 cron 每天调用。签到天天跑；refresh 仅在 access token 剩余 < 2 天时执行。
set -u
cd /root/shared-workspace/claude-code-proxy
PY=".venv/bin/python"
LOG="/tmp/trae_work_daily.log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

# 1) 每日签到（领 200 Work 积分）
"$PY" crack_traework.py --claim --secrets secrets.json >> "$LOG" 2>&1

# 2) access token 有效期检查：剩 < 2 天才刷新
TOKEN=$(python3 -c "import json;print(json.load(open('secrets.json')).get('trae_work_token',''))" 2>/dev/null)
if [ -n "$TOKEN" ]; then
    EXP=$(python3 -c "
import base64, json, time
try:
    p = '$TOKEN'.split('.')[1]
    p += '=' * (-len(p) % 4)
    exp = json.loads(base64.urlsafe_b64decode(p))['exp']
    remain_days = (exp - time.time()) / 86400
    print(f'{remain_days:.1f}')
except Exception:
    print('99')
" 2>/dev/null)
    if python3 -c "exit(0 if float('$EXP') < 2 else 1)" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] access token 剩余 ${EXP} 天，执行刷新" >> "$LOG"
        "$PY" crack_traework.py --refresh --secrets secrets.json >> "$LOG" 2>&1
    else
        echo "[$(date '+%H:%M:%S')] access token 剩余 ${EXP} 天，无需刷新" >> "$LOG"
    fi
fi

echo "=== 完成 ===" >> "$LOG"
