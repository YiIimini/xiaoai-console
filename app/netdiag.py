"""网络出口自检：小米登录被风控时，先看它是从哪个 IP 出去的。

小米对国内账号的异地登录极敏感。开着 VPN/代理时，urllib 会读系统代理，
登录请求就从海外节点发出 → 触发二次验证 → 短信 → 反复失败 → 20024。
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from typing import Any, Dict, List

from . import store

PROBE = "https://ipinfo.io/json"
CN_HINTS = ("CN", "China")


def _fetch(url: str, proxy: str | None = None, timeout: int = 6) -> Dict[str, Any]:
    """取一次出口信息。proxy=None 表示显式直连。"""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "xiaoai-console/1.0"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def _system_proxy() -> Dict[str, Any]:
    if sys.platform != "darwin":
        return {"note": "非 macOS，跳过系统代理读取"}
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return {"error": "读取系统代理失败"}
    info: Dict[str, Any] = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("HTTPEnable : 1") or line.startswith("HTTPSEnable : 1"):
            info["enabled"] = True
        if "HTTPProxy :" in line:
            info["host"] = line.split(":", 1)[1].strip()
        if "HTTPPort :" in line:
            info["port"] = line.split(":", 1)[1].strip()
    return info or {"enabled": False}


def _miservice_ip(timeout: int = 8) -> Dict[str, Any]:
    """走小米实际使用的通道（miservice/biohttp）探一次，这才是小米看到的 IP。"""
    try:
        from .mi import install_direct_transport

        install_direct_transport()
        import asyncio

        import miservice.biohttp as bio

        async def go():
            async with bio.ClientSession() as s:
                async with s.get(PROBE) as r:
                    return await r.json()

        return asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def check() -> Dict[str, Any]:
    cfg = store.load_config()["mi"]
    direct = _fetch(PROBE)
    sysproxy = _system_proxy()
    proxied = None
    if sysproxy.get("enabled") and sysproxy.get("host"):
        proxied = _fetch(PROBE, proxy="http://%s:%s" % (sysproxy["host"], sysproxy["port"]))
    via = _miservice_ip()

    def region(d: Dict[str, Any]) -> str:
        if d.get("error"):
            return "查询失败"
        return "%s %s · %s" % (d.get("city") or "-", d.get("country") or "-", d.get("ip") or "-")

    verdict: List[str] = []
    via_country = (via.get("country") or "").upper()
    direct_country = (direct.get("country") or "").upper()

    if via_country and via_country not in ("CN", ""):
        verdict.append(
            "小米请求从 %s 出口。国内账号会被判为异地登录，必然触发验证码并可能被风控。"
            % region(via))
        verdict.append("解决：在代理/加速器里把 mi.com 相关域名设为直连，或临时关闭代理后登录。")
    elif via_country in ("CN",):
        verdict.append("小米请求从国内出口（%s），这条正常。" % region(via))
        if proxied and (proxied.get("country") or "").upper() not in ("CN", ""):
            verdict.append("注意：系统代理出口在境外（%s），只是工作台已强制小米域名直连。"
                           % region(proxied))

    if sysproxy.get("enabled"):
        verdict.append("系统代理已开启：%s:%s" % (sysproxy.get("host"), sysproxy.get("port")))

    return {
        "direct": {"text": region(direct), "raw": {k: direct.get(k) for k in ("ip", "city", "country")}},
        "system_proxy": sysproxy,
        "proxied": {"text": region(proxied)} if proxied else None,
        "miservice": {"text": region(via), "raw": {k: via.get(k) for k in ("ip", "city", "country")}},
        "bypass_proxy": bool(cfg.get("bypass_proxy", True)),
        "verdict": verdict,
    }
