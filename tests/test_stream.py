"""流式：句子切分、SSE 解析、降级兜底。"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bridge, llm


class SentenceStreamTest(unittest.TestCase):
    def test_splits_on_punctuation(self):
        sp = bridge.SentenceStream(min_len=2)
        out = sp.feed("第一句。第二句！")
        self.assertEqual(out, ["第一句。", "第二句！"])

    def test_holds_short_fragment(self):
        sp = bridge.SentenceStream(min_len=6)
        self.assertEqual(sp.feed("好。"), [])
        out = sp.feed("现在灯已经打开了。")
        self.assertEqual(out, ["现在灯已经打开了。"])

    def test_flush_returns_remainder(self):
        sp = bridge.SentenceStream(min_len=2)
        sp.feed("没有句号的残句")
        self.assertEqual(sp.flush(), ["没有句号的残句"])

    def test_flush_empty_is_silent(self):
        """空残句不能产出兜底话术，否则会多播一句「我这边没有拿到结果」。"""
        sp = bridge.SentenceStream()
        sp.feed("完整一句。")
        self.assertEqual(sp.flush(), [])

    def test_code_block_not_spoken(self):
        sp = bridge.SentenceStream(min_len=1)
        out = sp.feed("结果是：\n```py\nprint(1)\n```\n完成。")
        self.assertTrue(all("print(1)" not in s for s in out))


class _SSEHandler(BaseHTTPRequestHandler):
    mode = "stream"          # stream | reject
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if _SSEHandler.mode == "reject":
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in ("你好", "，世界", "。"):
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        # 一个分片到达的 tool_call
        tc1 = {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": '{"pa'}}]}}]}
        tc2 = {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'th":"a.txt"}'}}]}}]}
        self.wfile.write(("data: " + json.dumps(tc1) + "\n\n").encode())
        self.wfile.write(("data: " + json.dumps(tc2) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class StreamParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8891
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), _SSEHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def cfg(self):
        return {"api_key": "k", "base_url": "http://127.0.0.1:%d/v1" % self.port,
                "model": "m", "timeout": 10, "stream": True}

    def test_stream_collects_text_and_tool_calls(self):
        _SSEHandler.mode = "stream"
        deltas = []
        msg = llm.chat_stream([{"role": "user", "content": "hi"}], self.cfg(),
                              tools=[{"type": "function", "function": {"name": "read_file"}}],
                              on_delta=deltas.append)
        self.assertEqual(msg["content"], "你好，世界。")
        self.assertEqual("".join(deltas), "你好，世界。")
        self.assertEqual(len(msg["tool_calls"]), 1)
        call = msg["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a.txt"})

    def test_stream_or_chat_falls_back_on_400(self):
        _SSEHandler.mode = "reject"
        plain = {"role": "assistant", "content": "兜底"}
        with mock.patch.object(llm, "chat", return_value=plain) as m:
            out = llm.stream_or_chat([{"role": "user", "content": "hi"}], self.cfg(), None, lambda t: None)
        self.assertEqual(out["content"], "兜底")
        m.assert_called_once()

    def test_stream_disabled_uses_plain(self):
        _SSEHandler.mode = "stream"
        plain = {"role": "assistant", "content": "普通"}
        cfg = dict(self.cfg(), stream=False)
        with mock.patch.object(llm, "chat", return_value=plain) as m:
            out = llm.stream_or_chat([{"role": "user", "content": "hi"}], cfg, None, lambda t: None)
        self.assertEqual(out["content"], "普通")
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
