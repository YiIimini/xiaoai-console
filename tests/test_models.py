"""模型列表拉取：不该把模型 ID 写死在代码里。"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.path.endswith("/models"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if _Handler.mode == "401":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if _Handler.mode == "notjson":
            out = b"<html>not json</html>"
        elif _Handler.mode == "nodata":
            out = json.dumps({"error": "x"}).encode()
        else:
            out = json.dumps({"object": "list", "data": [
                {"id": "m-b"}, {"id": "m-a"}, {"id": "m-a"}, {"no_id": 1}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ModelListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8894
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), _Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def cfg(self, **kw):
        base = {"base_url": "http://127.0.0.1:%d/v1" % self.port, "api_key": "k", "timeout": 5}
        base.update(kw)
        return base

    def test_returns_sorted_unique(self):
        _Handler.mode = "ok"
        self.assertEqual(llm.list_models(self.cfg()), ["m-a", "m-b"])

    def test_requires_api_key(self):
        with self.assertRaises(llm.LLMError) as ctx:
            llm.list_models({"base_url": "http://x/v1", "api_key": ""})
        self.assertIn("Key", str(ctx.exception))

    def test_http_error_surfaces(self):
        _Handler.mode = "401"
        with self.assertRaises(llm.LLMError) as ctx:
            llm.list_models(self.cfg())
        self.assertIn("401", str(ctx.exception))

    def test_non_json_rejected(self):
        _Handler.mode = "notjson"
        with self.assertRaises(llm.LLMError):
            llm.list_models(self.cfg())

    def test_missing_data_array(self):
        _Handler.mode = "nodata"
        with self.assertRaises(llm.LLMError) as ctx:
            llm.list_models(self.cfg())
        self.assertIn("data", str(ctx.exception))

    def test_trailing_slash_base(self):
        _Handler.mode = "ok"
        cfg = self.cfg(base_url="http://127.0.0.1:%d/v1/" % self.port)
        self.assertEqual(llm.list_models(cfg), ["m-a", "m-b"])


if __name__ == "__main__":
    unittest.main()
