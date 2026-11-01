#!/usr/bin/env bash
# 跑全部测试
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" -m unittest discover -s tests -t . -v
