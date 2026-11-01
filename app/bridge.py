"""桥接工作线程：轮询小爱对话 → 大模型 Agent（带工具）→ TTS 播报。"""
from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import llm, memory, store, tools
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


SENT_END = "。！？!?；;\n"


class SentenceStream:
    """把流式增量切成句子，够一句就交给播报。

    两个必须处理的细节：
      - 碎片太短（<min_len）先攒着，避免「好的。」这种半句反复起播
      - 代码块整体跳过。围栏可能被切在两个 chunk 里，所以要记住
        当前是否在代码块内，不能简单地按单个片段做正则替换
    """

    FENCE = "```"

    def __init__(self, min_len: int = 6) -> None:
        self.buf = ""
        self.min_len = min_len
        self.in_code = False

    def _emit(self, piece: str, out: List[str]) -> None:
        if not piece.strip():
            return
        clean = to_speech(CODE_FENCE.sub("", piece), 200)
        if len(clean) >= self.min_len:
            out.append(clean)

    def feed(self, text: str) -> List[str]:
        self.buf += text or ""
        out: List[str] = []
        while self.buf:
            fence = self.buf.find(self.FENCE)
            if self.in_code:
                if fence < 0:
                    self.buf = ""                 # 代码内容不播，直接丢
                    break
                self.buf = self.buf[fence + 3:]
                self.in_code = False
                continue

            cut = -1
            for i, ch in enumerate(self.buf):
                if ch in SENT_END:
                    cut = i
                    break

            if fence >= 0 and (cut < 0 or fence < cut):
                self._emit(self.buf[:fence], out)   # 围栏前的散文照常念
                self.buf = self.buf[fence + 3:]
                self.in_code = True
                continue
            if cut < 0:
                break
            piece, self.buf = self.buf[: cut + 1], self.buf[cut + 1:]
            self._emit(piece, out)
        return out

    def flush(self) -> List[str]:
        rest, self.buf = self.buf, ""
        if self.in_code or not rest.strip():
            # 未闭合的代码块不播；空残句更不能播，
            # 否则 to_speech("") 会吐出兜底句，凭空多一句废话
            self.in_code = False
            return []
        out: List[str] = []
        self._emit(rest, out)
        return out


class Bridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt = threading.Event()
        self.state = "stopped"          # stopped | starting | running | error
        self.speaker = None
        self._busy = False
        self._gen = 0
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
            "busy": self._busy,
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

                    # 上一轮还没结束时：能打断就打断，否则明确忽略，绝不排队重放
                    if self._busy:
                        if cfg["voice"].get("barge_in", True):
                            await self._barge_in()
                        else:
                            LOG.info("上一轮仍在处理，忽略: %s" % query[:30])
                            continue

                    self._gen += 1
                    self._busy = True
                    asyncio.ensure_future(self._handle_turn(cfg, query, self._gen))
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                LOG.error("轮询出错: %s" % self.stats["last_error"])
                await asyncio.sleep(min(10.0, interval * 3))
                continue
            await asyncio.sleep(interval)

    async def _barge_in(self) -> None:
        """新的一句话来了：停掉正在播报的内容，并作废当前这轮。"""
        self._gen += 1
        LOG.warn("检测到新的语音输入，打断当前回复")
        try:
            stop = getattr(self.speaker, "stop", None)
            if stop:
                await stop()
        except Exception as e:  # noqa: BLE001
            LOG.warn("停止播报失败: %s" % e)

    async def _handle_turn(self, cfg: Dict[str, Any], query: str, gen: int) -> None:
        """一轮完整处理。用 generation 作废旧轮，避免打断后旧结果又播出来。"""
        loop = asyncio.get_running_loop()
        vcfg = cfg["voice"]
        try:
            LOG.info("🎤 收到: %s" % query)
            if vcfg.get("ack_enabled") and (vcfg.get("ack") or "").strip():
                await self._say(vcfg, vcfg["ack"], gen)

            spoken_stream = False
            on_delta = None
            drain_task = None
            splitter = None
            sent_q: "queue.Queue[Optional[str]]" = queue.Queue()

            if vcfg.get("speak", True) and vcfg.get("stream_speech", True):
                splitter = SentenceStream()

                def on_delta(text: str, _sp=splitter, _q=sent_q, _g=gen) -> None:
                    if _g != self._gen:
                        return                      # 已被打断，丢弃旧轮的增量
                    for sent in _sp.feed(text):
                        _q.put(sent)

                drain_task = asyncio.ensure_future(self._drain_sentences(sent_q, vcfg, gen))

            turn = await loop.run_in_executor(None, self.run_agent, query, "voice", False, on_delta)

            if gen != self._gen:                    # 期间被新语音打断
                if drain_task is not None:
                    sent_q.put(None)
                    await drain_task
                LOG.info("该轮已被打断，结果不播报")
                return

            if drain_task is not None:
                for sent in splitter.flush():
                    sent_q.put(sent)
                sent_q.put(None)
                spoken_stream = await drain_task

            self.stats["turns"] += 1
            self.stats["last_query"] = query
            self.stats["last_answer"] = turn.get("answer", "")
            if spoken_stream:
                turn["spoken"] = True
                HIST.flush()
            elif vcfg.get("speak", True):
                if await self._say(vcfg, turn.get("answer", ""), gen):
                    turn["spoken"] = True
                    HIST.flush()
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            LOG.error("处理失败: %s" % self.stats["last_error"])
        finally:
            if gen == self._gen:
                self._busy = False

    async def _drain_sentences(self, q: "queue.Queue[Optional[str]]", voice_cfg: Dict[str, Any],
                               gen: int = 0) -> bool:
        """边生成边播报：队列里有句子就送音箱，收到 None 结束。被打断即停。"""
        spoke = False
        while True:
            try:
                sent = q.get_nowait()
            except queue.Empty:
                if gen and gen != self._gen:
                    return spoke
                await asyncio.sleep(0.05)
                continue
            if sent is None:
                break
            if gen and gen != self._gen:
                return spoke
            if await self._say(voice_cfg, sent):
                spoke = True
        return spoke

    async def _say(self, voice_cfg: Dict[str, Any], text: str, gen: int = 0) -> bool:
        if not (text or "").strip():
            return False
        if gen and gen != self._gen:
            return False
        speech = to_speech(text, int(voice_cfg.get("tts_max_chars", 160)))
        if not speech.strip():
            return False
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
    def run_agent(self, query: str, source: str = "manual", speak: bool = False,
                  on_delta: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
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
        # 记忆：把最近几轮问答拼在系统提示之后、本轮提问之前
        mem_ctx = memory.MEM.context()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": agent_cfg.get("system_prompt") or ""},
        ]
        if mem_ctx:
            messages.append({
                "role": "system",
                "content": "以下是最近的对话，可作为上下文参考；与当前问题无关就忽略。",
            })
            messages.extend(mem_ctx)
        messages.append({"role": "user", "content": query})
        try:
            rounds = int(llm_cfg.get("max_tool_rounds", 6))
            for _ in range(max(1, rounds)):
                if on_delta is not None and llm_cfg.get("stream", True):
                    msg = llm.stream_or_chat(messages, llm_cfg, tools.TOOL_SPECS, on_delta)
                else:
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
        if not turn["error"]:
            memory.MEM.add(query, turn["answer"], source)
        return turn


def _short(args: Dict[str, Any]) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 80 else s[:77] + "…"


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()
    return (line[0] if line else "")[:100]


BRIDGE = Bridge()
