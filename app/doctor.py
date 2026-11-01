"""命令行体检：./start.sh doctor，或在没有浏览器时排查环境。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import health, store  # noqa: E402

C = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "0": "\033[0m", "d": "\033[2m"}
LABEL = {"ok": "正常", "warn": "注意", "fail": "异常"}


def main() -> int:
    port = 0
    try:
        port = int(open(os.path.join(store.DATA_DIR, "port")).read().strip())
    except Exception:
        pass

    res = health.check(port)
    m = res["machine"]
    print("\n%s小爱同学 · 大模型工作台 —— 环境体检%s" % (C["0"], C["0"]))
    print("%s主机 %s · %s · Python %s · 虚拟环境%s%s\n" % (
        C["d"], m["host"], m["os"], m["python"], "已启用" if m["venv"] else "未启用", C["0"]))

    for r in res["rows"]:
        color = C.get(r["level"], "")
        print("%s[%s]%s %-12s %s" % (color, LABEL.get(r["level"], "?"), C["0"], r["name"], r["detail"]))
        if r.get("fix"):
            print("        %s→ %s%s" % (C["d"], r["fix"], C["0"]))

    s = res["summary"]
    color = C["fail"] if s["fail"] else (C["warn"] if s["warn"] else C["ok"])
    print("\n%s通过 %d · 注意 %d · 异常 %d%s" % (color, s["pass"], s["warn"], s["fail"], C["0"]))
    if s["fail"]:
        print("先把「异常」项处理掉再启动。")
        return 1
    print("可以直接 ./start.sh 启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
