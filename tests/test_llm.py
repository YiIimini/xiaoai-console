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


class FriendlyErrorTest(unittest.TestCase):
    """各家错误 JSON 五花八门，得翻成能照做的话。"""

    def test_401_is_explained(self):
        msg = str(llm._friendly_http_error(
            401, '{"error":{"message":"Authentication Fails, Your api key: ****a817 is invalid"}}',
            "拉取模型列表"))
        self.assertIn("Key 被端点拒绝", msg)
        self.assertIn("重新生成", msg)
        self.assertIn("a817", msg)          # 保留平台原文，方便核对是哪把 Key

    def test_404_mentions_base_url(self):
        msg = str(llm._friendly_http_error(404, "", "模型调用"))
        self.assertIn("/v1", msg)

    def test_429_mentions_quota(self):
        self.assertIn("额度", str(llm._friendly_http_error(429, "", "模型调用")))

    def test_5xx_is_transient(self):
        self.assertIn("恢复", str(llm._friendly_http_error(503, "", "模型调用")))

    def test_garbage_body_does_not_crash(self):
        msg = str(llm._friendly_http_error(401, "<html>502</html>", "x"))
        self.assertIn("Key 被端点拒绝", msg)

    def test_plain_string_error_body(self):
        msg = str(llm._friendly_http_error(401, '{"error":"bad key"}', "x"))
        self.assertIn("bad key", msg)
