"""控制台编码兜底。

Windows 上 stdout 默认是 cp1252/GBK，`print("中文")` 会抛 UnicodeEncodeError，
而服务启动日志正是中文——不处理的话进程会直接崩在这一行。
"""
from __future__ import annotations

import sys


def setup() -> None:
    """把标准输出/错误切到 UTF-8，失败就静默跳过（不影响主流程）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def safe_print(text: str, **kwargs) -> None:
    """任何情况下都不让打印异常冒泡到业务逻辑。"""
    try:
        print(text, **kwargs)
    except Exception:
        try:
            sys.__stdout__.write(str(text).encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
        except Exception:
            pass
