"""会话导出：把调试过程整段导成 Markdown / JSON。

导出的是「发生过什么」的完整证据：每轮的时间、耗时、工具调用参数与结果、
是否被播报。用来复盘「它当时到底干了什么」比翻日志快得多。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List


def _ts(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "—"


def _clock(t: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "—"


def to_markdown(turns: List[Dict[str, Any]], memory_turns: int = 0) -> str:
    lines: List[str] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    total = len(turns)
    tools = sum(len(t.get("tool_calls") or []) for t in turns)
    errors = sum(1 for t in turns if t.get("error"))
    voice = sum(1 for t in turns if t.get("source") == "voice")
    avg = int(sum(t.get("ms") or 0 for t in turns) / total) if total else 0

    lines += [
        "# 小爱同学 · 大模型工作台 —— 会话导出",
        "",
        "- 导出时间：%s" % now,
        "- 回合数：%d（语音 %d，手动 %d）" % (total, voice, total - voice),
        "- 工具调用：%d 次" % tools,
        "- 平均耗时：%d ms" % avg,
        "- 出错回合：%d" % errors,
        "- 记忆条数：%d" % memory_turns,
        "",
        "---",
        "",
    ]

    if not turns:
        lines += ["（还没有任何会话记录）", ""]
        return "\n".join(lines)

    for i, t in enumerate(turns, 1):
        src = "语音" if t.get("source") == "voice" else "手动"
        flags = []
        if t.get("spoken"):
            flags.append("已播报")
        if t.get("error"):
            flags.append("出错")
        head = "## %d. [%s] %s · %s ms" % (i, src, _clock(t.get("ts")), t.get("ms") or 0)
        if flags:
            head += " （%s）" % "、".join(flags)
        lines.append(head)
        lines.append("")
        lines.append("**问**")
        lines.append("")
        lines.append("> " + (t.get("query") or "—").replace("\n", "\n> "))
        lines.append("")
        lines.append("**答**")
        lines.append("")
        answer = t.get("answer") or "—"
        lines.append("```text")
        lines.append(answer.strip())
        lines.append("```")
        lines.append("")

        calls = t.get("tool_calls") or []
        if calls:
            lines.append("<details><summary>工具调用 %d 次</summary>" % len(calls))
            lines.append("")
            for c in calls:
                lines.append("- `%s` · %s ms" % (c.get("name"), c.get("ms", 0)))
                args = c.get("args")
                try:
                    args_s = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
                except (TypeError, ValueError):
                    args_s = str(args)
                lines.append("  - 参数：`%s`" % args_s.replace("`", "'")[:600])
                lines.append("  - 结果：")
                lines.append("")
                lines.append("    ```text")
                for ln in (c.get("result") or "").splitlines()[:40]:
                    lines.append("    " + ln)
                lines.append("    ```")
                lines.append("")
            lines.append("</details>")
            lines.append("")

        if t.get("error"):
            lines.append("**错误**：`%s`" % t["error"])
            lines.append("")

        lines.append("---")
        lines.append("")

    lines += [
        "<!-- 由 xiaoai-console 生成 · %s -->" % now,
        "",
    ]
    return "\n".join(lines)


def to_json(turns: List[Dict[str, Any]], memory_turns: int = 0) -> str:
    return json.dumps({
        "kind": "xiaoai-console-history",
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(turns),
        "memory_turns": memory_turns,
        "turns": turns,
    }, ensure_ascii=False, indent=2)
