"""会话导出：Markdown 结构与 JSON 结构。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import export

TURNS = [
    {"id": "t1", "ts": 1793516000.0, "source": "voice", "query": "跑一下测试",
     "answer": "挂了 2 个。", "ms": 1200, "spoken": True, "error": "",
     "tool_calls": [{"name": "run_shell", "args": {"cmd": "pytest -q"},
                     "result": "exit=1\n2 failed", "ms": 800}]},
    {"id": "t2", "ts": 1793516100.0, "source": "manual", "query": "看看日志",
     "answer": "有报错。", "ms": 300, "spoken": False, "error": "", "tool_calls": []},
]


class MarkdownTest(unittest.TestCase):
    def test_header_stats(self):
        md = export.to_markdown(TURNS, memory_turns=3)
        self.assertIn("# 小爱同学 · 大模型工作台", md)
        self.assertIn("回合数：2（语音 1，手动 1）", md)
        self.assertIn("工具调用：1 次", md)
        self.assertIn("记忆条数：3", md)

    def test_each_turn_rendered(self):
        md = export.to_markdown(TURNS)
        self.assertIn("跑一下测试", md)
        self.assertIn("挂了 2 个。", md)
        self.assertIn("run_shell", md)
        self.assertIn("pytest -q", md)
        self.assertIn("已播报", md)

    def test_tool_result_fenced(self):
        md = export.to_markdown(TURNS)
        self.assertIn("```text", md)
        self.assertIn("2 failed", md)

    def test_empty_history(self):
        md = export.to_markdown([])
        self.assertIn("还没有任何会话记录", md)

    def test_error_turn_flagged(self):
        turns = [dict(TURNS[0], error="LLM HTTP 500")]
        md = export.to_markdown(turns)
        self.assertIn("出错", md)
        self.assertIn("LLM HTTP 500", md)


class JsonTest(unittest.TestCase):
    def test_roundtrip(self):
        data = json.loads(export.to_json(TURNS, memory_turns=1))
        self.assertEqual(data["kind"], "xiaoai-console-history")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["memory_turns"], 1)
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(data["turns"][0]["query"], "跑一下测试")


if __name__ == "__main__":
    unittest.main()
