"""配置 / 历史 / 日志 的本地持久化。全部标准库，无第三方依赖。"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List

from .console import safe_print, setup as _setup_console

_setup_console()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
LOG_PATH = os.path.join(DATA_DIR, "console.log")

DEFAULT_SYSTEM_PROMPT = (
    "你是接在小爱音箱上的编程助手，用户用语音和你说话。\n"
    "规则：\n"
    "1) 需要看代码或跑命令时直接调用工具，不要反问。\n"
    "2) 最终回答必须口语化、不超过三句话，先说结论（通过/失败/已修改），"
    "再说关键细节（文件:行、错误摘要）。\n"
    "3) 不要朗读代码块、日志、表格；细节落盘由工作台负责。\n"
    "4) 危险操作（删除、强推、改系统配置）一律先说明再等确认。"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "mi": {
        "username": "",
        "password": "",
        "did": "",
        "dids": [],
        "hardware": "LX06",
        "token_path": "data/.mi.token",
        "poll_interval": 1.0,
        "mock": True,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_tool_rounds": 6,
        "timeout": 90,
        "stream": True,
    },
    "agent": {
        "repo": os.environ.get("XIAOAI_REPO") or os.path.dirname(ROOT),
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "allowed_cmds": "pytest,npm,pnpm,yarn,git,cargo,make,go,python3,python,ls,cat,head,tail,grep,rg,tree,node,uv,jest",
        "shell_timeout": 120,
        "max_output": 4000,
        "allow_write": True,
    },
    "miot": {
        "enabled": True,
        "mock": False,                 # 跟随 mi.mock 即可，一般不用单独开
        "allow_write": True,           # 关掉后米家只能读
        "blocked_keywords": "lock,doorlock,camera,ipc,gas,smoke,alarm,safe,doorbell,locker",
        "max_scene_steps": 50,
        "device_cache_seconds": 60,
    },
    "memory": {
        "enabled": True,
        "persist": True,
        "max_turns": 6,
        "max_chars": 2000,
        "ttl_minutes": 30,
    },
    "voice": {
        "stream_speech": True,
        "barge_in": True,
        "wake_words": "小助手,跑一下,调试,报错,改一下,看看代码,构建,测试",
        "speak": True,
        "tts_max_chars": 160,
        "ack": "收到",
        "ack_enabled": True,
        "continuous_window": 0,
    },
    "server": {"host": "127.0.0.1", "port": 8799},
}

# ---------------------------------------------------------------- 路径可移植
def resolve(path: str) -> str:
    """相对路径一律相对项目根解析，绝对路径原样返回（展开 ~）。"""
    if not path:
        return ""
    p = os.path.expanduser(str(path))
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(ROOT, p))


def portable_path(path: str) -> str:
    """落在项目内的路径改写成相对形式，换机后自动跟随。"""
    if not path:
        return path
    full = resolve(path)
    try:
        rel = os.path.relpath(full, ROOT)
    except ValueError:
        return path
    if rel.startswith(".."):
        return full          # 项目外，保持绝对
    return rel.replace(os.sep, "/")


def sanitize_for_import(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """导入外部配置时，把绑死上一台机器的路径重置为本机可用值。"""
    out = copy.deepcopy(cfg or {})
    mi = out.setdefault("mi", {})
    ag = out.setdefault("agent", {})
    mi["token_path"] = portable_path(mi.get("token_path") or "data/.mi.token")
    if not os.path.exists(resolve(mi.get("token_path"))):
        mi["token_path"] = "data/.mi.token"
    repo = ag.get("repo") or ""
    if not repo or not os.path.isdir(resolve(repo)):
        ag["repo"] = os.path.dirname(ROOT)
    return out


def export_config(include_secrets: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["mi"]["token_path"] = portable_path(cfg["mi"].get("token_path") or "data/.mi.token")
    cfg["agent"]["repo"] = portable_path(cfg["agent"].get("repo") or "")
    if not include_secrets:
        cfg["mi"]["password"] = ""
        cfg["llm"]["api_key"] = ""
    import platform as _pf
    import sys as _sys
    return {
        "kind": "xiaoai-console-config",
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from_machine": {"host": _pf.node(), "os": _pf.system(),
                         "python": _pf.python_version(), "arch": _pf.machine()},
        "include_secrets": include_secrets,
        "config": cfg,
    }


_lock = threading.RLock()


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _atomic_write(path: str, payload: Any, mode: int = 0o600) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            _atomic_write(CONFIG_PATH, DEFAULT_CONFIG)
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        cfg = _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
        # 旧版本把 token_path 存成了绝对路径，换机后会指向不存在的目录，这里顺手迁移
        tp = cfg["mi"].get("token_path") or ""
        if os.path.isabs(tp) and not os.path.exists(os.path.dirname(tp) or "/"):
            cfg["mi"]["token_path"] = "data/.mi.token"
        cfg["mi"]["token_path"] = portable_path(cfg["mi"]["token_path"] or "data/.mi.token")
        return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cfg = _deep_merge(load_config(), patch or {})
        _atomic_write(CONFIG_PATH, cfg)
        return cfg


PASSWORD_MASK = "********"


def mask_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(cfg)
    if out.get("mi", {}).get("password"):
        out["mi"]["password"] = PASSWORD_MASK
    if out.get("llm", {}).get("api_key"):
        key = out["llm"]["api_key"]
        out["llm"]["api_key"] = PASSWORD_MASK if len(key) < 12 else key[:6] + "…" + key[-4:]
        out["llm"]["api_key_masked"] = True
    return out


# ---------------------------------------------------------------- 日志
class LogBus:
    def __init__(self, size: int = 2000) -> None:
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._subs: List[Any] = []
        self._seq = 0

    def preload(self, path: str, limit: int = 200) -> None:
        """把历史日志文件的尾部读回内存，重启后日志面板不至于空白。"""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                rows = f.readlines()[-limit:]
        except Exception:
            return
        import re as _re

        pat = _re.compile(r"^(\d{2}:\d{2}:\d{2}) \[(info|warn|error|ok)\] (.*)$")
        today = time.localtime()
        with self._lock:
            for line in rows:
                line = line.rstrip("\n")
                if not line:
                    continue
                level, msg, ts = "info", line, time.time()
                m = pat.match(line)
                if m:
                    level, msg = m.group(2), m.group(3)
                    try:
                        hh, mm, ss = (int(x) for x in m.group(1).split(":"))
                        ts = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                          hh, mm, ss, 0, 0, -1))
                    except Exception:
                        ts = time.time()
                self._seq += 1
                self._buf.append({"seq": self._seq, "ts": ts, "level": level,
                                  "msg": msg, "replayed": True})

    def emit(self, level: str, msg: str, **extra: Any) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": time.time(), "level": level, "msg": msg}
            rec.update(extra)
            self._buf.append(rec)
            subs = list(self._subs)
        line = "%s [%s] %s" % (time.strftime("%H:%M:%S", time.localtime(rec["ts"])), level, msg)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if rec["seq"] % 400 == 0 and os.path.getsize(LOG_PATH) > 4 * 1024 * 1024:
                with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    keep = f.readlines()[-2000:]
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except Exception:
            pass
        safe_print(line, flush=True)
        for q in subs:
            try:
                q.put_nowait(rec)
            except Exception:
                pass
        return rec

    def info(self, msg: str, **e: Any) -> Dict[str, Any]:
        return self.emit("info", msg, **e)

    def warn(self, msg: str, **e: Any) -> Dict[str, Any]:
        return self.emit("warn", msg, **e)

    def error(self, msg: str, **e: Any) -> Dict[str, Any]:
        return self.emit("error", msg, **e)

    def ok(self, msg: str, **e: Any) -> Dict[str, Any]:
        return self.emit("ok", msg, **e)

    def tail(self, limit: int = 300, after: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._buf if r["seq"] > after]
        return rows[-limit:]

    def subscribe(self):
        import queue

        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


LOG = LogBus()
LOG.preload(LOG_PATH)


# ---------------------------------------------------------------- 历史
class History:
    def __init__(self, path: str = HISTORY_PATH, keep: int = 500) -> None:
        self.path = path
        self.keep = keep
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = self._read()

    def _read(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _flush(self) -> None:
        try:
            _atomic_write(self.path, self._items[-self.keep:], 0o644)
        except Exception:
            pass

    def add(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            turn.setdefault("id", "t%d" % int(time.time() * 1000))
            turn.setdefault("ts", time.time())
            self._items.append(turn)
            self._items = self._items[-self.keep:]
            self._flush()
            return turn

    def flush(self) -> None:
        with self._lock:
            self._flush()

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._items[-limit:])

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._flush()


HIST = History()
