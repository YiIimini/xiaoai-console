#!/usr/bin/env bash
# 小爱同学 · 大模型工作台 —— 一键启动 / 一键安装
#
#   ./start.sh            启动（首次会自动建 venv 装依赖）
#   ./start.sh doctor     只做环境体检，不启动
#   ./start.sh stop       停止
#   XIAOAI_PORT=8899 ./start.sh   指定端口
set -euo pipefail
cd "$(dirname "$0")"

PORT="${XIAOAI_PORT:-8799}"
VENV=".venv"
PY="$VENV/bin/python"
ACTION="${1:-start}"

say() { printf "\033[36m[xiaoai]\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m[xiaoai]\033[0m %s\n" "$*"; }
warn(){ printf "\033[33m[xiaoai]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[xiaoai]\033[0m %s\n" "$*" >&2; }

# ---------------------------------------------------------------- 选 Python
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

BASE_PY="$(find_python || true)"
if [ -z "$BASE_PY" ]; then
  err "没找到 Python 3.9+。"
  err "  macOS:  brew install python@3.12   或去 https://www.python.org/downloads/ 下载"
  err "  Ubuntu: sudo apt install -y python3 python3-venv"
  exit 1
fi

# ---------------------------------------------------------------- 动作分发
if [ "$ACTION" = "stop" ]; then exec ./stop.sh; fi

# ---------------------------------------------------------------- 建 venv
if [ ! -x "$PY" ]; then
  say "首次运行，创建虚拟环境 .venv（$("$BASE_PY" -V 2>&1)）"
  if ! "$BASE_PY" -m venv "$VENV" 2>/dev/null; then
    err "创建虚拟环境失败。Linux 上通常需要 python3-venv 包："
    err "  sudo apt install -y python3-venv"
    exit 1
  fi
fi

# ---------------------------------------------------------------- 装依赖
install_deps() {
  local args="-q --disable-pip-version-check -r requirements.txt"
  if "$VENV/bin/pip" install $args 2>/dev/null; then return 0; fi
  warn "默认源失败，改用国内镜像重试…"
  for mirror in \
    "https://pypi.tuna.tsinghua.edu.cn/simple" \
    "https://mirrors.aliyun.com/pypi/simple" \
    "https://pypi.mirrors.ustc.edu.cn/simple"; do
    say "  → $mirror"
    if "$VENV/bin/pip" install $args -i "$mirror" --trusted-host "$(echo "$mirror" | awk -F/ '{print $3}')" 2>/dev/null; then
      ok "依赖安装成功（镜像：${mirror}）"
      return 0
    fi
  done
  return 1
}

if ! "$PY" -c "import miservice" >/dev/null 2>&1; then
  say "安装依赖 miservice …"
  if install_deps; then
    ok "依赖就绪"
  else
    warn "依赖装不上（多半是没网或被墙）"
    warn "工作台仍会启动，但只能用「演示模式」；接真机前请联网再跑一次 ./start.sh"
  fi
else
  say "依赖已就绪"
fi

mkdir -p data

# ---------------------------------------------------------------- 体检 / 启动
if [ "$ACTION" = "doctor" ]; then
  exec "$PY" -m app.doctor
fi

# 上次用的端口优先（服务可能因占用自动后移过）
[ -s data/port ] && PORT="$(cat data/port)"

ping_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:$1/api/ping" >/dev/null 2>&1
  else
    "$PY" - "$1" <<'EOF' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen("http://127.0.0.1:%s/api/ping" % sys.argv[1], timeout=2)
EOF
  fi
}

if ping_ok "$PORT"; then
  [ -s data/port ] || echo "$PORT" > data/port
  say "工作台已在运行（端口 ${PORT}），直接打开"
else
  say "启动工作台，端口 $PORT 起（被占用会自动后移）"
  rm -f data/port
  nohup "$PY" -m app.server --port "$PORT" >> data/server.out 2>&1 &
  echo $! > data/server.pid
  for _ in $(seq 1 60); do
    [ -s data/port ] && break
    sleep 0.25
  done
  if [ ! -s data/port ]; then
    err "启动失败，看 data/server.out："
    tail -20 data/server.out >&2 || true
    exit 1
  fi
fi
[ -s data/port ] && PORT="$(cat data/port)"

URL="http://127.0.0.1:$PORT"
ok "工作台地址: $URL"
say "  体检: ./start.sh doctor    停止: ./stop.sh    日志: data/console.log"
case "$(uname -s)" in
  Darwin) open "$URL" >/dev/null 2>&1 || true ;;
  Linux)  command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 || true ;;
esac
