#!/usr/bin/env bash
# 推送前自检：身份、远端、仓库是否存在，然后推送。
set -uo pipefail
cd "$(dirname "$0")"

OWNER="${GITHUB_OWNER:-YiIimini}"
REPO="${GITHUB_REPO:-xiaoai-console}"
REMOTE="git@github.com:${OWNER}/${REPO}.git"

say() { printf "\033[36m[push]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[push]\033[0m %s\n" "$*" >&2; }

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "$REMOTE"
  say "已添加远端 origin → $REMOTE"
fi

say "检查 GitHub 连通性…"
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
  err "SSH 认证失败。先确认 ~/.ssh/id_ed25519 已加到 GitHub。"
  exit 1
fi

say "推送 main → $REMOTE"
if git push -u origin main; then
  say "完成：https://github.com/${OWNER}/${REPO}"
  exit 0
fi

cat <<TIPS

────────────────────────────────────────────
远端仓库还不存在。用任意一种方式建一个空仓库（不要勾 README/gitignore）：

  A. 网页（最快）
     https://github.com/new?name=${REPO}&visibility=public
     仓库名填 ${REPO}，可见性选 Public，其余留空 → Create repository
     建好后重新运行：./push.sh

  B. 命令行（需要 gh 且已 gh auth login）
     gh repo create ${REPO} --public --source=. --remote=origin --push

────────────────────────────────────────────
TIPS
exit 1
