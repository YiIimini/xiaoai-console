"""记忆工具的暴露与调用。"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import memory, store, tools


class MemoryToolTest(unittest.TestCase):
    def setUp(self):
        self._cfg = copy.deepcopy(store.load_config())
        self.dir = tempfile.mkdtemp()
        # 用独立文件，别动真实记忆
        self._orig_path = memory.MEM.path
        memory.MEM.path = os.path.join(self.dir, "mem.json")
        memory.MEM._loaded = False
        memory.MEM._turns = []

    def tearDown(self):
        memory.MEM.path = self._orig_path
        memory.MEM._loaded = False
        memory.MEM._turns = []
        store.save_config(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_tools_registered(self):
        names = [t["function"]["name"] for t in tools.TOOL_SPECS]
        self.assertIn("memory_remember", names)
        self.assertIn("memory_stats", names)

    def test_remember_then_stats(self):
        out = tools.dispatch({}, "memory_remember", {"text": "项目叫 aurora，部署在 8080"})
        self.assertIn("已记住", out)
        st = tools.dispatch({}, "memory_stats", {})
        self.assertIn("已存 1 条", st)
        self.assertIn("aurora", st)

    def test_remember_empty_rejected(self):
        out = tools.dispatch({}, "memory_remember", {"text": "   "})
        self.assertIn("失败", out)

    def test_remembered_fact_reaches_context(self):
        tools.dispatch({}, "memory_remember", {"text": "我用 pnpm 而不是 npm"})
        ctx = memory.MEM.context()
        joined = " ".join(m["content"] for m in ctx)
        self.assertIn("pnpm", joined)

    def test_forget_by_keyword_via_memory(self):
        memory.MEM.remember("记住 A")
        memory.MEM.remember("记住 B")
        msg = memory.MEM.forget("A")
        self.assertIn("1", msg)
        self.assertEqual(len(memory.MEM._turns), 1)


if __name__ == "__main__":
    unittest.main()
