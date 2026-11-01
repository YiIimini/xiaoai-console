#!/usr/bin/env bash
# 停止工作台
set -uo pipefail
cd "$(dirname "$0")"
PORT="${XIAOAI_PORT:-8799}"
[ -s data/port ] && PORT="$(cat data/port)"

kill_pid() {
  local pid="$1"
  [ -z "$pid" ] && return 1
  kill "$pid" 2>/dev/null || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

if [ -f data/server.pid ] && kill_pid "$(cat data/server.pid)"; then
  echo "[xiaoai] 已停止 (pid $(cat data/server.pid))"
  rm -f data/server.pid data/port
  exit 0
fi

# 没有 pid 文件（例如手动启动过），按端口反查
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
elif command -v ss >/dev/null 2>&1; then
  PIDS="$(ss -lptnH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | tr '\n' ' ')"
else
  PIDS=""
fi

if [ -n "${PIDS// /}" ]; then
  for p in $PIDS; do kill_pid "$p" && echo "[xiaoai] 已停止占用 $PORT 的进程 (pid $p)"; done
  rm -f data/port
else
  echo "[xiaoai] 没有在跑"
fi
