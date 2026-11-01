"""OpenAI 兼容 /chat/completions 客户端。纯标准库 urllib，不依赖 openai SDK。"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class LLMError(RuntimeError):
    pass


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置 LLM base_url")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


RETRYABLE_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)


def chat(
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    tools: Optional[List[Dict[str, Any]]] = None,
    retries: int = 2,
) -> Dict[str, Any]:
    """调用 chat completion，网络抖动/限流自动重试；只重试请求本身，不重复执行工具。"""
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return _chat_once(messages, cfg, tools)
        except LLMError as e:
            last = e
            msg = str(e)
            transient = ("超时" in msg) or ("连接失败" in msg) or any(
                ("HTTP %d" % code) in msg for code in RETRYABLE_STATUS)
            if attempt >= retries or not transient:
                raise
            time.sleep(1.2 * (attempt + 1))
    raise last if last else LLMError("未知错误")


def _chat_once(
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """单次请求。"""
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        raise LLMError("未配置 LLM api_key")

    payload: Dict[str, Any] = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.3)),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(cfg.get("base_url", "")),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "xiaoai-console/1.0",
        },
    )
    timeout = int(cfg.get("timeout", 90))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise LLMError("LLM HTTP %s: %s" % (e.code, detail or e.reason))
    except socket.timeout:
        raise LLMError("LLM 请求超时（%ss）" % timeout)
    except urllib.error.URLError as e:
        raise LLMError("LLM 连接失败: %s" % (e.reason,))

    try:
        data = json.loads(raw)
    except ValueError:
        raise LLMError("LLM 返回非 JSON: " + raw[:300])

    if isinstance(data, dict) and data.get("error"):
        raise LLMError("LLM 错误: %s" % (data["error"],))
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("LLM 返回结构异常: " + raw[:300])
    return msg


def ping(cfg: Dict[str, Any]) -> Dict[str, Any]:
    msg = chat([{"role": "user", "content": "回复两个字：在线"}], cfg)
    return {"ok": True, "reply": (msg.get("content") or "").strip()[:80], "model": cfg.get("model")}
