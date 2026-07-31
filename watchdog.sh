#!/bin/bash
# Watchdog for claude-code-proxy + CCR gateway + OMO Config Editor
# Checks HTTP health every 10 minutes; restarts unresponsive services

SERVICES=(
  "claude-code-proxy:http://127.0.0.1:8082/v1/models"
  "ccr-gateway:http://127.0.0.1:3457/health"
  "omo-config-editor:http://127.0.0.1:34560"
)
LOG=/root/shared-workspace/claude-code-proxy/watchdog.log
INTERVAL=600  # 10分钟一次（原60s过于频繁，曾因连续健康检查失败触发高频 systemctl restart ccr-gateway，
              # 间接导致 CCR 内部配置自动持久化逻辑把 SQLite 配置清空，详见 2026-07-29 事故记录）

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

STARTUP_DELAY=45

log "Watchdog started, waiting ${STARTUP_DELAY}s for all services to boot..."
sleep "$STARTUP_DELAY"
log "Startup delay elapsed, entering monitoring loop."

while true; do
  for entry in "${SERVICES[@]}"; do
    SERVICE="${entry%%:*}"
    URL="${entry#*:}"

    if ! curl -sf --max-time 5 "$URL" > /dev/null 2>&1; then
      log "[$SERVICE] Health check FAILED ($URL), restarting..."
      systemctl restart "$SERVICE" 2>&1 >> "$LOG"
      log "[$SERVICE] Restart exit code: $?"
      # 给刚重启的服务 20s 启动时间，避免循环重启
      sleep 20
    fi
  done
  sleep "$INTERVAL"
done
