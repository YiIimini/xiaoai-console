"""多音箱：轮询合并、定向播报、设备命名。"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store
from app.mi import MOCK_DEVICES, MockSpeaker


class _Log:
    def info(self, *a, **k): pass
    warn = error = ok = info


class MockSpeakerTest(unittest.TestCase):
    def setUp(self):
        self._cfg = copy.deepcopy(store.load_config())
        self.sp = MockSpeaker({"mock": True, "dids": ["mock-0001", "mock-0002"]}, _Log())

    def tearDown(self):
        store.save_config(self._cfg)

    def test_two_devices_listed(self):
        self.assertEqual(len(MOCK_DEVICES), 2)
        self.assertEqual(self.sp.resolve_dids(), ["mock-0001", "mock-0002"])

    def test_fallback_to_single_did(self):
        sp = MockSpeaker({"mock": True, "did": "mock-0002"}, _Log())
        self.assertEqual(sp.resolve_dids(), ["mock-0002"])

    def test_name_of(self):
        self.assertEqual(self.sp.name_of("mock-0002"), "演示音箱·卧室")
        self.assertEqual(self.sp.name_of("unknown"), "unknown")

    def test_speak_records_device(self):
        import asyncio

        asyncio.run(self.sp.speak("你好", "mock-0002"))
        self.assertEqual(self.sp.spoken[-1]["device"], "演示音箱·卧室")

    def test_inject_carries_device(self):
        import asyncio

        self.sp.inject("测试一句", "mock-0002")
        items = asyncio.run(self.sp.poll())
        self.assertEqual(items[0]["device"], "mock-0002")

    def test_inject_unique_request_ids(self):
        self.sp.inject("同一句话", "mock-0001")
        self.sp.inject("同一句话", "mock-0002")
        import asyncio

        items = asyncio.run(self.sp.poll())
        self.assertEqual(len({i["request_id"] for i in items}), 2)

    def test_default_config_has_dids_field(self):
        self.assertIn("dids", store.DEFAULT_CONFIG["mi"])
        self.assertEqual(store.DEFAULT_CONFIG["mi"]["dids"], [])


if __name__ == "__main__":
    unittest.main()
