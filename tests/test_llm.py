"""LLM 客户端的端点拼接与重试策略（不真实联网）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm


class EndpointTest(unittest.TestCase):
    def test_appends_path(self):
        self.assertEqual(llm._endpoint("https://api.x.com/v1"),
                         "https://api.x.com/v1/chat/completions")

    def test_strips_trailing_slash(self):
        self.assertEqual(llm._endpoint("https://api.x.com/v1/"),
                         "https://api.x.com/v1/chat/completions")

    def test_keeps_full_url(self):
        full = "https://api.x.com/v1/chat/completions"
        self.assertEqual(llm._endpoint(full), full)

    def test_empty_raises(self):
        with self.assertRaises(llm.LLMError):
            llm._endpoint("")


class RetryTest(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        ok = {"role": "assistant", "content": "好"}
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise llm.LLMError("LLM HTTP 429: rate limited")
            return ok

        with mock.patch.object(llm, "_chat_once", side_effect=flaky), \
             mock.patch.object(llm.time, "sleep", return_value=None):
            out = llm.chat([{"role": "user", "content": "hi"}],
                           {"api_key": "k", "base_url": "https://x/v1"})
        self.assertEqual(out["content"], "好")
        self.assertEqual(calls["n"], 3)

    def test_no_retry_on_auth_error(self):
        calls = {"n": 0}

        def bad(*a, **k):
            calls["n"] += 1
            raise llm.LLMError("LLM HTTP 401: invalid key")

        with mock.patch.object(llm, "_chat_once", side_effect=bad):
            with self.assertRaises(llm.LLMError):
                llm.chat([{"role": "user", "content": "hi"}],
                         {"api_key": "k", "base_url": "https://x/v1"})
        self.assertEqual(calls["n"], 1)

    def test_gives_up_after_retries(self):
        calls = {"n": 0}

        def always(*a, **k):
            calls["n"] += 1
            raise llm.LLMError("LLM 请求超时（90s）")

        with mock.patch.object(llm, "_chat_once", side_effect=always), \
             mock.patch.object(llm.time, "sleep", return_value=None):
            with self.assertRaises(llm.LLMError):
                llm.chat([{"role": "user", "content": "hi"}],
                         {"api_key": "k", "base_url": "https://x/v1"}, retries=2)
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
