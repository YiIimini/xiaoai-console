"""登录策略：能复用 token 就不要重新登录，风控要能被识别并冷却。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import mi


class _FakeStore:
    def __init__(self, token):
        self._token = token
        self.loads = 0

    async def load_token(self):
        self.loads += 1
        return self._token


class _FakeAccount:
    def __init__(self, token=None):
        self.token = None
        self.token_store = _FakeStore(token)
        self.login_calls = []

    async def login(self, sid):
        self.login_calls.append(sid)
        return True


class EnsureLoginTest(unittest.TestCase):
    def setUp(self):
        mi._COOLDOWN.update({"until": 0.0, "reason": ""})

    def test_reuses_cached_token(self):
        import asyncio

        acc = _FakeAccount({"userId": 1, "micoapi": ("ss", "st")})
        asyncio.run(mi.ensure_login(acc, "micoapi"))
        self.assertEqual(acc.login_calls, [], "有缓存还去登录了")
        self.assertEqual(acc.token_store.loads, 1)

    def test_logs_in_when_sid_missing(self):
        import asyncio

        acc = _FakeAccount({"userId": 1})       # 有 token 但没有这个 sid
        asyncio.run(mi.ensure_login(acc, "xiaomiio"))
        self.assertEqual(acc.login_calls, ["xiaomiio"])

    def test_logs_in_when_no_token(self):
        import asyncio

        acc = _FakeAccount(None)
        asyncio.run(mi.ensure_login(acc, "micoapi"))
        self.assertEqual(acc.login_calls, ["micoapi"])

    def test_broken_token_store_falls_back_to_login(self):
        import asyncio

        acc = _FakeAccount(None)

        async def boom():
            raise OSError("磁盘坏了")

        acc.token_store.load_token = boom
        asyncio.run(mi.ensure_login(acc, "micoapi"))
        self.assertEqual(acc.login_calls, ["micoapi"])


class RiskControlTest(unittest.TestCase):
    def setUp(self):
        mi._COOLDOWN.update({"until": 0.0, "reason": ""})

    def tearDown(self):
        mi._COOLDOWN.update({"until": 0.0, "reason": ""})

    def test_recognises_risk_control(self):
        err = mi.friendly_mi_error(Exception(
            "Failed to send OTP SMS: {'reason': '用户行为被限制', 'code': 20024}"))
        text = str(err)
        self.assertIn("风控", text)
        self.assertIn("等 30–60 分钟", text)
        self.assertIn("原始错误", text)

    def test_cooldown_blocks_next_attempt(self):
        mi.friendly_mi_error(Exception("用户行为被限制"))
        blocked = mi.in_cooldown()
        self.assertIn("冷却中", blocked)
        with self.assertRaises(mi.MiError):
            import asyncio
            spk = mi.RealSpeaker({"username": "u", "password": "p"}, _NullLog())
            asyncio.run(spk.connect())
        self.assertIn("冷却中", str(blocked))

    def test_plain_error_passes_through(self):
        err = mi.friendly_mi_error(Exception("网络断了"))
        self.assertIn("网络断了", str(err))
        self.assertEqual(mi.in_cooldown(), "")

    def test_cooldown_expires(self):
        mi.set_cooldown(0, "测试")
        mi._COOLDOWN["until"] = time.time() - 1
        self.assertEqual(mi.in_cooldown(), "")


class _NullLog:
    def info(self, *a, **k): pass
    warn = error = ok = info


if __name__ == "__main__":
    unittest.main()
