#!/bin/bash
# 停止股票分析调度器

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/scheduler.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "[INFO] PID 文件不存在，尝试按进程名查找..."
    pkill -f "main.py --serve --schedule" 2>/dev/null && echo "[INFO] 已停止" || echo "[INFO] 未找到运行中的进程"
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "[INFO] 已停止进程 PID=$PID"
else
    echo "[INFO] 进程 PID=$PID 已不在运行"
fi
rm -f "$PID_FILE"
