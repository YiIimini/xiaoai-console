"""对话记忆：上下文拼装、预算、过期、持久化。"""
import copy
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import memory, store


class MemoryUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mem = memory.Memory(os.path.join(self.dir, "mem.json"))
        self._cfg = copy.deepcopy(store.load_config())
        cfg = copy.deepcopy(self._cfg)
        cfg["memory"].update({"enabled": True, "persist": True, "max_turns": 6,
                              "max_chars": 2000, "ttl_minutes": 30})
        store.save_config(cfg)

    def tearDown(self):
        store.save_config(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_context_pairs(self):
        self.mem.add("第一问", "第一答")
        self.mem.add("第二问", "第二答")
        ctx = self.mem.context()
        self.assertEqual([m["role"] for m in ctx], ["user", "assistant", "user", "assistant"])
        self.assertEqual(ctx[0]["content"], "第一问")
        self.assertEqual(ctx[-1]["content"], "第二答")

    def test_max_turns_keeps_latest(self):
        for i in range(5):
            self.mem.add("问%d" % i, "答%d" % i)
        cfg = store.load_config()
        cfg["memory"]["max_turns"] = 2
        store.save_config(cfg)
        ctx = self.mem.context()
        self.assertEqual(len(ctx), 4)
        self.assertEqual(ctx[0]["content"], "问3")

    def test_char_budget_drops_oldest(self):
        for i in range(4):
            self.mem.add("Q" * 50 + str(i), "A" * 50)
        cfg = store.load_config()
        cfg["memory"]["max_chars"] = 220
        store.save_config(cfg)
        ctx = self.mem.context()
        self.assertLessEqual(sum(len(m["content"]) for m in ctx), 220)
        self.assertIn("3", ctx[-2]["content"])

    def test_ttl_expires(self):
        self.mem.add("很久以前", "旧答")
        self.mem._turns[0]["ts"] = time.time() - 3600
        self.assertEqual(self.mem.context(), [])

    def test_ttl_zero_never_expires(self):
        self.mem.add("很久以前", "旧答")
        self.mem._turns[0]["ts"] = time.time() - 86400
        cfg = store.load_config()
        cfg["memory"]["ttl_minutes"] = 0
        store.save_config(cfg)
        self.assertEqual(len(self.mem.context()), 2)

    def test_disabled_returns_nothing(self):
        self.mem.add("问", "答")
        cfg = store.load_config()
        cfg["memory"]["enabled"] = False
        store.save_config(cfg)
        self.assertEqual(self.mem.context(), [])
        self.mem.add("又问", "又答")
        self.assertEqual(len(self.mem._turns), 1)

    def test_persistence_across_instances(self):
        self.mem.add("持久化问题", "持久化回答")
        other = memory.Memory(os.path.join(self.dir, "mem.json"))
        ctx = other.context()
        self.assertEqual(ctx[0]["content"], "持久化问题")

    def test_clear(self):
        self.mem.add("问", "答")
        self.mem.clear()
        self.assertEqual(self.mem.context(), [])

    def test_remember_and_forget(self):
        self.mem.remember("我的项目叫 aurora")
        self.mem.remember("部署在 8080 端口")
        self.assertEqual(len(self.mem._turns), 2)
        msg = self.mem.forget("aurora")
        self.assertIn("1", msg)
        self.assertEqual(len(self.mem._turns), 1)

    def test_forget_all(self):
        self.mem.add("问", "答")
        self.assertIn("清空", self.mem.forget())

    def test_stats_shape(self):
        self.mem.add("问", "答")
        st = self.mem.stats()
        for k in ("enabled", "stored", "in_context", "ttl_minutes", "recent"):
            self.assertIn(k, st)
        self.assertEqual(st["stored"], 1)


class AgentMemoryWiringTest(unittest.TestCase):
    """记忆必须真的进到发给模型的 messages 里。"""

    def setUp(self):
        self._cfg = copy.deepcopy(store.load_config())
        self._backup = os.path.join(tempfile.mkdtemp(), "mem.json")
        if os.path.exists(memory.MEMORY_PATH):
            shutil.copy(memory.MEMORY_PATH, self._backup)
        memory.MEM.clear()
        cfg = copy.deepcopy(self._cfg)
        cfg["memory"].update({"enabled": True, "max_turns": 3})
        store.save_config(cfg)

    def tearDown(self):
        memory.MEM.clear()
        store.save_config(self._cfg)

    def test_history_reaches_llm(self):
        from unittest import mock
        from app import bridge, llm

        memory.MEM.add("我的项目叫 aurora", "好，记住了。")
        seen = {}

        def fake_chat(messages, cfg, tools=None, retries=2):
            seen["messages"] = messages
            return {"role": "assistant", "content": "好的"}

        with mock.patch.object(llm, "chat", side_effect=fake_chat), \
             mock.patch.object(llm, "stream_or_chat", side_effect=fake_chat):
            bridge.BRIDGE.run_agent("它叫什么来着", "manual")

        msgs = seen["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[-1]["content"], "它叫什么来着")
        joined = " ".join(m.get("content") or "" for m in msgs)
        self.assertIn("aurora", joined)

    def test_no_memory_when_disabled(self):
        from unittest import mock
        from app import bridge, llm

        cfg = store.load_config()
        cfg["memory"]["enabled"] = False
        store.save_config(cfg)
        memory.MEM.add("旧事", "旧答")

        seen = {}

        def fake_chat(messages, cfg, tools=None, retries=2):
            seen["messages"] = messages
            return {"role": "assistant", "content": "好的"}

        with mock.patch.object(llm, "chat", side_effect=fake_chat), \
             mock.patch.object(llm, "stream_or_chat", side_effect=fake_chat):
            bridge.BRIDGE.run_agent("新问题", "manual")
        joined = " ".join(m.get("content") or "" for m in seen["messages"])
        self.assertNotIn("旧事", joined)


if __name__ == "__main__":
    unittest.main()


class SceneSeedTest(unittest.TestCase):
    """示例场景用演示设备名，真机模式下不该预置。"""

    def setUp(self):
        import copy as _copy

        self._cfg = _copy.deepcopy(store.load_config())
        self.dir = tempfile.mkdtemp()
        from app import scenes

        self.scenes = scenes
        self._orig = scenes.SCENES_PATH
        scenes.SCENES_PATH = os.path.join(self.dir, "scenes.json")

    def tearDown(self):
        self.scenes.SCENES_PATH = self._orig
        store.save_config(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_real_mode_starts_empty(self):
        cfg = store.load_config()
        cfg["mi"]["mock"] = False
        cfg["miot"]["mock"] = False
        store.save_config(cfg)
        self.assertEqual(self.scenes.load(), {})

    def test_demo_mode_seeds_examples(self):
        cfg = store.load_config()
        cfg["mi"]["mock"] = True
        store.save_config(cfg)
        seeded = self.scenes.load()
        self.assertIn("回家", seeded)
        self.assertGreaterEqual(len(seeded), 1)
