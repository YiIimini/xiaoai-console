"""米家接入层：值转换、设备查找、安全边界、场景执行。

全部走演示设备，不碰真实米家账号。
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import miot, scenes, store


class _Base(unittest.TestCase):
    def setUp(self):
        self._cfg = copy.deepcopy(store.load_config())
        self._scenes_backup = scenes.load()
        cfg = copy.deepcopy(self._cfg)
        cfg["mi"]["mock"] = True
        cfg["miot"].update({"enabled": True, "allow_write": True, "mock": True})
        store.save_config(cfg)
        miot._CLIENT = None
        miot._CLIENT_KEY = ""

    def tearDown(self):
        store.save_config(self._cfg)
        scenes.save(self._scenes_backup)
        miot._CLIENT = None
        miot._CLIENT_KEY = ""


class CoerceTest(unittest.TestCase):
    def test_bool_words(self):
        for v in ("开", "on", "true", 1, True):
            self.assertIs(miot.coerce_value(v, "bool"), True, v)
        for v in ("关", "off", "0", False):
            self.assertIs(miot.coerce_value(v, "bool"), False, v)

    def test_bool_rejects_nonsense(self):
        with self.assertRaises(miot.MiotError):
            miot.coerce_value("调暗一点", "bool")

    def test_int_from_string(self):
        self.assertEqual(miot.coerce_value("26", "uint8"), 26)
        self.assertEqual(miot.coerce_value(26.7, "uint8"), 26)

    def test_int_rejects_text(self):
        with self.assertRaises(miot.MiotError):
            miot.coerce_value("热一点", "uint8")


class DeviceTest(_Base):
    def test_list_mock_devices(self):
        text = miot.list_devices_text()
        self.assertIn("客厅灯", text)
        self.assertIn("高危·只读", text)

    def test_find_by_name_and_did(self):
        self.assertEqual(miot.find_device("客厅灯")["did"], "mock-light-1")
        self.assertEqual(miot.find_device("mock-light-1")["name"], "客厅灯")

    def test_find_partial(self):
        self.assertEqual(miot.find_device("插座")["name"], "书桌插座")

    def test_find_unknown(self):
        with self.assertRaises(miot.MiotError):
            miot.find_device("不存在的灯")

    def test_capabilities_text(self):
        text = miot.capabilities("客厅灯")
        self.assertIn("power", text)
        self.assertIn("brightness", text)
        self.assertIn("开/关", text)


class WriteTest(_Base):
    def test_roundtrip(self):
        miot.write_prop("客厅灯", "power", "开")
        self.assertIn("= True", miot.read_prop("客厅灯", "power"))
        miot.write_prop("客厅灯", "power", "关")
        self.assertIn("= False", miot.read_prop("客厅灯", "power"))

    def test_high_risk_blocked(self):
        with self.assertRaises(miot.MiotError) as ctx:
            miot.write_prop("大门锁", "lock-state", 0)
        self.assertIn("高危", str(ctx.exception))

    def test_high_risk_action_blocked(self):
        with self.assertRaises(miot.MiotError):
            miot.do_action("大门锁", "unlock")

    def test_write_disabled(self):
        cfg = store.load_config()
        cfg["miot"]["allow_write"] = False
        store.save_config(cfg)
        with self.assertRaises(miot.MiotError) as ctx:
            miot.write_prop("客厅灯", "power", "开")
        self.assertIn("已关闭", str(ctx.exception))

    def test_unknown_prop(self):
        with self.assertRaises(miot.MiotError) as ctx:
            miot.read_prop("客厅灯", "volume")
        self.assertIn("没有属性", str(ctx.exception))


class SceneTest(_Base):
    def test_validate_ok(self):
        ok, msg = scenes.validate({"a": {"steps": [{"device": "客厅灯", "prop": "power", "value": "开"}]}})
        self.assertTrue(ok, msg)

    def test_validate_rejects_bad(self):
        cases = [
            ([], "对象"),
            ({"a": {}}, "steps"),
            ({"a": {"steps": []}}, "缺少"),
            ({"a": {"steps": [{"prop": "power"}]}}, "device"),
            ({"a": {"steps": [{"device": "x"}]}}, "prop 或 action"),
            ({"a": {"steps": [{"delay": "一会儿"}]}}, "delay"),
        ]
        for payload, kw in cases:
            with self.subTest(payload=str(payload)[:40]):
                ok, msg = scenes.validate(payload)
                self.assertFalse(ok)
                self.assertIn(kw, msg)

    def test_run_scene(self):
        scenes.save({"测试": {"desc": "开灯", "steps": [
            {"device": "客厅灯", "prop": "power", "value": "开"},
            {"delay": 0.01},
            {"device": "客厅灯", "prop": "brightness", "value": 30},
        ]}})
        out = scenes.run("测试")
        self.assertIn("已执行", out)
        self.assertIn("= True", miot.read_prop("客厅灯", "power"))
        self.assertIn("= 30", miot.read_prop("客厅灯", "brightness"))

    def test_run_partial_failure_reports_both(self):
        scenes.save({"混合": {"steps": [
            {"device": "客厅灯", "prop": "power", "value": "开"},
            {"device": "大门锁", "prop": "lock-state", "value": 0},
        ]}})
        out = scenes.run("混合")
        self.assertIn("部分完成", out)
        self.assertIn("高危", out)

    def test_run_all_failed_raises(self):
        scenes.save({"全错": {"steps": [
            {"device": "大门锁", "prop": "lock-state", "value": 0},
        ]}})
        with self.assertRaises(miot.MiotError):
            scenes.run("全错")

    def test_unknown_scene(self):
        scenes.save({"睡觉": {"steps": [{"device": "客厅灯", "prop": "power", "value": "关"}]}})
        with self.assertRaises(miot.MiotError):
            scenes.run("洗衣服")

    def test_fuzzy_scene_name(self):
        scenes.save({"睡觉模式": {"steps": [{"device": "客厅灯", "prop": "power", "value": "关"}]}})
        self.assertIn("睡觉模式", scenes.run("睡觉"))


class ToolDispatchTest(_Base):
    def test_tools_registered(self):
        from app import tools
        names = [t["function"]["name"] for t in tools.TOOL_SPECS]
        for n in ("mi_home_devices", "mi_home_set", "mi_home_scene"):
            self.assertIn(n, names)

    def test_dispatch_devices(self):
        from app import tools
        out = tools.dispatch({}, "mi_home_devices", {})
        self.assertIn("客厅灯", out)

    def test_dispatch_blocked_returns_message(self):
        from app import tools
        out = tools.dispatch({}, "mi_home_set", {"device": "大门锁", "prop": "lock-state", "value": 0})
        self.assertIn("高危", out)

    def test_disabled_switch(self):
        from app import tools
        cfg = store.load_config()
        cfg["miot"]["enabled"] = False
        store.save_config(cfg)
        out = tools.dispatch({}, "mi_home_devices", {})
        self.assertIn("已关闭", out)


if __name__ == "__main__":
    unittest.main()


class NoSilentDemoTest(_Base):
    """取消演示后，缺依赖必须报错，不能偷偷换成假设备。"""

    def test_missing_dependency_is_loud(self):
        cfg = store.load_config()
        cfg["mi"]["mock"] = False
        cfg["miot"]["mock"] = False
        store.save_config(cfg)
        miot._CLIENT = None
        miot._CLIENT_KEY = ""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "miservice":
                raise ImportError("模拟缺依赖")
            return real_import(name, *a, **k)

        builtins.__import__ = fake_import
        try:
            client = miot.make_client({"mock": False})
            self.assertIsInstance(client, miot.MissingDependencyMiot)
            with self.assertRaises(miot.MiotError) as ctx:
                client.devices()
            self.assertIn("miservice", str(ctx.exception))
        finally:
            builtins.__import__ = real_import

    def test_demo_flag_still_works(self):
        cfg = store.load_config()
        cfg["miot"]["mock"] = True
        store.save_config(cfg)
        self.assertIsInstance(miot.make_client({"mock": False}), miot.MockMiot)
