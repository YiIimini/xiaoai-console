"""打断与并发：新语音到达时作废旧轮，旧轮的结果不能再播出来。"""
import copy
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bridge, store
from app.mi import MockSpeaker


class _Log:
    def info(self, *a, **k): pass
    warn = error = ok = info


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.br = bridge.Bridge()
        self.br.speaker = MockSpeaker({}, _Log())
        self.br._gen = 0
        self.br._busy = False

    def test_barge_in_bumps_generation(self):
        before = self.br._gen
        bridge.asyncio.run(self.br._barge_in())
        self.assertGreater(self.br._gen, before)

    def test_old_generation_cannot_speak(self):
        self.br._gen = 5
        ok = bridge.asyncio.run(self.br._say({"tts_max_chars": 160}, "这句话不该被念", gen=4))
        self.assertFalse(ok)
        self.assertEqual(self.br.speaker.spoken, [])

    def test_current_generation_speaks(self):
        self.br._gen = 7
        ok = bridge.asyncio.run(self.br._say({"tts_max_chars": 160}, "可以念", gen=7))
        self.assertTrue(ok)

    def test_stop_is_called(self):
        called = {}

        async def fake_stop(did=""):
            called["yes"] = True
            return True

        self.br.speaker.stop = fake_stop
        bridge.asyncio.run(self.br._barge_in())
        self.assertTrue(called.get("yes"))

    def test_drain_stops_on_new_generation(self):
        import queue

        q = queue.Queue()
        q.put("第一句。")
        self.br._gen = 2
        # 拿到第一句后立刻模拟被打断
        orig = self.br._say

        async def say_and_bump(cfg, text, gen=0, device=""):
            r = await orig(cfg, text, gen)
            self.br._gen += 1
            q.put("第二句。")
            q.put(None)
            return r

        self.br._say = say_and_bump
        spoke = bridge.asyncio.run(self.br._drain_sentences(q, {"tts_max_chars": 160}, gen=2, device="mock-0001"))
        self.assertTrue(spoke)
        # 第二句属于旧轮，被打断后不应播
        self.assertEqual([s["text"] for s in self.br.speaker.spoken], ["第一句。"])


class ConfigDefaultTest(unittest.TestCase):
    def test_barge_in_default_on(self):
        self.assertTrue(store.DEFAULT_CONFIG["voice"].get("barge_in"))

    def test_busy_exposed_in_status(self):
        br = bridge.Bridge()
        self.assertIn("busy", br.status())


if __name__ == "__main__":
    unittest.main()


class SpeakerSelectionTest(unittest.TestCase):
    """取消演示模式后不能偷偷退回演示。"""

    def test_mock_flag_wins(self):
        from app.mi import MockSpeaker, make_speaker

        cfg = {"mock": True}
        self.assertIsInstance(make_speaker(cfg, _Log()), MockSpeaker)

    def test_missing_dependency_is_loud(self):
        import asyncio

        from app import mi

        orig = mi.miservice_available
        mi.miservice_available = lambda: False
        try:
            spk = mi.make_speaker({"mock": False}, _Log())
            self.assertIsInstance(spk, mi.MissingDependencySpeaker)
            with self.assertRaises(mi.MiError) as ctx:
                asyncio.run(spk.connect())
            self.assertIn("miservice", str(ctx.exception))
        finally:
            mi.miservice_available = orig
