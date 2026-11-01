"""对话记忆：让 Agent 记得上一轮说过什么。

两层：
  短期 —— 最近 N 轮问答拼进 messages，受字数预算约束
  长期 —— 落盘到 data/memory.json，重启后继续记得

刻意只存「问 / 答」摘要，不存工具调用轨迹：工具结果动辄几千字符，
全塞回去会撑爆上下文，而且对「刚才说的那个报错」这类追问没有帮助。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from . import store

MEMORY_PATH = os.path.join(store.DATA_DIR, "memory.json")
MAX_STORED = 200


class Memory:
    def __init__(self, path: str = MEMORY_PATH) -> None:
        self.path = path
        self._turns: List[Dict[str, Any]] = []
        self._loaded = False

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            turns = data.get("turns") if isinstance(data, dict) else None
            if isinstance(turns, list):
                self._turns = [t for t in turns if isinstance(t, dict) and t.get("q")]
        except Exception:
            self._turns = []

    def _flush(self) -> None:
        if not store.load_config()["memory"].get("persist", True):
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"updated": time.time(), "turns": self._turns[-MAX_STORED:]},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            store.LOG.warn("记忆落盘失败: %s" % e)

    # ------------------------------------------------------------ 读写
    def add(self, query: str, answer: str, source: str = "voice") -> None:
        cfg = store.load_config()["memory"]
        if not cfg.get("enabled", True):
            return
        self._load()
        q = (query or "").strip()
        a = (answer or "").strip()
        if not q:
            return
        self._turns.append({"q": q[:500], "a": a[:800], "ts": time.time(), "src": source})
        self._turns = self._turns[-MAX_STORED:]
        self._flush()

    def clear(self) -> None:
        self._load()
        self._turns = []
        self._flush()

    def stats(self) -> Dict[str, Any]:
        self._load()
        cfg = store.load_config()["memory"]
        ctx = self.context()
        last = self._turns[-1]["ts"] if self._turns else 0
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "stored": len(self._turns),
            "in_context": len(ctx) // 2,
            "last_turn_age": int(time.time() - last) if last else None,
            "ttl_minutes": cfg.get("ttl_minutes", 30),
            "recent": self._turns[-8:],
        }

    def context(self) -> List[Dict[str, str]]:
        """拼成可直接塞进 messages 的片段；超出 TTL 或预算的自动丢弃。"""
        self._load()
        cfg = store.load_config()["memory"]
        if not cfg.get("enabled", True):
            return []
        ttl = float(cfg.get("ttl_minutes", 30) or 0) * 60
        max_turns = int(cfg.get("max_turns", 6) or 0)
        max_chars = int(cfg.get("max_chars", 2000) or 0)
        now = time.time()

        usable = [t for t in self._turns if not ttl or (now - float(t.get("ts", 0))) <= ttl]
        usable = usable[-max_turns:] if max_turns > 0 else []
        if not usable:
            return []

        picked: List[Dict[str, Any]] = []
        budget = max_chars
        for t in reversed(usable):
            cost = len(t.get("q", "")) + len(t.get("a", ""))
            if budget - cost < 0 and picked:
                break
            budget -= cost
            picked.append(t)
        picked.reverse()

        out: List[Dict[str, str]] = []
        for t in picked:
            out.append({"role": "user", "content": t.get("q", "")})
            if t.get("a"):
                out.append({"role": "assistant", "content": t.get("a", "")})
        return out

    def remember(self, text: str) -> str:
        """让模型主动记一条事实，跨轮/跨重启都能取回。"""
        t = (text or "").strip()
        if not t:
            raise ValueError("要记的内容为空")
        self.add("（记住）" + t, "好，记住了。", source="note")
        return "已记住：%s" % t

    def forget(self, keyword: str = "") -> str:
        self._load()
        k = (keyword or "").strip()
        if not k:
            n = len(self._turns)
            self.clear()
            return "记忆已清空（原有 %d 条）" % n
        before = len(self._turns)
        self._turns = [t for t in self._turns if k not in t.get("q", "")]
        self._flush()
        return "已删除 %d 条包含「%s」的记忆" % (before - len(self._turns), k)


MEM = Memory()
