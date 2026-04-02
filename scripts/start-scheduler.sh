#!/bin/bash
# ============================================================
# 股票分析系统 - 定时任务启动脚本
# 运行模式：Web 服务 + 定时分析（合并模式）
# 每天 08:00 自动执行分析并推送报告
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$PROJECT_DIR/scheduler.pid"

# 检查虚拟环境
if [ ! -f "$VENV_PYTHON" ]; then
    echo "[ERROR] 虚拟环境未找到: $VENV_PYTHON"
    echo "请先执行: cd $PROJECT_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

mkdir -p "$LOG_DIR"

# 停止已有进程
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[INFO] 停止已有进程 PID=$OLD_PID ..."
        kill "$OLD_PID"
        sleep 2
    fi
    rm -f "$PID_FILE"
fi

LOG_FILE="$LOG_DIR/scheduler_$(date +%Y%m%d).log"

echo "[INFO] 启动股票分析调度器（合并模式：Web + 定时分析）"
echo "[INFO] 项目目录: $PROJECT_DIR"
echo "[INFO] 日志文件: $LOG_FILE"
echo "[INFO] 定时执行时间: 每天 08:00（由 .env SCHEDULE_TIME 控制）"

cd "$PROJECT_DIR"

# 合并模式：--serve（WebUI）+ --schedule（定时分析）
nohup "$VENV_PYTHON" main.py --serve --schedule \
    >> "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "[INFO] 启动成功，PID=$(cat $PID_FILE)"
echo "[INFO] 查看日志: tail -f $LOG_FILE"
