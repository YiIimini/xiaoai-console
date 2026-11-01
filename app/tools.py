"""Agent 的手：受白名单与目录约束的 shell / 文件工具。

安全设计（语音误触发的现实防御）：
  - shell 只允许白名单首命令，且禁止 ; | & > < ` $() 等串接符
  - 文件路径必须落在 repo 目录内（realpath 前缀校验）
  - 每次执行有超时与输出截断
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, List

BLOCKED_SUBSTRINGS = (
    "rm -rf", "rm -fr", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
    "chmod 777 /", "> /dev/sd", "git push --force", "git push -f",
    "curl | sh", "wget | sh", "sudo ",
)

# 即使写进白名单也永不放行的危险首命令
DANGEROUS_HEADS = frozenset({
    "rm", "rmdir", "dd", "mkfs", "fdisk", "diskutil", "shred", "chmod", "chown",
    "chgrp", "mv", "sudo", "su", "doas", "kill", "killall", "pkill", "shutdown",
    "reboot", "halt", "launchctl", "systemctl", "service", "crontab", "at",
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ssh", "scp", "rsync",
    "sh", "bash", "zsh", "dash", "fish", "eval", "exec", "source",
})

# 串接 / 重定向 / 子命令 / 换行：全部拒绝
# 注意 \n 与 \r 必须显式拦下，否则 "ls\nid" 会以两条命令执行
SHELL_METACHARS = re.compile(r"[;&|<>`\n\r\x00-\x08\x0b-\x1f]|\$\(")

# 允许但不构成沙箱的命令（可执行任意代码），UI 会提示
ARBITRARY_CODE_HEADS = frozenset({"python", "python3", "node", "ruby", "perl", "php"})


class ToolError(RuntimeError):
    pass


def _repo(cfg: Dict[str, Any]) -> str:
    from .store import resolve

    repo = (cfg.get("repo") or "").strip()
    if not repo:
        raise ToolError("未配置仓库路径，请到「Agent 与沙箱」填写")
    repo = os.path.realpath(resolve(repo))
    if not os.path.isdir(repo):
        raise ToolError("仓库路径在本机不存在: %s（换电脑后需要重新指定）" % repo)
    return repo


def _safe_path(cfg: Dict[str, Any], path: str) -> str:
    repo = _repo(cfg)
    raw = (path or ".").strip()
    full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(repo, raw))
    if full != repo and not full.startswith(repo + os.sep):
        raise ToolError("路径越界，仅允许仓库内: %s" % path)
    return full


def _allowed(cfg: Dict[str, Any]) -> List[str]:
    raw = cfg.get("allowed_cmds") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def run_shell(cfg: Dict[str, Any], cmd: str) -> str:
    cmd = (cmd or "").strip()
    if not cmd:
        raise ToolError("空命令")
    low = cmd.lower()
    for bad in BLOCKED_SUBSTRINGS:
        if bad in low:
            raise ToolError("命令包含被禁片段: %s" % bad)
    if SHELL_METACHARS.search(cmd):
        raise ToolError("命令包含串接/重定向符号，已拒绝")
    head = cmd.split()[0]
    if head in DANGEROUS_HEADS:
        raise ToolError("命令 %r 属于危险命令，任何情况下都不执行" % head)
    allowed = _allowed(cfg)
    if head not in allowed:
        raise ToolError("命令 %r 不在白名单内。可用: %s" % (head, ", ".join(allowed)))
    if head in ARBITRARY_CODE_HEADS:
        LOG_WARN = "（注意：%s 可执行任意代码，属于白名单的固有风险）" % head
    else:
        LOG_WARN = ""

    timeout = int(cfg.get("shell_timeout", 120))
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=_repo(cfg), capture_output=True,
            text=True, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise ToolError("命令超时（%ss）" % timeout)
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
    out = (out.strip() or "(无输出)") + LOG_WARN
    cap = int(cfg.get("max_output", 4000))
    if len(out) > cap:
        out = out[:cap] + "\n…[截断 %d 字符]" % (len(out) - cap)
    return "exit=%d\n%s" % (p.returncode, out)


def read_file(cfg: Dict[str, Any], path: str) -> str:
    full = _safe_path(cfg, path)
    if not os.path.isfile(full):
        raise ToolError("不是文件: %s" % path)
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    cap = int(cfg.get("max_output", 4000))
    if len(data) > cap:
        return data[:cap] + "\n…[截断 %d 字符]" % (len(data) - cap)
    return data or "(空文件)"


def write_file(cfg: Dict[str, Any], path: str, content: str) -> str:
    if not cfg.get("allow_write", True):
        raise ToolError("写入已在本工作台关闭")
    full = _safe_path(cfg, path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content or "")
    return "ok, 已写入 %s (%d 字节)" % (os.path.relpath(full, _repo(cfg)), len(content or ""))


def list_dir(cfg: Dict[str, Any], path: str = ".") -> str:
    full = _safe_path(cfg, path)
    if not os.path.isdir(full):
        raise ToolError("不是目录: %s" % path)
    rows = []
    for name in sorted(os.listdir(full))[:200]:
        if name.startswith(".git") or name in ("node_modules", "__pycache__", ".venv"):
            continue
        p = os.path.join(full, name)
        rows.append(("[D] " if os.path.isdir(p) else "[F] ") + name)
    return "\n".join(rows) or "(空目录)"


def git_diff(cfg: Dict[str, Any]) -> str:
    return run_shell(cfg, "git diff --stat")


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在项目仓库执行一条 shell 命令（跑测试、构建、git 状态）。只允许白名单首命令，禁止串接。",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string", "description": "完整命令行，例如 pytest -q"}},
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内某个文件的内容（自动截断）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对仓库根的文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入或覆盖仓库内某个文件，用于落地修复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出仓库内某个目录的文件与子目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "查看当前仓库未提交改动的统计。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def dispatch(cfg: Dict[str, Any], name: str, args: Dict[str, Any]) -> str:
    try:
        if name == "run_shell":
            return run_shell(cfg, args.get("cmd", ""))
        if name == "read_file":
            return read_file(cfg, args.get("path", ""))
        if name == "write_file":
            return write_file(cfg, args.get("path", ""), args.get("content", ""))
        if name == "list_dir":
            return list_dir(cfg, args.get("path", "."))
        if name == "git_diff":
            return git_diff(cfg)
        return "未知工具: %s" % name
    except ToolError as e:
        return "工具拒绝/失败: %s" % e
    except Exception as e:  # noqa: BLE001
        return "工具异常: %s: %s" % (type(e).__name__, e)
