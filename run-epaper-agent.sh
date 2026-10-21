#!/bin/zsh
# 启动局域网服务与三页定时更新器；按 Ctrl+C 会同时停止两者。
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/dashboard-runtime/run-real-pages.sh" || echo "WARN 初始刷新未全部完成；更新器会继续重试" >&2
"$ROOT/dashboard-runtime/run-dashboard-updater.sh" >> "$ROOT/dashboard-runtime/updater.log" 2>&1 &
UPDATER_PID=$!
trap 'kill "$UPDATER_PID" 2>/dev/null || true; exit 0' INT TERM EXIT
/usr/bin/python3 "$ROOT/epaper_agent.py" \
  --home "$ROOT/dashboard-runtime/home/out" \
  --stock "$ROOT/dashboard-runtime/stock/out" \
  --reading "$ROOT/dashboard-runtime/reading/out"
