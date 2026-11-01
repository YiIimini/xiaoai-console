"""用户自定义场景：把一串设备操作打包成一个词，语音直接触发。

scenes.json 结构：
{
  "睡觉": {
    "desc": "关灯、空调 26 度、关插座",
    "steps": [
      {"device": "客厅灯", "prop": "power", "value": "关"},
      {"device": "主卧空调", "prop": "target-temperature", "value": 26},
      {"delay": 0.5},
      {"device": "书桌插座", "prop": "power", "value": "关"}
    ]
  }
}

步骤三种形式：
  {"device": 名字, "prop": 属性, "value": 值}   —— 写属性
  {"device": 名字, "action": 动作, "args": []}  —— 执行动作
  {"delay": 秒}                                 —— 等待
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Tuple

from . import miot, store

SCENES_PATH = os.path.join(store.DATA_DIR, "scenes.json")

EXAMPLE = {
    "回家": {
        "desc": "开客厅灯、开插座",
        "steps": [
            {"device": "客厅灯", "prop": "power", "value": "开"},
            {"device": "客厅灯", "prop": "brightness", "value": 80},
            {"device": "书桌插座", "prop": "power", "value": "开"},
        ],
    },
    "睡觉": {
        "desc": "关灯、关插座、空调 26 度",
        "steps": [
            {"device": "客厅灯", "prop": "power", "value": "关"},
            {"device": "书桌插座", "prop": "power", "value": "关"},
            {"device": "主卧空调", "prop": "power", "value": "开"},
            {"device": "主卧空调", "prop": "target-temperature", "value": 26},
        ],
    },
    "全关": {
        "desc": "把所有能关的都关掉",
        "steps": [
            {"device": "客厅灯", "prop": "power", "value": "关"},
            {"device": "主卧空调", "prop": "power", "value": "关"},
            {"device": "书桌插座", "prop": "power", "value": "关"},
        ],
    },
}


def load() -> Dict[str, Any]:
    if not os.path.exists(SCENES_PATH):
        save(EXAMPLE)
        return dict(EXAMPLE)
    try:
        with open(SCENES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(scenes: Dict[str, Any]) -> None:
    tmp = SCENES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(scenes, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SCENES_PATH)


def validate(scenes: Any) -> Tuple[bool, str]:
    """导入/保存前校验，避免把一步坏数据留到语音触发时才发现。"""
    if not isinstance(scenes, dict):
        return False, "场景文件必须是一个对象：{场景名: {steps: [...]}}"
    for name, body in scenes.items():
        if not isinstance(body, dict):
            return False, "场景「%s」必须是对象" % name
        steps = body.get("steps")
        if not isinstance(steps, list) or not steps:
            return False, "场景「%s」缺少 steps 或为空" % name
        if len(steps) > 50:
            return False, "场景「%s」步骤过多（上限 50）" % name
        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                return False, "场景「%s」第 %d 步格式错误" % (name, i)
            if "delay" in st:
                try:
                    float(st["delay"])
                except (TypeError, ValueError):
                    return False, "场景「%s」第 %d 步 delay 不是数字" % (name, i)
                continue
            if not st.get("device"):
                return False, "场景「%s」第 %d 步缺少 device" % (name, i)
            if not (st.get("prop") or st.get("action")):
                return False, "场景「%s」第 %d 步需要 prop 或 action" % (name, i)
    return True, ""


def resolve_name(name: str) -> str:
    want = (name or "").strip()
    scenes = load()
    if want in scenes:
        return want
    hits = [k for k in scenes if want and (want in k or k in want)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise miot.MiotError("没有叫「%s」的场景。现有: %s" % (want, "、".join(scenes) or "（空）"))
    raise miot.MiotError("「%s」匹配到多个场景: %s" % (want, "、".join(hits)))


def run(name: str) -> str:
    """按顺序执行场景，逐步收集结果；单步失败不中断，最后一起汇报。"""
    real = resolve_name(name)
    body = load().get(real) or {}
    steps: List[Dict[str, Any]] = body.get("steps") or []
    if not steps:
        raise miot.MiotError("场景「%s」没有任何步骤" % real)

    ok, bad = [], []
    for i, st in enumerate(steps, 1):
        if "delay" in st:
            time.sleep(max(0.0, min(30.0, float(st["delay"]))))
            continue
        try:
            if st.get("action"):
                msg = miot.do_action(st["device"], st["action"], st.get("args"))
            else:
                msg = miot.write_prop(st["device"], st["prop"], st.get("value"))
            ok.append(msg)
        except Exception as e:  # noqa: BLE001
            bad.append("第 %d 步 %s: %s" % (i, st.get("device", "?"), e))
    store.LOG.info("🎬 场景「%s」执行完成：成功 %d 步，失败 %d 步" % (real, len(ok), len(bad)))
    if not bad:
        return "场景「%s」已执行：%s" % (real, "；".join(ok) if ok else "全部完成")
    if not ok:
        raise miot.MiotError("场景「%s」全部失败：%s" % (real, "；".join(bad)))
    return "场景「%s」部分完成，成功 %d 步，失败：%s" % (real, len(ok), "；".join(bad))


def summary_text() -> str:
    scenes = load()
    if not scenes:
        return "还没有定义任何场景。"
    lines = []
    for name, body in scenes.items():
        desc = (body or {}).get("desc") or ""
        n = len((body or {}).get("steps") or [])
        lines.append("- %s（%d 步）%s" % (name, n, ("：" + desc) if desc else ""))
    return "共 %d 个场景：\n%s" % (len(scenes), "\n".join(lines))
