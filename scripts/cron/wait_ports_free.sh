#!/bin/bash
# systemd ExecStartPre 钩子：等待 targets.json 里所有 listenPort 端口释放后再启动。
#
# 背景（2026-08-03 排查）：短时间内连续 `systemctl restart claude-code-proxy`
# 时，旧进程 SIGTERM 后端口未必立即释放（连接未完全关闭/内核延迟），新进程
# 启动 bind 时报 OSError: [Errno 98] address already in use，导致
# status=3/NOTIMPLEMENTED 崩溃循环（RestartSec=10 后再次撞上同样的竞态）。
#
# 本脚本在真正 ExecStart 前轮询检查所有目标端口，全部空闲才放行，从根本上
# 避免 bind 冲突，而不是被动等崩溃后重试。

set -u
REPO_DIR="/root/shared-workspace/claude-code-proxy"
TARGETS_JSON="$REPO_DIR/targets.json"
MAX_WAIT_SECONDS=30
POLL_INTERVAL=1

if [ ! -f "$TARGETS_JSON" ]; then
    exit 0  # 配置缺失时不阻塞启动，交给 server.py 自己报错
fi

# 用 python3（venv 内置）解析 targets.json 取所有 listenPort，避免依赖 jq
PORTS=$("$REPO_DIR/.venv/bin/python3" -c "
import json
with open('$TARGETS_JSON') as f:
    data = json.load(f)
targets = data if isinstance(data, list) else data.get('targets', [])
ports = sorted({str(t['listenPort']) for t in targets if 'listenPort' in t})
print(' '.join(ports))
" 2>/dev/null)

if [ -z "$PORTS" ]; then
    exit 0
fi

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_SECONDS" ]; do
    busy=""
    for port in $PORTS; do
        if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port "; then
            busy="$busy $port"
        fi
    done
    if [ -z "$busy" ]; then
        exit 0  # 全部端口空闲，放行启动
    fi
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
done

echo "wait_ports_free: 超时 ${MAX_WAIT_SECONDS}s 后端口仍被占用:$busy，继续启动（由 server.py 自行报错）" >&2
exit 0  # 不阻塞启动失败：超时也放行，避免服务永久无法启动
