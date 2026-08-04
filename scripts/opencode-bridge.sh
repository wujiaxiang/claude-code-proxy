#!/bin/bash

# =====================================================================
# 脚本名称: opencode-bridge
# 核心功能: tmux 持久化 opencode TUI —— 每次 SSH 进入都在后台运行，
#           终端关闭进程不停；随时可 attach 回同一个会话
#
# 用法:
#   opencode-bridge            确保后台运行并 attach（最常用）
#   opencode-bridge start      仅后台启动（不 attach，SSH 立即返回）
#   opencode-bridge attach     连接到已有会话（无会话则先启动）
#   opencode-bridge stop       停止后台会话
#   opencode-bridge restart    重启会话
#   opencode-bridge status     查看运行状态
#   opencode-bridge logs       查看 opencode 日志
#
# 特性:
#   - 启动 opencode 时带 -c（--continue），自动继续上次会话
#   - tmux 前缀键为默认 Ctrl+B；detach 用 Ctrl+B 再按 d（小写）
# =====================================================================

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SESSION_NAME="opencode-bridge"
OPENCODE_BIN="$(command -v opencode 2>/dev/null || echo /root/.opencode/bin/opencode)"
LOG_DIR="${OPENCODE_BRIDGE_LOG_DIR:-/tmp/opencode-bridge-logs}"

# 默认工作目录：执行本脚本时所在的目录，可用 OPENCODE_BRIDGE_DIR 覆盖
DEFAULT_DIR="${OPENCODE_BRIDGE_DIR:-$PWD}"
cd "$DEFAULT_DIR" 2>/dev/null || cd "${OPENCODE_BRIDGE_DIR:-$HOME}"

mkdir -p "$LOG_DIR"

# ── 子命令分发 ──
CMD="${1:-auto}"
case "$CMD" in
    auto|attach|start|stop|restart|status|logs|help)
        ;;
    *)
        echo -e "${RED}未知参数: $1${NC}"
        echo -e "用法: opencode-bridge [start|attach|stop|restart|status|logs|help]"
        exit 1
        ;;
esac

# ── 基础环境检查（仅首次/start 时提示） ──
check_env() {
    if ! command -v tmux &> /dev/null; then
        echo -e "${RED}错误: 未安装 tmux，请先: apt-get install -y tmux${NC}"
        exit 1
    fi
    if [ ! -x "$OPENCODE_BIN" ]; then
        echo -e "${RED}错误: 找不到 opencode: ${OPENCODE_BIN}${NC}"
        exit 1
    fi
}

is_running() {
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

# ── 会话级固定 tmux 前缀键（Ctrl+B，tmux 默认） ──
# 显式声明保证脚本自包含：无论全局 ~/.tmux.conf 把 prefix 改成什么，
# 本会话一律用 Ctrl+B。会话级 set-option 只影响本会话，不污染其他 tmux 会话。
ensure_prefix() {
    tmux set-option -t "$SESSION_NAME" prefix C-b 2>/dev/null
    tmux bind-key -T prefix C-b send-prefix 2>/dev/null
}

# ── 启动（后台，不阻塞） ──
do_start() {
    if is_running; then
        echo -e "🟢 opencode 已在后台运行 (会话: ${CYAN}${SESSION_NAME}${NC})"
        return 0
    fi
    echo -e "${YELLOW}正在后台启动 opencode...${NC}"
    # tmux 独立会话：SSH 断开（SIGHUP）不影响会话内进程
    tmux new-session -d -s "$SESSION_NAME" -x 220 -y 55 \
        "export PATH=\"$HOME/.opencode/bin:\$PATH\"; exec ${OPENCODE_BIN} -c 2>>${LOG_DIR}/opencode.log"
    ensure_prefix
    sleep 2
    if is_running; then
        echo -e "🟢 ${GREEN}opencode 已后台启动${NC} (会话: ${CYAN}${SESSION_NAME}${NC})"
        echo -e "   attach:  ${GREEN}opencode-bridge attach${NC}   或  ${GREEN}tmux attach -t ${SESSION_NAME}${NC}"
        echo -e "   detach:  ${CYAN}Ctrl+B d${NC}（回到 SSH，进程继续跑）"
    else
        echo -e "${RED}启动失败，查看日志: tail -f ${LOG_DIR}/opencode.log${NC}"
        exit 1
    fi
}

# ── attach（无会话则先启动） ──
do_attach() {
    do_start
    ensure_prefix
    exec tmux attach-session -t "$SESSION_NAME"
}

# ── auto：默认行为，确保运行并 attach ──
do_auto() {
    if is_running; then
        echo -e "🟢 发现已有后台会话，正在连接... (detach: ${CYAN}Ctrl+B d${NC})"
        ensure_prefix
        exec tmux attach-session -t "$SESSION_NAME"
    else
        do_start
        ensure_prefix
        exec tmux attach-session -t "$SESSION_NAME"
    fi
}

case "$CMD" in
    start)   check_env; do_start ;;
    attach)  check_env; do_attach ;;
    auto)    check_env; do_auto ;;
    stop)
        if is_running; then
            tmux kill-session -t "$SESSION_NAME"
            echo -e "🛑 ${YELLOW}已停止后台会话${NC}"
        else
            echo -e "ℹ️ 没有运行中的会话"
        fi
        ;;
    restart)
        if is_running; then
            tmux kill-session -t "$SESSION_NAME"
            echo -e "🛑 已停止旧会话，重启中..."
        fi
        check_env; do_start
        ;;
    status)
        if is_running; then
            echo -e "🟢 ${GREEN}运行中${NC}"
            echo -e "   会话: ${CYAN}${SESSION_NAME}${NC}  (pid: $(tmux list-sessions -F '#{session_name} #{pane_pid}' | awk -v s="$SESSION_NAME" '$1==s{print $2}'))"
            echo -e "   查看屏幕: ${GREEN}opencode-bridge attach${NC}"
        else
            echo -e "⚪ ${YELLOW}未运行${NC}"
            echo -e "   启动: ${GREEN}opencode-bridge start${NC}"
        fi
        ;;
    logs)
        tail -f "$LOG_DIR/opencode.log" 2>/dev/null || { echo "日志为空: $LOG_DIR/opencode.log"; exit 1; }
        ;;
    help)
        echo -e "${BLUE}============ opencode-bridge ============${NC}"
        echo -e "tmux 持久化 opencode TUI —— SSH 断开进程不停"
        echo -e ""
        echo -e "  ${GREEN}opencode-bridge${NC}           确保后台运行并 attach（最常用）"
        echo -e "  ${GREEN}opencode-bridge start${NC}     仅后台启动（SSH 立即返回）"
        echo -e "  ${GREEN}opencode-bridge attach${NC}    连接已有会话"
        echo -e "  ${GREEN}opencode-bridge stop${NC}      停止后台会话"
        echo -e "  ${GREEN}opencode-bridge restart${NC}   重启会话"
        echo -e "  ${GREEN}opencode-bridge status${NC}    查看状态"
        echo -e "  ${GREEN}opencode-bridge logs${NC}      实时日志"
        echo -e ""
        echo -e "常用 tmux 快捷键: detach=${CYAN}Ctrl+B d${NC}  新建窗口=${CYAN}Ctrl+B c${NC}  切换=${CYAN}Ctrl+B n${NC}"
        echo -e "${BLUE}==========================================${NC}"
        ;;
esac
