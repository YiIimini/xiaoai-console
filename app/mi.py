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

import json
import os
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional

from . import store

# ---------------------------------------------------------------- 验证码中转
# 首次登录时小米可能要求验证码；工作台弹框输入，这里做跨线程交接。
class OtpSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.since: float = 0.0
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
            self.since = time.time()
            self.code = None
            self._evt.clear()
        self._evt.wait(timeout=180)
        with self._lock:
            return self.code or ""

    def provide(self, code: str) -> None:
        with self._lock:
            self.code = code
            self._evt.set()

    def cancel(self) -> None:
        """用户放弃输入：立刻结束等待，不要挂在后台占 3 分钟。"""
        with self._lock:
            self.code = ""
            self._evt.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "waiting": self.method is not None and not self._evt.is_set(),
                "method": self.method,
                "since": self.since,
            }


OTP = OtpSlot()


class MiError(RuntimeError):
    pass


def miservice_available() -> bool:
    try:
        import miservice  # noqa: F401

        return True
    except Exception:
        return False




# ---------------------------------------------------------------- 登录与风控
# 参考成熟实现（micloud / python-miio / Home Assistant xiaomi_miot）的做法：
#   1) token 优先，只有缺 sid 才登录
#   2) deviceId 必须稳定——每次随机会被小米当成新设备，直接触发短信验证
#   3) 登录串行，同一时刻只允许一个在飞
#   4) 冷却要落盘，重启不重置
#   5) 采纳小米返回的 wt 作为冷却时长

COOLDOWN_FILE = os.path.join(store.DATA_DIR, "mi-cooldown.json")
DEVICE_FILE = os.path.join(store.DATA_DIR, ".mi.device")

_COOLDOWN: Dict[str, Any] = {"until": 0.0, "reason": ""}

#: 按事件循环分锁。曾用 threading.Lock，但同一循环里的两个协程会在
#: acquire 上互相卡死（阻塞的是整个循环），所以必须用 asyncio.Lock。
_LOGIN_LOCKS: Dict[int, Any] = {}


def _login_lock():
    import asyncio as _aio

    loop = _aio.get_running_loop()
    key = id(loop)
    lock = _LOGIN_LOCKS.get(key)
    if lock is None:
        lock = _aio.Lock()
        if len(_LOGIN_LOCKS) > 8:        # 循环对象不多，兜个上限
            _LOGIN_LOCKS.clear()
        _LOGIN_LOCKS[key] = lock
    return lock

RISK_MARKERS = (
    ("用户行为被限制", "小米风控拦截：短时间内登录或验证码请求过多。"),
    ("20024", "小米风控拦截：短时间内登录或验证码请求过多。"),
    ("Failed to send OTP", "小米拒绝下发验证码——通常是登录太频繁触发了风控。"),
    ("client withdraw", "小米作废了当前会话（client withdraw）——通常紧跟风控之后。"),
    ('"code":-127', "小米作废了当前会话（client withdraw）——通常紧跟风控之后。"),
)
RISK_NEEDLES = {"用户行为被限制", "20024", "Failed to send OTP", "client withdraw", '"code":-127'}


# ---------------------------------------------------------------- 直连小米
_DIRECT_READY = False


def install_direct_transport() -> bool:
    """让小米请求绕过系统代理，强制直连。

    为什么必须这么做：miservice 用 urllib，而 urllib 的 build_opener 默认会
    读系统代理（macOS 上读 scutil --proxy）。开了 VPN/代理时，登录请求会从
    海外节点发出——对国内账号就是「异地登录」，直接触发二次验证和短信，
    反复失败后变成 20024 用户行为被限制。

    biohttp 每个请求都自己 build_opener，所以把它的 build_opener 换成
    注入了空 ProxyHandler 的版本即可，作用域只限 miservice，不影响大模型请求。
    """
    global _DIRECT_READY
    if _DIRECT_READY:
        return True
    if not store.load_config()["mi"].get("bypass_proxy", True):
        return False
    try:
        import miservice.biohttp as bio
        import urllib.request as _rq
    except Exception:
        return False

    orig = bio.urlrequest.build_opener

    class _Shim:
        pass

    shim = _Shim()
    for attr in dir(bio.urlrequest):
        try:
            setattr(shim, attr, getattr(bio.urlrequest, attr))
        except Exception:
            pass

    def build_opener(*handlers):
        return orig(_rq.ProxyHandler({}), *handlers)   # 空字典＝不走任何代理

    shim.build_opener = build_opener
    bio.urlrequest = shim
    _DIRECT_READY = True
    return True


