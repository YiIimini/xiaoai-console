"""环境自检：回答「换了台电脑能不能跑起来」。"""
from __future__ import annotations

import os
import platform
import socket
import sys
from typing import Any, Dict, List

from . import store

OK, WARN, BAD = "ok", "warn", "fail"


def _row(level: str, name: str, detail: str, fix: str = "") -> Dict[str, Any]:
    return {"level": level, "name": name, "detail": detail, "fix": fix}


def check(port: int = 0) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    cfg = store.load_config()

    # 1) Python
    v = sys.version_info
    if v >= (3, 9):
        rows.append(_row(OK, "Python 版本", "%d.%d.%d · %s" % (v[0], v[1], v[2], platform.machine())))
    else:
        rows.append(_row(BAD, "Python 版本", "当前 %d.%d，需要 3.9 以上" % (v[0], v[1]),
                         "安装 Python 3.10+ 后重新运行 ./start.sh"))

    # 2) 虚拟环境
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        rows.append(_row(OK, "虚拟环境", "已启用 " + os.path.basename(sys.prefix)))
    else:
        rows.append(_row(WARN, "虚拟环境", "正在用系统 Python 运行",
                         "用 ./start.sh 启动会自动使用 .venv，避免污染系统环境"))

    # 3) miservice
    try:
        import miservice  # noqa: F401
        from importlib.metadata import version

        try:
            ver = version("miservice")
        except Exception:
            ver = "未知版本"
        rows.append(_row(OK, "依赖 miservice", "已安装 " + ver))
    except Exception:
        rows.append(_row(WARN, "依赖 miservice", "未安装",
                         "联网后运行 ./start.sh 会自动安装；暂时只能用演示模式"))

    # 4) data 目录可写
    probe = os.path.join(store.DATA_DIR, ".write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        rows.append(_row(OK, "数据目录可写", store.DATA_DIR))
    except Exception as e:  # noqa: BLE001
        rows.append(_row(BAD, "数据目录可写", str(e), "检查目录权限，或用有写权限的位置重新解压"))

    # 5) 端口（正在服务的那个端口本身当然是「被占用」的，要单独判断）
    if port:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            rows.append(_row(OK, "服务端口", "%d 空闲" % port))
        except OSError:
            rows.append(_row(OK, "服务端口", "%d 正由本实例监听" % port))
        finally:
            s.close()

    # 6) 配置完整性
    has_user = bool((cfg["mi"].get("username") or "").strip())
    has_pass = bool((cfg["mi"].get("password") or "").strip())
    has_key = bool((cfg["llm"].get("api_key") or "").strip())
    if has_user and has_pass:
        rows.append(_row(OK, "小米账号", "已填写"))
    else:
        rows.append(_row(WARN, "小米账号", "未填写", "在「音箱接入」填账号密码，否则只能演示模式"))

    if has_key:
        rows.append(_row(OK, "大模型 Key", "已配置 · " + str(cfg["llm"].get("model"))))
    else:
        rows.append(_row(WARN, "大模型 Key", "未配置", "在「大模型」填 API Key，任何 OpenAI 兼容端点都行"))

    # 6.5) 米家接入
    miot_cfg = cfg.get("miot") or {}
    if not miot_cfg.get("enabled", True):
        rows.append(_row(OK, "米家接入", "已关闭"))
    else:
        demo = bool(miot_cfg.get("mock")) or bool(cfg["mi"].get("mock"))
        if demo:
            mode = "演示设备"
        else:
            try:
                import miservice  # noqa: F401

                mode = "真实设备"
            except Exception:
                mode = "真实设备（缺 miservice，读不到）"
        write = "可写" if miot_cfg.get("allow_write", True) else "只读"
        rows.append(_row(OK, "米家接入", "%s · %s" % (mode, write)))
        try:
            import json as _json
            import os as _os

            cache = _os.path.join(store.DATA_DIR, "miot-cache", "devices.json")
            if _os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as f:
                    n = len((_json.load(f) or {}).get("devices") or [])
                rows.append(_row(OK, "米家设备缓存", "%d 台（到「米家设备」刷新可更新）" % n))
            else:
                rows.append(_row(WARN, "米家设备缓存", "还没拉过设备列表",
                                 "到「米家设备」点「刷新设备」"))
        except Exception:
            pass

    # 7) token 缓存
    if not cfg["mi"].get("mock"):
        try:
            import miservice  # noqa: F401
        except Exception:
            rows.append(_row(BAD, "读取设备依赖", "未装 miservice，接不了真机",
                             "在当前 Python 里执行：python -m pip install miservice（或用 ./start.sh 启动）"))

    if cfg["mi"].get("mock") and has_user:
        rows.append(_row(WARN, "运行模式", "演示模式 + 账号已填写",
                         "演示模式不会连真机；接管音箱请在「音箱接入」取消勾选"))

    try:
        from .mi import in_cooldown

        blocked = in_cooldown()
        if blocked:
            rows.append(_row(BAD, "小米登录", blocked, "等风控解除后再点「测试登录」"))
    except Exception:
        pass

    tok = store.resolve(cfg["mi"].get("token_path") or "data/.mi.token")
    if os.path.exists(tok):
        rows.append(_row(OK, "登录缓存", "%s（换机后需重新登录）" % os.path.relpath(tok, store.ROOT)))
    else:
        rows.append(_row(WARN, "登录缓存", "本机还没有小米登录凭据",
                         "首次点「测试登录」时会要求验证码，通过后自动生成"))

    # 8) 仓库路径（换机最容易踩的坑）
    repo = store.resolve(cfg["agent"].get("repo") or "")
    if repo and os.path.isdir(repo):
        rows.append(_row(OK, "仓库路径", repo))
    elif repo:
        rows.append(_row(BAD, "仓库路径", "本机不存在：" + repo,
                         "换机后旧路径失效，点右侧「用本机默认路径」重设"))
    else:
        rows.append(_row(WARN, "仓库路径", "未设置", "在「Agent 与沙箱」指定一个 git 仓库根目录"))

    score = sum(1 for r in rows if r["level"] == OK)
    bad = sum(1 for r in rows if r["level"] == BAD)
    return {
        "ok": bad == 0,
        "rows": rows,
        "summary": {"pass": score, "warn": sum(1 for r in rows if r["level"] == WARN), "fail": bad},
        "machine": machine_info(),
    }


def machine_info() -> Dict[str, Any]:
    return {
        "host": platform.node(),
        "os": "%s %s" % (platform.system(), platform.release()),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "project": store.ROOT,
        "data": store.DATA_DIR,
        "venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    }
