#!/usr/bin/env bash
# 打包成可搬运的 zip：去掉 .venv、data、__pycache__，保留代码和文档。
# 用法：./pack.sh  → 生成 dist/xiaoai-console-<时间>.zip
set -euo pipefail
cd "$(dirname "$0")"

NAME="xiaoai-console"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="dist/${NAME}-${STAMP}.zip"
mkdir -p dist

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
        --exclude '*.pyc' --exclude 'dist' --exclude '.git' --exclude '.DS_Store' \
        ./ "$TMP/${NAME}/"
  ( cd "$TMP" && zip -qr - "${NAME}" ) > "$OUT"
else
  # 没有 rsync 就直接让 zip 做排除
  zip -qr "$OUT" . \
      -x '.venv/*' 'data/*' '*/__pycache__/*' '*.pyc' 'dist/*' '.git/*' '*.DS_Store'
fi

SIZE="$(du -h "$OUT" | cut -f1)"
echo "已打包: $OUT ($SIZE)"
echo
echo "新电脑上："
echo "  1) 解压  unzip $(basename "$OUT")"
echo "  2) 进入  cd ${NAME}"
echo "  3) 启动  ./start.sh          （Windows 双击 start.bat）"
echo "  4) 首次会自动建虚拟环境、装依赖；配置和登录缓存不在包里，需要重新填或用「迁移与环境」导入。"
