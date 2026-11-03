#!/usr/bin/env bash
# 打包成可搬运的 zip。
#
#   ./pack.sh                 只打代码（不含任何账号信息）
#   ./pack.sh --with-config   连配置一起打（含小米账号密码、大模型 Key）
#   ./pack.sh --all           代码 + 配置 + 宿主挂载用到的全部文件
#
# 产物在 dist/，解压后在新电脑上跑 ./start.sh 即可。
set -euo pipefail
cd "$(dirname "$0")"

WITH_CONFIG=0
NAME="xiaoai-console"
for arg in "$@"; do
  case "$arg" in
    --with-config|--all) WITH_CONFIG=1 ;;
    --help|-h) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

STAMP="$(date +%Y%m%d-%H%M)"
SUFFIX=""
[ "$WITH_CONFIG" = 1 ] && SUFFIX="-含配置"
OUT="dist/${NAME}-${STAMP}${SUFFIX}.zip"
mkdir -p dist

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DEST="$TMP/${NAME}"
mkdir -p "$DEST"

# ---------- 代码本体 ----------
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
        --exclude '*.pyc' --exclude 'dist' --exclude '.git' --exclude '.DS_Store' \
        ./ "$DEST/"
else
  ( cd . && tar cf - \
      --exclude .venv --exclude data --exclude __pycache__ \
      --exclude '*.pyc' --exclude dist --exclude .git --exclude .DS_Store . ) \
    | ( cd "$DEST" && tar xf - )
fi

# ---------- 配置（可选）----------
if [ "$WITH_CONFIG" = 1 ]; then
  mkdir -p "$DEST/data"
  if [ -f data/config.json ]; then
    # 去掉绑死在这台机器上的绝对路径：agent.repo 指向旧电脑的项目目录，
    # 带过去只会变成「仓库路径不存在」。删掉让它按新机器的默认值走。
    python3 - "$DEST/data/config.json" <<'PYEOF'
import json, sys
src = "data/config.json"
dst = sys.argv[1]
with open(src, encoding="utf-8") as f:
    cfg = json.load(f)
cfg.get("agent", {}).pop("repo", None)
cfg.get("mi", {}).pop("token_path", None)
with open(dst, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
    chmod 600 "$DEST/data/config.json"
  fi
  # 场景/记忆属于使用数据，一并带走更有用
  for f in scenes.json memory.json; do
    [ -f "data/$f" ] && cp "data/$f" "$DEST/data/$f"
  done
  # 明确不带：.mi.token 与 .mi.device —— 它们是本机登录凭据与设备指纹，
  # 带到新机器反而更容易被判异地/陌生设备
fi

# ---------- 迁移说明 ----------
cat > "$DEST/迁移到新电脑.txt" <<'TXT'
小爱音箱 · 大模型工作台 —— 换机说明
====================================

一、新电脑上要装什么
--------------------
只要求 Python 3.9 以上，其余依赖脚本自己装。

  macOS： 一般自带 python3；没有就 `brew install python@3.12`
  Windows：去 python.org 装，安装时勾选 "Add python.exe to PATH"
  Linux：  sudo apt install -y python3 python3-venv

二、第一次启动
--------------
  macOS / Linux：  ./start.sh
  Windows：        双击 start.bat

脚本会自动建虚拟环境、装 miservice（默认源失败会切国内镜像）、
挑一个空闲端口、然后打开浏览器。大约 20 秒。

  体检：  ./start.sh doctor
  停止：  ./stop.sh

三、配置要不要重填
------------------
取决于你拿到的是哪个包：

  · 文件名带「含配置」：解压后直接就有账号、Key、设备 DID，不用重填
  · 不带「含配置」：    打开页面按「音箱接入」的四步引导重新填一遍

特别注意：包里的 data/ 不含 .mi.token 和 .mi.device。
这是故意的——它们是本机登录凭据和设备指纹，带到新机器反而更容易
被小米判定为陌生设备。第一次点「测试登录」会收到一条验证码，填完即通。

四、如果你也把 LureDaily 带过来了
--------------------------------
在 LureDaily 目录下执行：

  /path/to/xiaoai-console/tools/install-luredaily.sh /path/to/LureDaily

它会把代码内置进 LureDaily/vendor/、装好挂载模块、改好路由和侧栏。
之后访问 http://127.0.0.1:8787/xiaoai 即可。

注意：同一台机器不要同时跑 standalone（默认 8799）和挂载版，
两边会轮询同一个小米账号，互相抢回答。

五、常见问题
------------
· 端口被占用：脚本会自动后移，实际端口写在 data/port
· 依赖装不上：没网也能启动，但只用演示模式；联网后重跑 ./start.sh
· 小米登录报 20024：先看「迁移与环境 → 检测网络出口」，
  出口在境外会触发异地风控；工作台已强制小米域名直连
TXT

# ---------- 打 zip ----------
( cd "$TMP" && zip -qr - "${NAME}" ) > "$OUT"

SIZE="$(du -h "$OUT" | cut -f1)"
COUNT="$(zipinfo -1 "$OUT" 2>/dev/null | wc -l | tr -d ' ')"
echo "已打包: $OUT"
echo "  大小: $SIZE   文件数: $COUNT"
[ "$WITH_CONFIG" = 1 ] && echo "  ⚠ 内含明文账号密码与 API Key，别外传"
echo
echo "复制到新电脑后："
echo "  unzip $(basename "$OUT")"
echo "  cd ${NAME}"
echo "  ./start.sh          # Windows 双击 start.bat"
