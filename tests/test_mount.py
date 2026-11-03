"""挂载桥接：代码定位与请求分发（不依赖宿主）。"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("xiaoai_app", ROOT / "tools" / "xiaoai_app.py")
xiaoai_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xiaoai_app)


class _FakeOuter:
    """模拟宿主的 BaseHTTPRequestHandler：只记录写了什么。"""

    def __init__(self, command="GET"):
        self.command = command
        self.request = None
        self.client_address = ("127.0.0.1", 12345)
        self.server = None
        self.headers = {}
        self.request_version = "HTTP/1.1"
        self.protocol_version = "HTTP/1.1"
        self.close_connection = False
        self.raw_requestline = b""
        import io

        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.sent = []

    def send_response(self, code, message=None):
        self.sent.append(("status", code))

    def send_header(self, k, v):
        self.sent.append((k.lower(), v))

    def end_headers(self):
        self.sent.append(("end", ""))


class ResolveTest(unittest.TestCase):
    def test_finds_repo_by_env(self):
        os.environ["XIAOAI_HOME"] = str(ROOT)
        xiaoai_app._state["home"] = None
        try:
            self.assertEqual(xiaoai_app.home(), ROOT.resolve())
            self.assertTrue(xiaoai_app.available())
        finally:
            os.environ.pop("XIAOAI_HOME", None)
            xiaoai_app._state["home"] = None

    def test_missing_everything_reports_paths(self):
        saved = dict(os.environ)
        os.environ.pop("XIAOAI_HOME", None)
        xiaoai_app._state["home"] = None
        missing = Path("/definitely/not/here")
        orig = xiaoai_app._candidates
        xiaoai_app._candidates = lambda: iter([missing])
        try:
            self.assertIsNone(xiaoai_app.home())
            msg = xiaoai_app.error()
            # 路径在 Windows 上会规范成反斜杠，按 str(Path) 比对
            self.assertIn(str(missing), msg)
        finally:
            xiaoai_app._candidates = orig
            xiaoai_app._state["home"] = None
            os.environ.clear()
            os.environ.update(saved)


class MountTest(unittest.TestCase):
    def setUp(self):
        os.environ["XIAOAI_HOME"] = str(ROOT)
        xiaoai_app._state["home"] = None
        xiaoai_app._state["handler"] = None

    def tearDown(self):
        os.environ.pop("XIAOAI_HOME", None)
        xiaoai_app._state["home"] = None
        xiaoai_app._state["handler"] = None

    def test_ignores_other_paths(self):
        outer = _FakeOuter()
        self.assertFalse(xiaoai_app.mount(outer, "/teardown/"))
        self.assertFalse(xiaoai_app.mount(outer, "/api/state"))
        self.assertEqual(outer.sent, [])

    def test_prefix_match_not_substring(self):
        outer = _FakeOuter()
        # /xiaoaiX 不能被当成前缀命中
        self.assertFalse(xiaoai_app.mount(outer, "/xiaoaiX/api/state"))

    def _raw(self, outer):
        return outer.wfile.getvalue()

    def _status(self, outer):
        line = self._raw(outer).split(b"\r\n", 1)[0].decode("latin-1")
        return int(line.split()[1])

    def test_handles_root_as_index(self):
        outer = _FakeOuter()
        self.assertTrue(xiaoai_app.mount(outer, "/xiaoai"))
        # 内层 Handler 直接把完整 HTTP 响应写进 wfile，所以解析原始字节
        self.assertEqual(self._status(outer), 200)
        self.assertIn(b"<title>", self._raw(outer))

    def test_strips_prefix_before_dispatch(self):
        outer = _FakeOuter()
        self.assertTrue(xiaoai_app.mount(outer, "/xiaoai/api/ping"))
        body = self._raw(outer).decode("utf-8")
        self.assertIn('"ok": true', body)

    def test_query_string_is_preserved(self):
        """宿主传进来的 path 已去掉 query，必须用 outer.path 补回来。"""
        outer = _FakeOuter()
        outer.path = "/xiaoai/api/ping?x=1"
        self.assertTrue(xiaoai_app.mount(outer, "/xiaoai/api/ping"))
        self.assertIn('"ok": true', self._raw(outer).decode("utf-8"))

    def test_query_reaches_handler(self):
        """带参数的路由要真的收到参数，而不是空。"""
        outer = _FakeOuter()
        outer.path = "/xiaoai/api/miot/spec?device=no-such-device"
        xiaoai_app.mount(outer, "/xiaoai/api/miot/spec")
        body = self._raw(outer).decode("utf-8")
        self.assertNotIn("不能为空", body)

    def test_static_asset_served(self):
        outer = _FakeOuter()
        self.assertTrue(xiaoai_app.mount(outer, "/xiaoai/app.js"))
        self.assertEqual(self._status(outer), 200)
        self.assertGreater(len(self._raw(outer)), 1000)

    def test_ssr_failure_returns_503_with_reason(self):
        xiaoai_app._state["home"] = None
        xiaoai_app._state["handler"] = None
        orig = xiaoai_app._candidates
        xiaoai_app._candidates = lambda: iter([Path("/nope")])
        outer = _FakeOuter()
        try:
            self.assertTrue(xiaoai_app.mount(outer, "/xiaoai"))
            status = [v for k, v in outer.sent if k == "status"][0]
            self.assertEqual(status, 503)
            self.assertIn("找不到", outer.wfile.getvalue().decode("utf-8"))
        finally:
            xiaoai_app._candidates = orig


if __name__ == "__main__":
    unittest.main()
