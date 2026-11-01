"""播报文本压缩与唤醒词逻辑。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bridge import BRIDGE, to_speech


class SpeechTest(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(to_speech("测试通过。", 160), "测试通过。")

    def test_code_fence_removed(self):
        out = to_speech("结果如下：\n```python\nprint(1)\n```\n完成。", 160)
        self.assertNotIn("print(1)", out)
        self.assertIn("代码略", out)

    def test_markdown_stripped(self):
        out = to_speech("## 标题\n**重点**：`code`", 160)
        for ch in ("#", "**", "`"):
            self.assertNotIn(ch, out)

    def test_truncates_on_sentence_boundary(self):
        text = "第一句。" * 40
        out = to_speech(text, 40)
        self.assertLessEqual(len(out), 41)
        self.assertTrue(out.endswith("。") or out.endswith("…"))

    def test_empty_input_has_fallback(self):
        self.assertTrue(to_speech("", 160))


class WakeWordTest(unittest.TestCase):
    cfg = {"voice": {"wake_words": "小助手,跑一下", "continuous_window": 0}}

    def test_hit(self):
        self.assertTrue(BRIDGE._should_handle(self.cfg, "小助手，跑一下测试", 0))

    def test_miss(self):
        self.assertFalse(BRIDGE._should_handle(self.cfg, "今天天气怎么样", 0))

    def test_strip_removes_wake_word(self):
        self.assertEqual(BRIDGE._strip_wake(self.cfg, "小助手，跑一下测试"), "跑一下测试")

    def test_continuous_window(self):
        import time
        cfg = {"voice": {"wake_words": "小助手", "continuous_window": 10}}
        self.assertTrue(BRIDGE._should_handle(cfg, "继续", time.time()))


if __name__ == "__main__":
    unittest.main()