def stable_device_id() -> str:
    """固定设备指纹：随机 deviceId 会让每次登录都被判定为「陌生设备」。"""
    try:
        with open(DEVICE_FILE, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass
    import uuid

    val = uuid.uuid4().hex[:16].upper()
    try:
        os.makedirs(os.path.dirname(DEVICE_FILE), exist_ok=True)
        with open(DEVICE_FILE, "w", encoding="utf-8") as f:
            f.write(val)
        os.chmod(DEVICE_FILE, 0o600)
    except OSError:
        pass
    return val


def reset_fingerprint() -> Dict[str, Any]:
    """清掉 token 与设备指纹，让下一次登录看起来来自一台全新设备。

    手机 App 正常、只有这台机器被 20024 时，这是最有效的一步——
    小米限制的是「这个客户端」，不是账号。
    """
    removed = []
    for path, label in (
        (store.resolve("data/.mi.token"), "登录 token"),
        (DEVICE_FILE, "设备指纹"),
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(label)
        except OSError as e:
            raise MiError("删除 %s 失败：%s" % (label, e))
    _COOLDOWN.update({"until": 0.0, "reason": ""})
    _save_cooldown()
    return {"removed": removed}


def _load_cooldown() -> None:
    try:
        with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if float(data.get("until") or 0) > time.time():
            _COOLDOWN.update({"until": float(data["until"]), "reason": data.get("reason") or ""})
    except Exception:
        pass


def _save_cooldown() -> None:
    try:
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump({"until": _COOLDOWN["until"], "reason": _COOLDOWN["reason"]}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def in_cooldown() -> str:
    """冷却期内返回原因，否则空串。"""
    if _COOLDOWN["until"] and time.time() >= _COOLDOWN["until"]:
        _COOLDOWN.update({"until": 0.0, "reason": ""})
        _save_cooldown()
    if time.time() < _COOLDOWN["until"]:
        left = int(_COOLDOWN["until"] - time.time())
        return "小米风控冷却中（还需 %d 分 %d 秒）：%s" % (left // 60, left % 60, _COOLDOWN["reason"])
    return ""


def set_cooldown(seconds: int, reason: str) -> None:
    _COOLDOWN["until"] = time.time() + max(0, seconds)
    _COOLDOWN["reason"] = reason
    _save_cooldown()


def _wt_seconds(text: str) -> int:
    """小米在风控错误里会给 wt（等待权重），能解析就采纳，夹在 10–60 分钟。"""
    m = re.search(r"'wt':\s*(\d+)", text) or re.search(r'"wt":\s*(\d+)', text)
    if not m:
        return 600
    try:
        return max(600, min(3600, int(m.group(1))))
    except ValueError:
        return 600


def friendly_mi_error(exc: Exception) -> "MiError":
    """把小米那串 JSON 翻成能照做的话，并识别风控。"""
    text = str(exc)
    for marker, hint in RISK_MARKERS:
        if marker in text:
            secs = _wt_seconds(text)
            set_cooldown(secs, hint)
            return MiError(
                hint + "\n"
                "已经停止重试，%d 分钟内不再登录。\n"
                "如果手机上的米家 App 能正常用，说明账号本身没问题，是这台设备的登录指纹被限制了：\n"
                "1) 先等冷却走完；仍不通就点「重置登录指纹」，让小米把它当成一台全新设备\n"
                "2) 重置后只点一次「测试登录」——小米会发一条验证码，填完即通\n"
                "3) 通过后 token 缓存在 data/.mi.token，之后不再重复登录\n"
                "4) 若重置后依旧 20024，说明限制是按 IP / 时间窗生效的，隔几小时再试\n\n"
                "原始错误：" % (secs // 60) + text[:200]
            )
    return MiError(text)


async def ensure_login(account, sid: str) -> None:
    """只有缓存里没有该 sid 时才真正登录，且同一时刻只允许一个登录在飞。

    MiAccount.login() 每次都会走完整的 serviceLogin，可能触发验证码和风控。
    原先是无条件调用，于是「发现设备」「测试登录」「桥接重连」每点一次就登录一次，
    加上 token 文件被删，几下就把账号送进风控。
    """
    if account.token is None and getattr(account, "token_store", None) is not None:
        try:
            account.token = await account.token_store.load_token()
        except Exception:
            account.token = None

    # deviceId 必须稳定，否则每次登录都像新设备
    if not account.token:
        account.token = {}
    account.token.setdefault("deviceId", stable_device_id())

    if sid in account.token:
        return                       # 复用缓存，零网络请求

    async with _login_lock():
        # 双重检查：等锁期间别人可能已经登录成功
        if account.token and sid in account.token:
            return
        await account.login(sid)


_load_cooldown()


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

        install_direct_transport()
        self.session = MiAccount(
            None, user, pwd,
            token_store=store.resolve(self.cfg.get("token_path") or "data/.mi.token"),
            otp_callback=otp_callback,
        )
        blocked = in_cooldown()
        if blocked:
            raise MiError(blocked)
        try:
            await ensure_login(self.session, "micoapi")
        except MiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise friendly_mi_error(e)
        self.mina = MiNAService(self.session)
        devs = await self.mina.device_list() or []
        self.dids = devs
        self.log.ok("小米登录成功，发现设备 %d 台" % len(devs))

    async def devices(self) -> List[Dict[str, Any]]:
        if self.mina is None:
            await self.connect()
        return await self.mina.device_list() or []

    def resolve_dids(self) -> List[str]:
        """接管的音箱列表：优先 mi.dids，其次 mi.did，最后账号下第一台。"""
        out = [str(d).strip() for d in (self.cfg.get("dids") or []) if str(d).strip()]
        if out:
            return out
        did = (self.cfg.get("did") or "").strip()
        if did:
            return [did]
        if self.dids:
            return [str(self.dids[0].get("deviceID") or "")]
        raise MiError("未指定设备 did，且未发现任何小爱音箱")

    def resolve_did(self) -> str:
        return self.resolve_dids()[0]

    def name_of(self, did: str) -> str:
        for d in self.dids or []:
            if str(d.get("deviceID") or "") == did:
                return d.get("name") or did
        return did

    async def poll(self) -> List[Dict[str, Any]]:
        if self.mina is None:
            await self.connect()
        out: List[Dict[str, Any]] = []
        for did in self.resolve_dids():
            try:
                raw = await self.mina.get_latest_ask(did) or []
            except Exception as e:  # noqa: BLE001
                self.log.warn("轮询音箱 %s 失败: %s" % (self.name_of(did), friendly_mi_error(e)))
                continue
            out.extend(self._parse(raw, did))
        return out

    def _parse(self, raw: List[Dict[str, Any]], did: str) -> List[Dict[str, Any]]:
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
                "device": did,
            })
        return out

    async def speak(self, text: str, did: str = "") -> bool:
        if self.mina is None:
            await self.connect()
        return await self.mina.text_to_speech(did or self.resolve_did(), text)

    async def stop(self, did: str = "") -> bool:
        """打断当前播报。"""
        if self.mina is None:
            await self.connect()
        return await self.mina.player_stop(did or self.resolve_did())


# ---------------------------------------------------------------- 演示模式
MOCK_DEVICES = [
    {"deviceID": "mock-0001", "hardware": "MOCK", "name": "演示音箱·客厅"},
    {"deviceID": "mock-0002", "hardware": "MOCK", "name": "演示音箱·卧室"},
]


class MockSpeaker:
    """不碰真机，用来验证「轮询 → 大模型 → 工具 → 播报」整条链路。"""

    is_mock = True

    def __init__(self, mi_cfg: Dict[str, Any], log) -> None:
        self.cfg = mi_cfg
        self.log = log
        self.dids = list(MOCK_DEVICES)
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.spoken: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        self.log.ok("演示模式已连接（不会访问小米云）")

    async def devices(self) -> List[Dict[str, Any]]:
        return [dict(d) for d in MOCK_DEVICES]

    def resolve_dids(self) -> List[str]:
        out = [str(d).strip() for d in (self.cfg.get("dids") or []) if str(d).strip()]
        if out:
            return out
        return [(self.cfg.get("did") or "mock-0001").strip() or "mock-0001"]

    def resolve_did(self) -> str:
        return self.resolve_dids()[0]

    def name_of(self, did: str) -> str:
        for d in MOCK_DEVICES:
            if d["deviceID"] == did:
                return d["name"]
        return did

    def inject(self, text: str, did: str = "") -> None:
        # 用纳秒 + 序号：同一毫秒内对两台音箱说同一句话时，
        # 原来按毫秒+长度生成的 id 会撞车，导致后一条被去重丢掉
        self._seq = getattr(self, "_seq", 0) + 1
        self._q.put({
            "request_id": "mock-%d-%d" % (time.time_ns(), self._seq),
            "ts": time.time(),
            "question": text,
            "device": did or self.resolve_did(),
        })

    async def poll(self) -> List[Dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    async def speak(self, text: str, did: str = "") -> bool:
        who = self.name_of(did or self.resolve_did())
        self.spoken.append({"ts": time.time(), "text": text, "device": who})
        self.spoken = self.spoken[-50:]
        self.log.info("🔊 [%s] 播报: %s" % (who, text))
        return True

    async def stop(self, did: str = "") -> bool:
        who = self.name_of(did or self.resolve_did())
        self.spoken.append({"ts": time.time(), "text": "（打断）", "device": who})
        self.log.info("🔇 [%s] 截断当前播报" % who)
        return True


class MissingDependencySpeaker:
    """想接真机但环境里没有 miservice：明确失败，不要假装在工作。"""

    is_mock = False

    def __init__(self, mi_cfg: Dict[str, Any], log) -> None:
        self.cfg = mi_cfg
        self.log = log
        self.dids = []

    async def connect(self) -> None:
        raise MiError(
            "已取消演示模式，但当前 Python 环境没有安装 miservice，接不了真机。\n"
            "在运行工作台的那个 Python 里装一下：<那个 python> -m pip install miservice"
        )

    async def devices(self):
        raise MiError("缺少 miservice")

    def resolve_dids(self):
        return []

    def resolve_did(self) -> str:
        raise MiError("缺少 miservice")

    def name_of(self, did: str) -> str:
        return did

    async def poll(self):
        return []

    async def speak(self, text: str, did: str = "") -> bool:
        return False

    async def stop(self, did: str = "") -> bool:
        return False


def make_speaker(mi_cfg: Dict[str, Any], log):
    """按配置选真机 / 演示。

    注意：只有「用户主动勾了演示模式」或「依赖真的缺失且确实无法接真机」时才降级。
    以前的写法是 mock=False 但缺依赖就静默变演示，用户取消勾选后毫无反应，
    以为开关坏了——现在缺依赖会直接报错，让人看见问题。
    """
    if mi_cfg.get("mock"):
        return MockSpeaker(mi_cfg, log)
    if not miservice_available():
        return MissingDependencySpeaker(mi_cfg, log)
    return RealSpeaker(mi_cfg, log)
