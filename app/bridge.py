"""桥接工作线程：轮询小爱对话 → 大模型 Agent（带工具）→ TTS 播报。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional

from . import llm, store, tools
from .mi import MiError, make_speaker

LOG = store.LOG
HIST = store.HIST

CODE_FENCE = re.compile(r"```.*?```", re.S)
SPEAK_CLEAN = re.compile(r"[#*`>|_~]+")


def to_speech(text: str, limit: int = 160) -> str:
    """把 Markdown 回答压成适合朗读的一小段。"""
    if not text:
        return "我这边没有拿到结果。"
    t = CODE_FENCE.sub("（代码略）", text)
    t = SPEAK_CLEAN.sub("", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in ("。", "；", "，", ".", ";", ","):
        idx = cut.rfind(sep)
        if idx > limit * 0.5:
            return cut[: idx + 1]
    return cut + "…"


class Bridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt = threading.Event()
        self.state = "stopped"          # stopped | starting | running | error
        self.speaker = None
        self.stats: Dict[str, Any] = {
            "turns": 0,
            "polls": 0,
            "errors": 0,
            "started_at": None,
            "last_query": "",
            "last_answer": "",
            "last_error": "",
            "spoken": [],
        }

    # ------------------------------------------------------------ 控制
    @property
    def running(self) -> bool:
        return self.state in ("starting", "running")

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "running": self.running,
            "uptime": int(time.time() - self.stats["started_at"]) if self.stats["started_at"] else 0,
            "stats": {k: v for k, v in self.stats.items() if k != "spoken"},
            "spoken": self.stats["spoken"][-10:],
        }

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"ok": True, "state": self.state, "msg": "已在运行"}
            cfg = store.load_config()
            self._stop_evt.clear()
            self.state = "starting"
            self.stats["last_error"] = ""
            self._thread = threading.Thread(target=self._run, name="xiaoai-bridge", daemon=True)
            self._thread.start()
            LOG.info("桥接启动中（模式：%s）" % ("演示" if cfg["mi"].get("mock") else "真机"))
            return {"ok": True, "state": self.state}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._stop_evt.set()
            loop = self._loop
            if loop and loop.is_running():
                try:
                    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._shutdown(loop)))
                except Exception:
                    pass
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=6)
            self._thread = None
            self.state = "stopped"
            LOG.info("桥接已停止")
            return {"ok": True, "state": self.state}

    async def _shutdown(self, loop) -> None:
        await asyncio.sleep(0.05)
        loop.stop()

    # ------------------------------------------------------------ 线程主体
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            LOG.error("桥接异常退出: %s" % self.stats["last_error"])
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            if self.state != "error":
                self.state = "stopped"

    @staticmethod
    def _fingerprint(cfg: Dict[str, Any]) -> str:
        """接入身份指纹：账号 / 设备 / 模式任一变化就重新连接。"""
        import hashlib

        mi = cfg["mi"]
        raw = "|".join([
            str(mi.get("username") or ""),
            hashlib.sha256((mi.get("password") or "").encode()).hexdigest()[:16],
            str(mi.get("did") or ""),
            str(mi.get("hardware") or ""),
            str(mi.get("token_path") or ""),
            "mock" if mi.get("mock") else "real",
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def _connect(self, cfg: Dict[str, Any]) -> bool:
        """（重新）建立音箱连接；成功后更新指纹。"""
        self.speaker = make_speaker(cfg["mi"], LOG)
        try:
            await self.speaker.connect()
        except MiError as e:
            self.state = "error"
            self.stats["last_error"] = str(e)
            LOG.error("连接失败: %s" % e)
            return False
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            LOG.error("连接异常: %s" % self.stats["last_error"])
            return False
        self._fp = self._fingerprint(cfg)
        return True

    async def _main(self) -> None:
        cfg = store.load_config()
        self._fp = ""
        if not await self._connect(cfg):
            return

        self.state = "running"
        self.stats["started_at"] = time.time()
        if getattr(self.speaker, "is_mock", False):
            if (cfg["mi"].get("username") or "").strip():
                LOG.warn("当前是演示模式：已填的小米账号不会用于连接真机。"
                         "要接管音箱，请到「音箱接入」取消勾选演示模式后重启")
            else:
                LOG.info("演示模式：用「总览 → 模拟发问」测试链路，不需要真实音箱")
        LOG.ok("桥接就绪，开始轮询")

        seen: List[str] = []
        last_hit = 0.0
        # 启动基线：早于此刻的旧对话一律跳过，避免重启后重复应答
        baseline = self.stats["started_at"] - 2.0
        loop = asyncio.get_running_loop()

        while not self._stop_evt.is_set():
            cfg = store.load_config()           # 每轮热更新配置
            interval = max(0.5, float(cfg["mi"].get("poll_interval", 1.0)))
            if self._fingerprint(cfg) != self._fp:
                LOG.warn("接入配置已变更（账号/设备/模式），正在重新连接…")
                if not await self._connect(cfg):
                    await asyncio.sleep(max(3.0, interval))
                continue
            try:
                batch = await self.speaker.poll()
                self.stats["polls"] += 1
                for item in batch:
                    rid = item.get("request_id") or str(item.get("ts"))
                    if rid in seen:
                        continue
                    seen.append(rid)
                    seen[:] = seen[-200:]
                    q = item.get("question") or ""
                    if not q:
                        continue
                    if item.get("ts") and item["ts"] < baseline:
                        continue
                    if not self._should_handle(cfg, q, last_hit):
                        LOG.info("忽略（无唤醒词）: %s" % q[:40])
                        continue
                    last_hit = time.time()
                    query = self._strip_wake(cfg, q)
                    LOG.info("🎤 收到: %s" % query)
                    vcfg = cfg["voice"]
                    if vcfg.get("ack_enabled") and (vcfg.get("ack") or "").strip():
                        await self._say(vcfg, vcfg["ack"])
                    turn = await loop.run_in_executor(None, self.run_agent, query, "voice")
                    self.stats["turns"] += 1
                    self.stats["last_query"] = query
                    self.stats["last_answer"] = turn.get("answer", "")
                    if cfg["voice"].get("speak", True):
                        if await self._say(cfg["voice"], turn.get("answer", "")):
                            turn["spoken"] = True
                            HIST.flush()
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                LOG.error("轮询出错: %s" % self.stats["last_error"])
                await asyncio.sleep(min(10.0, interval * 3))
                continue
            await asyncio.sleep(interval)

    async def _say(self, voice_cfg: Dict[str, Any], text: str) -> bool:
        speech = to_speech(text, int(voice_cfg.get("tts_max_chars", 160)))
        try:
            await self.speaker.speak(speech)
            self.stats["spoken"].append({"ts": time.time(), "text": speech})
            self.stats["spoken"] = self.stats["spoken"][-20:]
            return True
        except Exception as e:  # noqa: BLE001
            LOG.warn("播报失败: %s" % e)
            return False

    # ------------------------------------------------------------ 唤醒词
    @staticmethod
    def _words(cfg: Dict[str, Any]) -> List[str]:
        raw = cfg["voice"].get("wake_words") or ""
        return [w.strip() for w in re.split(r"[,，\s]+", raw) if w.strip()]

    def _should_handle(self, cfg: Dict[str, Any], q: str, last_hit: float) -> bool:
        window = float(cfg["voice"].get("continuous_window", 0) or 0)
        if window > 0 and (time.time() - last_hit) <= window:
            return True
        return any(w in q for w in self._words(cfg))

    def _strip_wake(self, cfg: Dict[str, Any], q: str) -> str:
        out = q
        for w in sorted(self._words(cfg), key=len, reverse=True):
            if w in out:
                out = out.replace(w, "", 1).strip(" ，,。.!！?？")
                break
        return out or q

    # ------------------------------------------------------------ Agent 单轮
    def run_agent(self, query: str, source: str = "manual", speak: bool = False) -> Dict[str, Any]:
        cfg = store.load_config()
        agent_cfg = cfg["agent"]
        llm_cfg = cfg["llm"]
        started = time.time()
        turn: Dict[str, Any] = {
            "source": source,
            "query": query,
            "answer": "",
            "tool_calls": [],
            "ms": 0,
            "error": "",
            "spoken": False,
        }
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": agent_cfg.get("system_prompt") or ""},
            {"role": "user", "content": query},
        ]
        try:
            rounds = int(llm_cfg.get("max_tool_rounds", 6))
            for _ in range(max(1, rounds)):
                msg = llm.chat(messages, llm_cfg, tools.TOOL_SPECS)
                calls = msg.get("tool_calls") or []
                if not calls:
                    turn["answer"] = (msg.get("content") or "").strip()
                    break
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": calls,
                })
                for tc in calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    t0 = time.time()
                    result = tools.dispatch(agent_cfg, name, args)
                    call = {
                        "name": name,
                        "args": args,
                        "result": result[-1500:],
                        "ms": int((time.time() - t0) * 1000),
                    }
                    turn["tool_calls"].append(call)
                    LOG.info("🔧 %s(%s) → %s" % (name, _short(args), _first_line(result)))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or name,
                        "content": result,
                    })
            else:
                turn["answer"] = "工具调用超过上限，已中断。"
        except llm.LLMError as e:
            turn["error"] = str(e)
            turn["answer"] = "大模型调用失败：%s" % e
            LOG.error("LLM 失败: %s" % e)
        except Exception as e:  # noqa: BLE001
            turn["error"] = "%s: %s" % (type(e).__name__, e)
            turn["answer"] = "处理出错：%s" % turn["error"]
            LOG.error("Agent 异常: %s" % turn["error"])

        turn["ms"] = int((time.time() - started) * 1000)
        HIST.add(turn)
        return turn


def _short(args: Dict[str, Any]) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 80 else s[:77] + "…"


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()
    return (line[0] if line else "")[:100]


BRIDGE = Bridge()
