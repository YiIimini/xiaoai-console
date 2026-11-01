"""米家（MIoT）设备接入层。

协议链路：miservice → MiIOService → api.io.mi.com
认证：同一个小米账号，sid 不同（音箱走 micoapi，米家走 xiaomiio），共用 data/.mi.token。

设计要点：
  - 后台常驻一个事件循环线程，同步接口供工具层调用，避免每次重登
  - 设备能力（siid/piid）从 miot-spec.org 拉取并落盘缓存，翻成人类可读的名字
  - 高危设备（门锁/摄像头/燃气/烟感）默认禁止写入，白名单可覆盖
  - 演示模式用内存假设备，整条链路可离线验证
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import store

LOG = store.LOG
CACHE_DIR = os.path.join(store.DATA_DIR, "miot-cache")

# 默认禁止写入的设备型号关键字（子串匹配，小写）
DEFAULT_BLOCKED = "lock,doorlock,camera,ipc,gas,smoke,alarm,safe,doorbell,locker"

BOOL_TRUE = {"true", "on", "1", "开", "打开", "开启", "yes", "是"}
BOOL_FALSE = {"false", "off", "0", "关", "关闭", "否", "no"}


class MiotError(RuntimeError):
    pass


# ---------------------------------------------------------------- 后台事件循环
class _Loop:
    """常驻事件循环：把 async 客户端包装成同步调用。"""

    _instance: Optional["_Loop"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="miot-loop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @classmethod
    def get(cls) -> "_Loop":
        with cls._lock:
            if cls._instance is None or not cls._instance.thread.is_alive():
                cls._instance = _Loop()
            return cls._instance

    def call(self, coro, timeout: float = 30):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _read_cache(name: str) -> Any:
    try:
        with open(_cache_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(name: str, data: Any) -> None:
    try:
        tmp = _cache_path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _cache_path(name))
    except Exception:
        pass


# ---------------------------------------------------------------- 能力解析
def describe_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """把 miot-spec 实例翻成便于人/模型使用的能力表。

    返回 {props: {名字: {siid,piid,format,values,unit,service}},
          actions: {名字: {siid,aiid,service,in_fields}}}
    """
    props: Dict[str, Dict[str, Any]] = {}
    actions: Dict[str, Dict[str, Any]] = {}
    for svc in (spec or {}).get("services") or []:
        svc_name = ((svc.get("description") or {}).get("name")) or ("service-%s" % svc.get("iid"))
        siid = svc.get("iid")
        for p in svc.get("properties") or []:
            pname = ((p.get("description") or {}).get("name")) or ("prop-%s" % p.get("iid"))
            values = [v.get("description") for v in (p.get("value-list") or [])
                      if isinstance(v, dict) and v.get("description")]
            props[pname] = {
                "siid": siid,
                "piid": p.get("iid"),
                "format": p.get("format"),
                "access": p.get("access") or [],
                "values": values,
                "unit": p.get("unit"),
                "service": svc_name,
            }
        for a in svc.get("actions") or []:
            aname = ((a.get("description") or {}).get("name")) or ("action-%s" % a.get("iid"))
            actions[aname] = {
                "siid": siid,
                "aiid": a.get("iid"),
                "service": svc_name,
                "in": [((i.get("description") or {}).get("name")) for i in (a.get("in") or [])],
            }
    return {"props": props, "actions": actions}


def coerce_value(raw: Any, fmt: Optional[str], allowed: Optional[List[str]] = None) -> Any:
    """把用户/模型给的值，按 MIoT format 转成设备要的类型。"""
    f = (fmt or "").lower()
    if f in ("bool", "boolean"):
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in BOOL_TRUE:
            return True
        if s in BOOL_FALSE:
            return False
        raise MiotError("开关类属性只接受 开/关，收到: %r" % raw)
    if f in ("uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32", "int64"):
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            raise MiotError("数值属性需要整数，收到: %r" % raw)
    if f in ("float", "double"):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise MiotError("数值属性需要小数，收到: %r" % raw)
    if f == "string":
        return str(raw)
    return raw


# ---------------------------------------------------------------- 真机客户端
class RealMiot:
    is_mock = False

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._svc = None
        self._lock = threading.Lock()

    async def _ensure(self):
        if self._svc is not None:
            return self._svc
        try:
            from miservice import MiAccount, MiIOService
        except Exception:
            raise MiotError("未安装 miservice，无法连接米家")
        user = (self.cfg.get("username") or "").strip()
        pwd = (self.cfg.get("password") or "").strip()
        if not user or not pwd:
            raise MiotError("未填写小米账号密码，请到「音箱接入」填写（米家共用同一账号）")

        from .mi import OTP

        async def otp_callback(method: str) -> str:
            LOG.warn("米家登录需要验证码（方式：%s），请在工作台输入" % method)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, OTP.request, method)

        account = MiAccount(
            None, user, pwd,
            token_store=store.resolve(self.cfg.get("token_path") or "data/.mi.token"),
            otp_callback=otp_callback,
        )
        await account.login("xiaomiio")
        self._svc = MiIOService(account)
        return self._svc

    async def _devices(self) -> List[Dict[str, Any]]:
        ttl = float(store.load_config()["miot"].get("device_cache_seconds", 60) or 0)
        cached = _read_cache("devices.json")
        if ttl > 0 and cached and (time.time() - float(cached.get("ts", 0))) < ttl:
            return cached.get("devices") or []
        svc = await self._ensure()
        rows = await svc.device_list()
        out = []
        for d in rows or []:
            out.append({
                "did": str(d.get("did") or ""),
                "name": d.get("name") or "",
                "model": d.get("model") or "",
            })
        _write_cache("devices.json", {"ts": time.time(), "devices": out})
        return out

    async def _spec(self, model: str) -> Dict[str, Any]:
        cached = _read_cache("spec-%s.json" % model)
        if cached:
            return cached
        svc = await self._ensure()
        try:
            raw = await svc.miot_spec(model)
        except Exception as e:  # noqa: BLE001
            raise MiotError("拉取能力描述失败: %s" % e)
        if not isinstance(raw, dict) or "services" not in raw:
            raise MiotError("该型号在 miot-spec.org 上没有描述: %s" % model)
        _write_cache("spec-%s.json" % model, raw)
        return raw

    async def _get(self, did: str, siid: int, piid: int):
        svc = await self._ensure()
        vals = await svc.miot_get_props(did, [(siid, piid)])
        return vals[0] if vals else None

    async def _set(self, did: str, siid: int, piid: int, value):
        svc = await self._ensure()
        return await svc.miot_set_props(did, [(siid, piid, value)])

    async def _action(self, did: str, siid: int, aiid: int, args: List[Any]):
        svc = await self._ensure()
        return await svc.miot_action(did, (siid, aiid), args)

    # ---- 同步门面
    def devices(self, timeout: float = 40) -> List[Dict[str, Any]]:
        return _Loop.get().call(self._devices(), timeout)

    def spec(self, model: str, timeout: float = 40) -> Dict[str, Any]:
        return _Loop.get().call(self._spec(model), timeout)

    def get(self, did, siid, piid, timeout: float = 25):
        return _Loop.get().call(self._get(did, siid, piid), timeout)

    def set(self, did, siid, piid, value, timeout: float = 25):
        return _Loop.get().call(self._set(did, siid, piid, value), timeout)

    def action(self, did, siid, aiid, args=None, timeout: float = 25):
        return _Loop.get().call(self._action(did, siid, aiid, args or []), timeout)


# ---------------------------------------------------------------- 演示设备
_MOCK_DEVICES = [
    {"did": "mock-light-1", "name": "客厅灯", "model": "yeelink.light.color8"},
    {"did": "mock-ac-1", "name": "主卧空调", "model": "zhimi.aircondition.ma1"},
    {"did": "mock-plug-1", "name": "书桌插座", "model": "cuco.plug.v3"},
    {"did": "mock-lock-1", "name": "大门锁", "model": "loock.lock.cc1"},
]

_MOCK_SPECS = {
    "yeelink.light.color8": {
        "services": [
            {"iid": 2, "description": {"name": "light"},
             "properties": [
                 {"iid": 1, "description": {"name": "power"}, "format": "bool", "access": ["read", "write"]},
                 {"iid": 2, "description": {"name": "brightness"}, "format": "uint8", "access": ["read", "write"], "unit": "percentage"},
                 {"iid": 3, "description": {"name": "color-temperature"}, "format": "uint32", "access": ["read", "write"], "unit": "kelvin"},
             ],
             "actions": []},
        ]
    },
    "zhimi.aircondition.ma1": {
        "services": [
            {"iid": 2, "description": {"name": "air-conditioner"},
             "properties": [
                 {"iid": 1, "description": {"name": "power"}, "format": "bool", "access": ["read", "write"]},
                 {"iid": 2, "description": {"name": "target-temperature"}, "format": "uint8", "access": ["read", "write"], "unit": "celsius"},
                 {"iid": 3, "description": {"name": "mode"}, "format": "uint8", "access": ["read", "write"],
                  "value-list": [{"description": "auto"}, {"description": "cool"}, {"description": "heat"}, {"description": "fan"}]},
             ],
             "actions": []},
        ]
    },
    "cuco.plug.v3": {
        "services": [
            {"iid": 2, "description": {"name": "switch"},
             "properties": [
                 {"iid": 1, "description": {"name": "power"}, "format": "bool", "access": ["read", "write"]},
             ],
             "actions": [{"iid": 1, "description": {"name": "toggle"}, "in": []}]},
        ]
    },
    "loock.lock.cc1": {
        "services": [
            {"iid": 3, "description": {"name": "door-lock"},
             "properties": [
                 {"iid": 1, "description": {"name": "lock-state"}, "format": "uint8", "access": ["read"]},
             ],
             "actions": [{"iid": 1, "description": {"name": "unlock"}, "in": []}]},
        ]
    },
}


class MockMiot:
    """演示模式：内存里的假设备，用来验证「语音 → 模型 → 场景 → 设备」整条链路。"""
    is_mock = True

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.state = {
            ("mock-light-1", 2, 1): False,
            ("mock-light-1", 2, 2): 60,
            ("mock-light-1", 2, 3): 4000,
            ("mock-ac-1", 2, 1): False,
            ("mock-ac-1", 2, 2): 26,
            ("mock-ac-1", 2, 3): 0,
            ("mock-plug-1", 2, 1): True,
            ("mock-lock-1", 3, 1): 1,
        }

    def devices(self, timeout: float = 0) -> List[Dict[str, Any]]:
        return [dict(d) for d in _MOCK_DEVICES]

    def spec(self, model: str, timeout: float = 0) -> Dict[str, Any]:
        if model not in _MOCK_SPECS:
            raise MiotError("演示模式没有该型号: %s" % model)
        return _MOCK_SPECS[model]

    def get(self, did, siid, piid, timeout: float = 0):
        return self.state.get((did, siid, piid))

    def set(self, did, siid, piid, value, timeout: float = 0):
        self.state[(did, siid, piid)] = value
        LOG.info("（演示）%s 属性 %s-%s 设为 %r" % (did, siid, piid, value))
        return [0]

    def action(self, did, siid, aiid, args=None, timeout: float = 0):
        LOG.info("（演示）%s 执行动作 %s-%s" % (did, siid, aiid))
        return 0


def make_client(mi_cfg: Dict[str, Any]) -> Any:
    miot_cfg = store.load_config()["miot"]
    if miot_cfg.get("mock") or mi_cfg.get("mock"):
        return MockMiot(mi_cfg)
    try:
        import miservice  # noqa: F401
    except Exception:
        return MockMiot(mi_cfg)
    return RealMiot(mi_cfg)


_CLIENT: Optional[Any] = None
_CLIENT_KEY = ""


def client() -> Any:
    """按当前配置返回客户端（账号/模式变化时重建）。"""
    global _CLIENT, _CLIENT_KEY
    cfg = store.load_config()
    key = "%s|%s|%s" % (cfg["mi"].get("username"), cfg["mi"].get("mock"), cfg["miot"].get("mock"))
    if _CLIENT is None or key != _CLIENT_KEY:
        _CLIENT = make_client(cfg["mi"])
        _CLIENT_KEY = key
    return _CLIENT


# ---------------------------------------------------------------- 设备与安全
def blocked_keywords() -> List[str]:
    raw = store.load_config()["miot"].get("blocked_keywords") or DEFAULT_BLOCKED
    return [w.strip().lower() for w in str(raw).split(",") if w.strip()]


def is_blocked(device: Dict[str, Any]) -> bool:
    model = (device.get("model") or "").lower()
    name = (device.get("name") or "").lower()
    return any(k in model or k in name for k in blocked_keywords())


def find_device(name_or_did: str) -> Dict[str, Any]:
    want = (name_or_did or "").strip()
    if not want:
        raise MiotError("设备名不能为空")
    devs = client().devices()
    for d in devs:
        if d["did"] == want:
            return d
    hits = [d for d in devs if want in (d.get("name") or "") or want.lower() in (d.get("model") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise MiotError("没有找到设备「%s」。现有设备: %s" % (
            want, "、".join(d["name"] for d in devs) or "（空）"))
    raise MiotError("「%s」匹配到多台设备: %s，请说全名" % (
        want, "、".join(d["name"] for d in hits)))


def resolve_prop(device: Dict[str, Any], prop: str) -> Dict[str, Any]:
    want = (prop or "").strip()
    caps = describe_spec(client().spec(device["model"]))
    props = caps["props"]
    if want in props:
        return props[want]
    low = want.lower().replace("_", "-")
    for k, v in props.items():
        if k.lower().replace("_", "-") == low:
            return v
    hits = [k for k in props if low in k.lower()]
    if len(hits) == 1:
        return props[hits[0]]
    raise MiotError("设备「%s」没有属性「%s」。可用: %s" % (
        device["name"], want, "、".join(sorted(props)) or "（无）"))


def resolve_action(device: Dict[str, Any], action: str) -> Dict[str, Any]:
    caps = describe_spec(client().spec(device["model"]))
    acts = caps["actions"]
    want = (action or "").strip()
    if want in acts:
        return acts[want]
    low = want.lower().replace("_", "-")
    for k, v in acts.items():
        if k.lower().replace("_", "-") == low:
            return v
    raise MiotError("设备「%s」没有动作「%s」。可用: %s" % (
        device["name"], want, "、".join(sorted(acts)) or "（无）"))


def capabilities(name_or_did: str) -> str:
    dev = find_device(name_or_did)
    caps = describe_spec(client().spec(dev["model"]))
    lines = ["设备：%s（型号 %s）" % (dev["name"], dev["model"])]
    if is_blocked(dev):
        lines.append("⚠ 该设备属于高危类别，工作台只允许读、不允许写")
    lines.append("可读写的属性：")
    for k, v in sorted(caps["props"].items()):
        extra = ""
        if v.get("values"):
            extra = " 取值: " + "/".join(v["values"])
        elif v.get("format") in ("bool", "boolean"):
            extra = " 取值: 开/关"
        unit = (" 单位: " + v["unit"]) if v.get("unit") else ""
        rw = "读写" if "write" in (v.get("access") or []) else "只读"
        lines.append("  - %s（%s，%s）%s%s" % (k, rw, v.get("format"), unit, extra))
    if caps["actions"]:
        lines.append("可执行的动作：")
        for k, v in sorted(caps["actions"].items()):
            lines.append("  - %s%s" % (k, ("（参数：%s）" % ", ".join(x for x in v["in"] if x)) if any(v["in"]) else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 同步操作
def read_prop(name_or_did: str, prop: str) -> str:
    dev = find_device(name_or_did)
    meta = resolve_prop(dev, prop)
    val = client().get(dev["did"], meta["siid"], meta["piid"])
    return "%s 的 %s = %s" % (dev["name"], prop, val)


def write_prop(name_or_did: str, prop: str, value: Any) -> str:
    cfg = store.load_config()["miot"]
    if not cfg.get("allow_write", True):
        raise MiotError("米家写入已关闭（「米家设备」面板可开启）")
    dev = find_device(name_or_did)
    if is_blocked(dev):
        raise MiotError("「%s」属于高危设备（门锁/摄像头/燃气等），只允许读取，禁止写入" % dev["name"])
    meta = resolve_prop(dev, prop)
    if "write" not in (meta.get("access") or []) and meta.get("access"):
        raise MiotError("属性 %s 是只读的" % prop)
    coerced = coerce_value(value, meta.get("format"), meta.get("values"))
    codes = client().set(dev["did"], meta["siid"], meta["piid"], coerced)
    code = codes[0] if isinstance(codes, list) and codes else codes
    if code not in (0, None):
        raise MiotError("设备返回错误码 %s" % code)
    LOG.info("🏠 %s → %s = %r" % (dev["name"], prop, coerced))
    return "%s 的 %s 已设为 %r" % (dev["name"], prop, coerced)


def do_action(name_or_did: str, action: str, args: Optional[List[Any]] = None) -> str:
    cfg = store.load_config()["miot"]
    if not cfg.get("allow_write", True):
        raise MiotError("米家写入已关闭（「米家设备」面板可开启）")
    dev = find_device(name_or_did)
    if is_blocked(dev):
        raise MiotError("「%s」属于高危设备，禁止执行动作" % dev["name"])
    meta = resolve_action(dev, action)
    code = client().action(dev["did"], meta["siid"], meta["aiid"], args or [])
    if code not in (0, None):
        raise MiotError("设备返回错误码 %s" % code)
    LOG.info("🏠 %s 执行 %s" % (dev["name"], action))
    return "%s 已执行 %s" % (dev["name"], action)


def list_devices_text() -> str:
    devs = client().devices()
    if not devs:
        return "米家账号下没有设备。"
    lines = []
    for d in devs:
        flag = "（高危·只读）" if is_blocked(d) else ""
        lines.append("- %s｜型号 %s%s" % (d["name"], d["model"], flag))
    return "共 %d 台设备：\n%s" % (len(devs), "\n".join(lines))
