"""控制台编码回归测试：Windows 非 UTF-8 控制台下不能崩。"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import console


class ConsoleTest(unittest.TestCase):
    def test_safe_print_survives_broken_stream(self):
        class Broken(io.TextIOBase):
            def write(self, *a, **k):
                raise UnicodeEncodeError("charmap", "工作台", 0, 1, "boom")

        with mock.patch.object(sys, "stdout", Broken()):
            console.safe_print("工作台已启动")   # 不应抛异常

    def test_setup_is_idempotent(self):
        console.setup()
        console.setup()

    def test_setup_switches_cp1252_stream_to_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        with mock.patch.object(sys, "stdout", stream):
            console.setup()
            console.safe_print("工作台")
        self.assertIn("工作台", raw.getvalue().decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
