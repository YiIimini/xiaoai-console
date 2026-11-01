"""工作台 HTTP 服务：纯标准库 http.server，零框架依赖。

局部 API：
  GET  /api/ping | /api/state | /api/history | /api/devices | /api/logs
  GET  /api/logs/stream                     SSE 实时日志
  POST /api/config | /api/bridge/{start,stop,restart} | /api/chat
  POST /api/tts | /api/diag/{llm,mi} | /api/mi/otp | /api/mock/say
  POST /api/history/clear
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import bridge, health, llm, store
from .mi import OTP, MiError, miservice_available

ROOT = store.ROOT
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
LOG = store.LOG
HIST = store.HIST
BR = bridge.BRIDGE


def run_async(coro, timeout: float = 90) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout))
    finally:
        try:
            loop.close()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoaiConsole/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    # ------------------------------------------------------------ 工具
    MAX_BODY = 2 * 1024 * 1024      # 2 MB，防止超大请求打爆内存

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _same_origin(self) -> bool:
        """只接受本机来源的写请求，挡住 DNS rebinding 和跨站表单。"""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True                     # 非浏览器客户端（curl / 脚本）放行
        host = (self.headers.get("Host") or "").strip()
        try:
            o = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        ok_hosts = {"127.0.0.1", "localhost", "::1"}
        return o.hostname in ok_hosts and (not host or o.netloc == host)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > self.MAX_BODY:
            raise ValueError("请求体过大（上限 %d 字节）" % self.MAX_BODY)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path == "/api/ping":
            return self._json({"ok": True, "ts": time.time()})
        if path == "/api/state":
            cfg = store.load_config()
            return self._json({
                "ok": True,
                "config": store.mask_config(cfg),
                "bridge": BR.status(),
                "miservice": miservice_available(),
                "otp": OTP.snapshot(),
                "mock": bool(cfg["mi"].get("mock")) or not miservice_available(),
                "version": "1.0.0",
            })
        if path == "/api/history":
            return self._json({"ok": True, "items": HIST.list(int(qs.get("limit", ["100"])[0]))})
        if path == "/api/logs":
            after = int(qs.get("after", ["0"])[0])
            limit = int(qs.get("limit", ["300"])[0])
            return self._json({"ok": True, "items": LOG.tail(limit, after)})
        if path == "/api/logs/stream":
            return self._sse()
        if path == "/api/devices":
            return self._devices()
        if path == "/api/portable/selfcheck":
            return self._json({"ok": True, **health.check(SERVER_PORT)})
        if path == "/api/portable/export":
            secrets = qs.get("secrets", ["0"])[0] in ("1", "true", "yes")
            return self._json({"ok": True, "bundle": store.export_config(secrets)})
        if path == "/api/portable/machine":
            return self._json({"ok": True, **health.machine_info()})
        if path.startswith("/api/"):
            return self._err("未知接口: " + path, 404)
        return self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._same_origin():
            LOG.warn("拒绝跨站写请求: Origin=%s" % self.headers.get("Origin"))
            return self._err("跨站请求被拒绝", 403)
        body = self._body()
        try:
            if path == "/api/config":
                return self._post_config(body)
            if path == "/api/bridge/start":
                return self._json({"ok": True, **BR.start(), "bridge": BR.status()})
            if path == "/api/bridge/stop":
                return self._json({"ok": True, **BR.stop(), "bridge": BR.status()})
            if path == "/api/bridge/restart":
                BR.stop()
                time.sleep(0.3)
                return self._json({"ok": True, **BR.start(), "bridge": BR.status()})
            if path == "/api/chat":
                return self._chat(body)
            if path == "/api/tts":
                return self._tts(body)
            if path == "/api/mock/say":
                return self._mock_say(body)
            if path == "/api/diag/llm":
                return self._diag_llm()
            if path == "/api/diag/mi":
                return self._diag_mi()
            if path == "/api/mi/otp":
                code = str(body.get("code") or "").strip()
                if not code:
                    return self._err("验证码为空")
                OTP.provide(code)
                LOG.info("已提交验证码")
                return self._json({"ok": True})
            if path == "/api/portable/import":
                return self._portable_import(body)
            if path == "/api/portable/default-repo":
                cfg = store.load_config()
                default = os.path.dirname(store.ROOT)
                store.save_config({"agent": {"repo": default}})
                LOG.warn("仓库路径已重置为 %s" % default)
                return self._json({"ok": True, "repo": default})
            if path == "/api/history/clear":
                HIST.clear()
                return self._json({"ok": True})
            return self._err("未知接口: " + path, 404)
        except MiError as e:
            return self._err("小米接口: %s" % e, 502)
        except Exception as e:  # noqa: BLE001
            LOG.error("接口异常 %s: %s" % (path, e))
            return self._err("%s: %s" % (type(e).__name__, e), 500)

    # ------------------------------------------------------------ 具体实现
    def _post_config(self, body: Dict[str, Any]) -> None:
        patch: Dict[str, Any] = {}
        for section in ("mi", "llm", "agent", "voice"):
            if isinstance(body.get(section), dict):
                patch[section] = dict(body[section])
        # 掩码占位符表示「不修改」
        mi_pw = (patch.get("mi") or {}).get("password")
        if mi_pw == store.PASSWORD_MASK:
            patch["mi"].pop("password", None)
        llm_key = (patch.get("llm") or {}).get("api_key")
        if llm_key and ("…" in llm_key or llm_key == store.PASSWORD_MASK):
            patch["llm"].pop("api_key", None)
        for sec, key, lo, hi in (("mi", "poll_interval", 0.5, 60),
                                 ("llm", "temperature", 0.0, 2.0),
                                 ("llm", "max_tool_rounds", 1, 20),
                                 ("llm", "timeout", 10, 600),
                                 ("agent", "shell_timeout", 5, 600),
                                 ("agent", "max_output", 500, 20000),
                                 ("voice", "tts_max_chars", 40, 600),
                                 ("voice", "continuous_window", 0, 120)):
            holder = patch.get(sec)
            if isinstance(holder, dict) and key in holder:
                try:
                    holder[key] = min(hi, max(lo, float(holder[key])))
                except (TypeError, ValueError):
                    holder.pop(key, None)
        cfg = store.save_config(patch)
        LOG.info("配置已保存")
        return self._json({"ok": True, "config": store.mask_config(cfg)})

    def _chat(self, body: Dict[str, Any]) -> None:
        text = str(body.get("text") or "").strip()
        if not text:
            return self._err("内容为空")
        LOG.info("⌨️  手动测试: %s" % text)
        turn = BR.run_agent(text, "manual")
        if body.get("speak"):
            cfg = store.load_config()
            if BR.speaker is None:
                return self._json({"ok": True, "turn": turn, "warn": "桥接未启动，未播报"})
            speech = bridge.to_speech(turn.get("answer", ""), int(cfg["voice"].get("tts_max_chars", 160)))
            try:
                ok = run_async(BR.speaker.speak(speech), 20)
                turn["spoken"] = bool(ok)
                LOG.info("🔊 手动播报: %s" % speech)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": True, "turn": turn, "warn": "播报失败: %s" % e})
        return self._json({"ok": True, "turn": turn})

    def _tts(self, body: Dict[str, Any]) -> None:
        text = str(body.get("text") or "").strip()
        if not text:
            return self._err("播报内容为空")
        if BR.speaker is None:
            return self._err("桥接未启动，先点「启动桥接」")
        ok = run_async(BR.speaker.speak(text), 20)
        LOG.info("🔊 试播: %s" % text)
        return self._json({"ok": bool(ok)})

    def _mock_say(self, body: Dict[str, Any]) -> None:
        text = str(body.get("text") or "").strip()
        if not text:
            return self._err("内容为空")
        if BR.speaker is None or not hasattr(BR.speaker, "inject"):
            return self._err("当前不是演示模式，且桥接未启动")
        BR.speaker.inject(text)
        LOG.info("🧪 模拟发问: %s" % text)
        return self._json({"ok": True})

    def _devices(self) -> None:
        cfg = store.load_config()
        from .mi import make_speaker

        spk = make_speaker(cfg["mi"], LOG)
        devs = run_async(spk.devices(), 60) or []
        rows = [
            {
                "did": str(d.get("deviceID") or ""),
                "hardware": d.get("hardware") or "",
                "name": d.get("name") or d.get("alias") or "",
            }
            for d in devs
        ]
        from .mi import MockSpeaker
        return self._json({"ok": True, "devices": rows, "mock": isinstance(spk, MockSpeaker)})

    def _portable_import(self, body: Dict[str, Any]) -> None:
        bundle = body.get("bundle")
        if isinstance(bundle, str):
            try:
                bundle = json.loads(bundle)
            except ValueError:
                return self._err("配置 JSON 解析失败，检查是否复制完整")
        if not isinstance(bundle, dict):
            return self._err("缺少配置内容")
        incoming = bundle.get("config") if "config" in bundle else bundle
        if not isinstance(incoming, dict):
            return self._err("配置结构不对，需要 {config: {...}} 或直接是配置对象")
        patch: Dict[str, Any] = {}
        for section in ("mi", "llm", "agent", "voice"):
            if isinstance(incoming.get(section), dict):
                patch[section] = dict(incoming[section])
        patch = store.sanitize_for_import(patch)
        # 空密钥不覆盖本机已有值
        if not (patch.get("mi", {}).get("password") or "").strip():
            patch.get("mi", {}).pop("password", None)
        if not (patch.get("llm", {}).get("api_key") or "").strip():
            patch.get("llm", {}).pop("api_key", None)
        cfg = store.save_config(patch)
        LOG.ok("已导入配置（机器相关路径已按本机重写）")
        return self._json({"ok": True, "config": store.mask_config(cfg),
                           "note": "路径已重写为本机可用值；如桥接正在运行请点「重启」"})

    def _diag_llm(self) -> None:
        cfg = store.load_config()
        try:
            r = llm.ping(cfg["llm"])
            LOG.ok("LLM 连通: %s (%s)" % (r.get("reply"), r.get("model")))
            return self._json({"ok": True, **r})
        except llm.LLMError as e:
            LOG.error("LLM 诊断失败: %s" % e)
            return self._json({"ok": False, "error": str(e)}, 502)

    def _diag_mi(self) -> None:
        cfg = store.load_config()
        from .mi import make_speaker

        spk = make_speaker(cfg["mi"], LOG)
        t0 = time.time()
        run_async(spk.connect(), 180)
        devs = run_async(spk.devices(), 60) or []
        return self._json({
            "ok": True,
            "ms": int((time.time() - t0) * 1000),
            "devices": [
                {"did": str(d.get("deviceID") or ""), "hardware": d.get("hardware") or "", "name": d.get("name") or ""}
                for d in devs
            ],
        })

    # ------------------------------------------------------------ SSE
    def _sse(self) -> None:
        try:
            after = int((urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                         .get("after", ["0"])[0]))
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = LOG.subscribe()
        try:
            for rec in LOG.tail(200, after):
                self.wfile.write(("data: " + json.dumps(rec, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    rec = q.get(timeout=15)
                    self.wfile.write(("data: " + json.dumps(rec, ensure_ascii=False) + "\n\n").encode("utf-8"))
                except Exception:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            LOG.unsubscribe(q)

    # ------------------------------------------------------------ 静态文件
    def _static(self, path: str) -> None:  # noqa: D401
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.realpath(WEB)):
            return self._err("禁止访问", 403)
        if not os.path.isfile(full):
            return self._err("文件不存在: " + rel, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def _bind(host: str, want: int, span: int = 20):
    """端口被占用时自动后移，返回 (httpd, 实际端口)。"""
    last = None
    for p in range(want, want + span):
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            return httpd, p
        except OSError as e:
            last = e
            continue
    raise SystemExit("端口 %d-%d 全部被占用: %s" % (want, want + span - 1, last))


SERVER_PORT = 0


def serve(host: str, port: int) -> None:
    global SERVER_PORT
    httpd, port = _bind(host, port)
    SERVER_PORT = port
    httpd.daemon_threads = True
    try:
        with open(os.path.join(store.DATA_DIR, "port"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass
    print("工作台已启动: http://%s:%d" % (host, port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        BR.stop()
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--autostart", action="store_true", help="启动服务后自动拉起桥接")
    args = ap.parse_args()
    if args.autostart:
        threading.Timer(1.5, BR.start).start()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
