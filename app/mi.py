"""小米音箱接入层：miservice 适配 + 演示模式（Mock）。

miservice 3.x 关键 API：
    MiAccount(session, username, password, token_store=...)
    await account.login('micoapi')
    mina = MiNAService(account)
    await mina.device_list()            -> [{deviceID, hardware, name, ...}]
    await mina.get_latest_ask(deviceId) -> [{request_id, timestamp_ms, response:{answer:[{question,content}]}}]
    await mina.text_to_speech(deviceId, text) -> bool
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional

from . import store

# ---------------------------------------------------------------- 验证码中转
# 首次登录时小米可能要求验证码；工作台弹框输入，这里做跨线程交接。
class OtpSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.method: Optional[str] = None
        self.code: Optional[str] = None
        self._evt = threading.Event()

    def reset(self) -> None:
        with self._lock:
            self.method = None
            self.code = None
            self._evt.clear()

    def request(self, method: str) -> str:
        with self._lock:
            self.method = method
            self.code = None
            self._evt.clear()
        self._evt.wait(timeout=180)
        with self._lock:
            return self.code or ""

    def provide(self, code: str) -> None:
        with self._lock:
            self.code = code
            self._evt.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"waiting": self.method is not None and not self._evt.is_set(), "method": self.method}


OTP = OtpSlot()


class MiError(RuntimeError):
    pass


def miservice_available() -> bool:
    try:
        import miservice  # noqa: F401

        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 真机
class RealSpeaker:
    is_mock = False

    def __init__(self, mi_cfg: Dict[str, Any], log) -> None:
        self.cfg = mi_cfg
        self.log = log
        self.session = None
        self.mina = None
        self.dids: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        if not miservice_available():
            raise MiError("未安装 miservice，请运行 ./start.sh 或 pip install miservice")
        from miservice import MiAccount, MiNAService  # 延迟导入

        user = (self.cfg.get("username") or "").strip()
        pwd = (self.cfg.get("password") or "").strip()
        if not user or not pwd:
            raise MiError("未填写小米账号/密码")

        async def otp_callback(method: str) -> str:
            self.log.warn("小米登录需要验证码（方式：%s），请在工作台右上角输入" % method)
            loop = __import__("asyncio").get_running_loop()
            return await loop.run_in_executor(None, OTP.request, method)

        self.session = MiAccount(
            None, user, pwd,
            token_store=store.resolve(self.cfg.get("token_path") or "data/.mi.token"),
            otp_callback=otp_callback,
        )
        ok = await self.session.login("micoapi")
        if not ok:
            raise MiError("小米登录失败（账号/密码/验证码）")
        self.mina = MiNAService(self.session)
        devs = await self.mina.device_list() or []
        self.dids = devs
        self.log.ok("小米登录成功，发现设备 %d 台" % len(devs))

    async def devices(self) -> List[Dict[str, Any]]:
        if self.mina is None:
            await self.connect()
        return await self.mina.device_list() or []

    def resolve_did(self) -> str:
        did = (self.cfg.get("did") or "").strip()
        if did:
            return did
        if self.dids:
            return str(self.dids[0].get("deviceID") or "")
        raise MiError("未指定设备 did，且未发现任何小爱音箱")

    async def poll(self) -> List[Dict[str, Any]]:
        if self.mina is None:
            await self.connect()
        did = self.resolve_did()
        raw = await self.mina.get_latest_ask(did) or []
        out: List[Dict[str, Any]] = []
        for item in raw:
            answers = (item.get("response") or {}).get("answer") or []
            q = ""
            for a in answers:
                q = (a.get("question") or "").strip()
                if q:
                    break
            if not q:
                continue
            out.append({
                "request_id": item.get("request_id") or "",
                "ts": (item.get("timestamp_ms") or 0) / 1000.0,
                "question": q,
            })
        return out

    async def speak(self, text: str) -> bool:
        if self.mina is None:
            await self.connect()
        return await self.mina.text_to_speech(self.resolve_did(), text)


# ---------------------------------------------------------------- 演示模式
class MockSpeaker:
    """不碰真机，用来验证「轮询 → 大模型 → 工具 → 播报」整条链路。"""

    is_mock = True

    def __init__(self, mi_cfg: Dict[str, Any], log) -> None:
        self.cfg = mi_cfg
        self.log = log
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.spoken: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        self.log.ok("演示模式已连接（不会访问小米云）")

    async def devices(self) -> List[Dict[str, Any]]:
        return [{"deviceID": "mock-0001", "hardware": "MOCK", "name": "演示音箱"}]

    def resolve_did(self) -> str:
        return (self.cfg.get("did") or "mock-0001").strip() or "mock-0001"

    def inject(self, text: str) -> None:
        self._q.put({
            "request_id": "mock-%d" % int(time.time() * 1000),
            "ts": time.time(),
            "question": text,
        })

    async def poll(self) -> List[Dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    async def speak(self, text: str) -> bool:
        self.spoken.append({"ts": time.time(), "text": text})
        self.spoken = self.spoken[-50:]
        self.log.info("🔊 播报: %s" % text)
        return True


def make_speaker(mi_cfg: Dict[str, Any], log):
    if mi_cfg.get("mock") or not miservice_available():
        return MockSpeaker(mi_cfg, log)
    return RealSpeaker(mi_cfg, log)
