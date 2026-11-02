#!/usr/bin/env bash
# 把「小爱音箱工作台」挂进任意一台电脑上的 LureDaily 工作台（/xiaoai 子路径）。
#
# 用法：
#   ./tools/install-luredaily.sh /path/to/LureDaily
#   XIAOAI_MOUNT=vendor ./tools/install-luredaily.sh /path/to/LureDaily   # 强制内置一份代码
#
# 做的事：
#   1. 把本仓库代码复制到 <LureDaily>/vendor/xiaoai-console（自带一份，换机可用）
#   2. 安装桥接模块 dashboard/xiaoai_app.py
#   3. 写 dashboard/xiaoai.path（指向内置那份）
#   4. 幂等修改 dashboard/server.py：加 import + /xiaoai 的 GET/POST 分发
#   5. 幂等给 dashboard/index.html 侧栏加入口
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "用法: $0 /path/to/LureDaily" >&2; exit 1; }
[ -d "$TARGET/dashboard" ] || { echo "目标不像 LureDaily（缺 dashboard/）: $TARGET" >&2; exit 1; }
TARGET="$(cd "$TARGET" && pwd)"
DASH="$TARGET/dashboard"

say() { printf "\033[36m[install]\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m[install]\033[0m %s\n" "$*"; }

# ---------- 1. 内置代码 ----------
VENDOR="$TARGET/vendor/xiaoai-console"
say "复制代码到 $VENDOR"
mkdir -p "$TARGET/vendor"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
        --exclude '.venv' --exclude 'data' --exclude 'dist' --exclude '__pycache__' \
        --exclude '*.pyc' --exclude '.git' --exclude '.DS_Store' \
        "$SRC/" "$VENDOR/"
else
  rm -rf "$VENDOR"; mkdir -p "$VENDOR"
  ( cd "$SRC" && tar cf - --exclude .venv --exclude data --exclude dist \
      --exclude __pycache__ --exclude .git . ) | ( cd "$VENDOR" && tar xf - )
fi

# ---------- 2. 桥接模块 ----------
cp "$SRC/tools/xiaoai_app.py" "$DASH/xiaoai_app.py"
ok "已安装 dashboard/xiaoai_app.py"

# ---------- 3. 路径文件 ----------
echo "$VENDOR" > "$DASH/xiaoai.path"
ok "已写 dashboard/xiaoai.path → $VENDOR"

# ---------- 4. 改 server.py ----------
python3 - "$DASH/server.py" <<'PY'
import sys
from pathlib import Path

p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
changed = []

if "import xiaoai_app" not in s:
    for anchor in ("import teardown_bridge", "import vtuber_app", "from pathlib import Path"):
        if anchor in s:
            s = s.replace(anchor, anchor + "\nimport xiaoai_app", 1)
            changed.append("import")
            break

GET_ANCHOR = '''        if path == "/teardown" or path.startswith("/teardown/"):'''
if 'xiaoai_app.mount(self, path)' not in s.split("def do_POST")[0]:
    for anchor in (GET_ANCHOR, '''        if path in ("/collect", "/collect.html"):'''):
        if anchor in s:
            s = s.replace(anchor, '''        # ── 小爱音箱工作台（/xiaoai/）──────────────────────────────
        if path == "/xiaoai" or path.startswith("/xiaoai/"):
            if xiaoai_app.mount(self, path):
                return

''' + anchor, 1)
            changed.append("GET")
            break

POST_ANCHOR = '''        length = int(self.headers.get("Content-Length") or 0)'''
if 'xiaoai_app.mount(self, path)' not in s.split("def do_POST")[1][:2000]:
    if POST_ANCHOR in s:
        s = s.replace(POST_ANCHOR, '''        # ── 小爱音箱工作台：先于读体分发 ───────────────────────────
        if path.startswith("/xiaoai/"):
            if xiaoai_app.mount(self, path):
                return

''' + POST_ANCHOR, 1)
        changed.append("POST")

p.write_text(s, encoding="utf-8")
print("server.py 改动:", ", ".join(changed) if changed else "无（已是集成状态）")
PY

# ---------- 5. 侧栏入口 ----------
python3 - "$DASH/index.html" <<'PY'
import sys
from pathlib import Path

p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
if 'href="/xiaoai"' in s:
    print("侧栏入口: 已存在")
else:
    anchor = '''  <a class="rail-item accent" id="collectEntry" href="/collect"'''
    if anchor not in s:
        print("侧栏入口: 未找到导航锚点，跳过（可手动加）")
    else:
        s = s.replace(anchor, '''  <a class="rail-item" href="/xiaoai" title="小爱音箱工作台（语音接大模型、米家设备）" aria-label="小爱音箱工作台">
    <span class="ico" data-icon="settings" data-size="20"></span><span class="lbl"><span class="lbl-t">小爱音箱</span></span></a>
''' + anchor, 1)
        p.write_text(s, encoding="utf-8")
        print("侧栏入口: 已添加")
PY

ok "安装完成"
echo
echo "下一步："
echo "  1) 重启宿主工作台：cd $TARGET && ./.venv/bin/python dashboard/server.py --port 8787"
echo "  2) 打开 http://127.0.0.1:8787/xiaoai"
echo
echo "注意：同一台机器不要再同时跑 standalone 那份（默认 8799），"
echo "      否则两个进程会轮询同一个小米账号，互相抢回答。"
