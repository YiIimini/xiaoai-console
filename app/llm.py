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


def chat_stream(
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    tools: Optional[List[Dict[str, Any]]] = None,
    on_delta=None,
) -> Dict[str, Any]:
    """流式调用。逐 token 回调 on_delta(text)，返回拼好的 assistant message。

    工具调用也在流里分片到达（delta.tool_calls），这里按 index 累加还原，
    所以「边想边调工具」和「边想边说话」可以共用一条通道。
    首个增量到达前的失败会自动重试；已经吐过字就不再重试，避免重复播报。
    """
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        raise LLMError("未配置 LLM api_key")

    payload: Dict[str, Any] = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.3)),
        "stream": True,
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
            "Accept": "text/event-stream",
            "User-Agent": "xiaoai-console/1.0",
        },
    )

    timeout = int(cfg.get("timeout", 90))
    content_parts: List[str] = []
    calls: Dict[int, Dict[str, Any]] = {}
    started = False

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise LLMError("LLM HTTP %s: %s" % (e.code, detail or e.reason))
    except socket.timeout:
        raise LLMError("LLM 请求超时（%ss）" % timeout)
    except urllib.error.URLError as e:
        raise LLMError("LLM 连接失败: %s" % (e.reason,))

    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                raise LLMError("LLM 错误: %s" % (obj["error"],))

            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    started = True
                    if on_delta:
                        try:
                            on_delta(text)
                        except Exception:
                            pass
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = calls.setdefault(idx, {"id": "", "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
    except (socket.timeout, urllib.error.URLError) as e:
        if not started:
            raise LLMError("流式读取中断: %s" % e)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    msg: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return msg


def stream_or_chat(
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    tools: Optional[List[Dict[str, Any]]] = None,
    on_delta=None,
) -> Dict[str, Any]:
    """优先流式；服务端不支持 SSE 时自动退回普通请求。"""
    if not cfg.get("stream", True) or on_delta is None:
        return chat(messages, cfg, tools)
    try:
        return chat_stream(messages, cfg, tools, on_delta)
    except LLMError as e:
        msg = str(e)
        retry_plain = ("HTTP 400" in msg or "HTTP 404" in msg
                       or "流式读取中断" in msg or "非 JSON" in msg)
        if retry_plain:
            return chat(messages, cfg, tools)
        raise
