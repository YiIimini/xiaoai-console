"""把小爱音箱工作台挂到宿主工作台的 `/xiaoai` 子路径下。

复用 standalone 那套 Handler，不复制任何路由逻辑：
把宿主连接里的 rfile/wfile/headers 交给一个新的 xiaoai Handler 实例，
改写路径前缀后直接调用它的 do_GET / do_POST。

这样两边永远同步——加一个接口只改 xiaoai-console 一处。

包定位顺序（从「打包带走」到「本机开发」）：
  1. 环境变量 XIAOAI_HOME
  2. dashboard/xiaoai.path 里写的一行绝对路径（安装脚本会写）
  3. dashboard/vendor/xiaoai-console（打包时内置进仓库的那份）
  4. 仓库根的 vendor/xiaoai-console
  5. 工作区同级目录里的 xiaoai-console（开发时常见摆放）

打包时 3 命中，换电脑直接可用；本机开发时 2 或 5 命中。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PREFIX = "/xiaoai"
DASH = Path(__file__).resolve().parent
ROOT = DASH.parent

_state: dict = {"home": None, "error": None, "handler": None}


PATH_FILE = DASH / "xiaoai.path"


def _from_path_file():
    try:
        raw = PATH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(raw).expanduser() if raw else None


def _candidates():
    env = os.environ.get("XIAOAI_HOME")
    if env:
        yield Path(env).expanduser()
    p = _from_path_file()
    if p is not None:
        yield p
    yield DASH / "vendor" / "xiaoai-console"
    yield ROOT / "vendor" / "xiaoai-console"
    yield ROOT.parent / "xiaoai-console"
    yield Path.home() / "xiaoai-console"


def home() -> Path | None:
    if _state["home"] is not None:
        return _state["home"]
    for c in _candidates():
        try:
            if (c / "app" / "server.py").is_file():
                _state["home"] = c.resolve()
                return _state["home"]
        except OSError:
            continue
    return None


def available() -> bool:
    return home() is not None


def error() -> str:
    if available():
        return ""
    tried = "、".join(str(c) for c in _candidates())
    return "找不到 xiaoai-console 代码目录。已尝试：%s" % tried


def handler_class():
    """导入并缓存 standalone 的 Handler 类。"""
    if _state["handler"] is not None:
        return _state["handler"]
    h = home()
    if h is None:
        raise RuntimeError(error())
    if str(h) not in sys.path:
        sys.path.insert(0, str(h))
    from app.server import Handler  # noqa: WPS433  延迟导入，避免宿主启动即加载

    _record_load_time()
    _state["handler"] = Handler
    return Handler


def mount(outer, path: str) -> bool:
    """在宿主 handler 上就地处理一个 /xiaoai 请求；返回是否已处理。"""
    if not (path == PREFIX or path.startswith(PREFIX + "/")):
        return False

    warn_if_standalone_running()
    warn_if_stale()

    try:
        Handler = handler_class()
    except Exception as e:  # noqa: BLE001
        outer.send_response(503)
        outer.send_header("Content-Type", "text/plain; charset=utf-8")
        body = ("小爱音箱工作台未就绪：%s\n" % e).encode("utf-8")
        outer.send_header("Content-Length", str(len(body)))
        outer.end_headers()
        outer.wfile.write(body)
        return True

    inner_path = path[len(PREFIX):] or "/"
    # 只允许走到工作台自己的路由，不允许借道跳出前缀
    if not inner_path.startswith("/"):
        inner_path = "/" + inner_path

    h = Handler.__new__(Handler)
    h.request = outer.request
    h.client_address = outer.client_address
    h.server = outer.server
    h.rfile = outer.rfile
    h.wfile = outer.wfile
    h.headers = outer.headers
    h.command = outer.command
    h.path = inner_path
    h.request_version = outer.request_version
    h.protocol_version = outer.protocol_version
    h.close_connection = True
    h.raw_requestline = b""
    # 绕过 BaseHTTPRequestHandler.__init__，这些内部字段必须自己补上，
    # 否则 send_response 里没有 _headers_buffer 会直接抛异常、连接被掐断
    h._headers_buffer = []          # type: ignore[attr-defined]
    h.requestline = "%s %s %s" % (outer.command, inner_path,
                                  getattr(outer, "request_version", "HTTP/1.1"))
    h.log_request = lambda *a, **k: None       # 静音，宿主已有自己的访问日志

    try:
        cmd = outer.command
        if cmd == "GET":
            h.do_GET()
        elif cmd == "POST":
            h.do_POST()
        elif cmd == "OPTIONS":
            h.do_OPTIONS()
        else:
            outer.send_response(405)
            outer.send_header("Content-Length", "0")
            outer.end_headers()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        try:
            if not h._headers_buffer:
                outer.send_response(500)
                outer.send_header("Content-Type", "text/plain; charset=utf-8")
                body = ("小爱音箱工作台内部错误：%s: %s\n" % (type(e).__name__, e)).encode("utf-8")
                outer.send_header("Content-Length", str(len(body)))
                outer.end_headers()
                outer.wfile.write(body)
        except Exception:
            pass
    finally:
        outer.close_connection = True
    return True


_warned = {"done": False}
_stale = {"checked": False}


def _module_mtime() -> float:
    """当前进程里那份 app/server.py 的真实修改时间。"""
    import sys as _sys

    mod = _sys.modules.get("app.server")
    path = getattr(mod, "__file__", None)
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def _record_load_time() -> None:
    global _LOADED_AT
    _LOADED_AT = _module_mtime()


_LOADED_AT = 0.0


def warn_if_stale() -> None:
    """宿主不重启就一直用内存里的旧模块，改完代码会以为「没生效」。"""
    if _stale["checked"]:
        return
    _stale["checked"] = True
    if not _LOADED_AT:
        return
    now = _module_mtime()
    if now > _LOADED_AT + 1:
        print("[xiaoai] 提示：小爱工作台代码在加载后被更新过，"
              "当前进程仍在用旧版本。重启宿主工作台后才会生效。", flush=True)


def warn_if_standalone_running() -> None:
    """同机双开会抢同一个小米账号，两边轮询、互相覆盖回答，必须提醒。"""
    if _warned["done"]:
        return
    _warned["done"] = True
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 8799), timeout=0.6):
            print("[xiaoai] 警告：检测到 standalone 实例仍在 8799 运行。"
                  "两个进程会轮询同一个小米账号，请关掉其中一个。", flush=True)
    except OSError:
        pass


def status() -> dict:
    h = home()
    return {
        "available": h is not None,
        "home": str(h) if h else "",
        "prefix": PREFIX,
        "error": "" if h else error(),
    }
