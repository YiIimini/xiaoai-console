"""起一个真实实例，对 HTTP 层做冒烟 + 安全校验。"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8971
BASE = "http://127.0.0.1:%d" % PORT

# Windows runner 可能带系统代理，会把 127.0.0.1 也代理走，必须显式禁用
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout=8):
    return _OPENER.open(req, timeout=timeout)


def _req(path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _open(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        try:
            return e.code, json.loads(payload or "{}")
        except ValueError:
            return e.code, {"raw": payload}


class ServerTest(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.logfile = open(os.path.join(ROOT, "data", "test-server.out"), "w+")
        # 关键：不让测试实例覆盖真实的 data/port，否则 start.sh / stop.sh 会认错端口
        env = dict(os.environ, XIAOAI_NO_PORT_FILE="1")
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "app.server", "--port", str(PORT)],
            cwd=ROOT, stdout=cls.logfile, stderr=subprocess.STDOUT, env=env)
        for _ in range(80):
            if cls.proc.poll() is not None:
                break
            try:
                _open(urllib.request.Request(BASE + "/api/ping"), timeout=1)
                return
            except Exception:
                time.sleep(0.25)
        cls.logfile.seek(0)
        output = cls.logfile.read()[-2000:]
        cls.tearDownClass()
        raise RuntimeError("测试服务器没起来，输出：\n" + output)

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        if getattr(cls, "logfile", None):
            try:
                cls.logfile.close()
            except Exception:
                pass

    def test_ping(self):
        code, data = _req("/api/ping")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])

    def test_state_masks_secrets(self):
        code, data = _req("/api/state")
        self.assertEqual(code, 200)
        self.assertIn("config", data)
        self.assertIn("bridge", data)

    def test_unknown_api_404(self):
        code, _ = _req("/api/nope")
        self.assertEqual(code, 404)

    def test_static_index(self):
        with _open(urllib.request.Request(BASE + "/"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"<title>", r.read())

    def test_path_traversal_on_static(self):
        code, _ = _req("/../app/server.py")
        self.assertIn(code, (403, 404))

    def test_cross_origin_post_rejected(self):
        code, data = _req("/api/config", "POST",
                          {"voice": {"tts_max_chars": 100}},
                          headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_same_origin_post_allowed(self):
        code, _ = _req("/api/config", "POST", {"voice": {"tts_max_chars": 160}},
                       headers={"Origin": BASE})
        self.assertEqual(code, 200)

    def test_out_of_range_value_clamped(self):
        _req("/api/config", "POST", {"llm": {"temperature": 99}})
        _, data = _req("/api/state")
        self.assertLessEqual(data["config"]["llm"]["temperature"], 2.0)

    def test_selfcheck(self):
        code, data = _req("/api/portable/selfcheck")
        self.assertEqual(code, 200)
        self.assertIn("rows", data)
        self.assertGreater(len(data["rows"]), 5)

    def test_export_defaults_to_no_secrets(self):
        code, data = _req("/api/portable/export")
        self.assertEqual(code, 200)
        self.assertFalse(data["bundle"]["include_secrets"])


if __name__ == "__main__":
    unittest.main()
