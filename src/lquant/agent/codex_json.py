"""``codex exec --json`` 的 JSONL 输出 → 归一化事件。

事件契约与 :mod:`lquant.agent.claude_json` **完全一致**（见 cli_agent 的模块
docstring），这样 ``CliAgentService._consume`` 对两个 provider 只有一份实现。

下面这套词汇表来自实机取证（codex-cli 0.159.3，``codex exec --json``），
不是猜的：``thread.started`` / ``turn.started`` / ``turn.completed`` /
``turn.failed`` / ``item.started`` / ``item.updated`` / ``item.completed``，
item 类型有 ``agent_message`` / ``reasoning`` / ``command_execution`` /
``mcp_tool_call`` / ``file_change`` / ``todo_list`` / ``error`` / ``web_search``。

两处与 claude 的**实质差异**，必须在这里处理掉，别泄到上层：

1. **item 级 ``error`` 是非致命的**。实测「模型元数据缺失」也走
   ``item.completed/error`` 发出来，而该轮照样 ``turn.completed`` 正常收尾 ——
   若映射成契约里的 ``error``，每次调用都会被判死。故降级为 ``system``
   （``data.level="error"``），真正的终态失败只有 ``turn.failed`` / ``thread.failed``。
2. **没有 token 级增量**。实测让模型输出 40 行文本，正文仍以**单个**
   ``item.completed/agent_message`` 整块给出（不是逐 token 的 ``item.updated``）。
   所以 codex 走 A2A 流式时，正文是一整帧而非逐字追加 —— 这是 CLI 的能力边界，
   不是取舍。同一轮可以出现**多个** ``agent_message`` item，按序追加即正文。
"""

from __future__ import annotations

import json
from typing import Any

_SUMMARY_LEN = 200

#: 归一化事件的固定键（与 claude_json._event 保持一致）
_EVENT_KEYS = ("kind", "text", "name", "args", "summary", "session_id",
               "tool_use_id", "data")


def _event(
    kind: str,
    *,
    text: str = "",
    name: str = "",
    args: dict[str, Any] | None = None,
    summary: str = "",
    session_id: str = "",
    tool_use_id: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text,
        "name": name,
        "args": args if args is not None else {},
        "summary": summary,
        "session_id": session_id,
        "tool_use_id": tool_use_id,
        "data": data if data is not None else {},
    }


def _mcp_text(item: dict[str, Any]) -> str:
    """mcp_tool_call 的结果文本：正常取 result.content[].text，失败取 error.message。"""
    err = item.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "")
        if msg:
            return f"调用失败：{msg}"
    res = item.get("result")
    if res is None:
        return ""
    if isinstance(res, dict):
        parts = res.get("content")
        if isinstance(parts, list):
            texts = [str(p.get("text")) for p in parts
                     if isinstance(p, dict) and p.get("text") is not None]
            if texts:
                return "\n".join(texts)
    return json.dumps(res, ensure_ascii=False)


def _failure_text(obj: dict[str, Any]) -> str:
    """turn.failed / thread.failed 的失败原因（载荷未见文档，防御式取）。"""
    for key in ("message", "error", "reason", "failure"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict) and val.get("message"):
            return str(val["message"])
    return "Codex 执行失败"


def _shell_result(item: dict[str, Any]) -> tuple[str, str]:
    """command_execution 的结果文本；无输出时用退出码兜底，别留空帧。"""
    out = str(item.get("aggregated_output") or "")
    if out:
        return out, out[:_SUMMARY_LEN]
    code = item.get("exit_code")
    status = str(item.get("status") or "")
    text = f"(无输出，exit_code={code}, status={status})"
    return text, text


def _parse_item(obj: dict[str, Any], *, completed: bool) -> list[dict[str, Any]]:
    item = obj.get("item")
    if not isinstance(item, dict):
        return []
    itype = str(item.get("type") or "")

    if itype == "agent_message":
        text = str(item.get("text") or "")
        return [_event("delta", text=text)] if completed and text else []

    if itype == "reasoning":
        text = str(item.get("text") or "")
        return [_event("thinking", text=text)] if completed and text else []

    if itype == "command_execution":
        cmd = str(item.get("command") or "")
        if not completed:
            # 只宣告开始；item.updated 的中间输出不追（实测不触发，
            # 而 completed 的 aggregated_output 是全文，追了只会重复）
            return [_event("tool_call", name="shell", args={"command": cmd},
                           tool_use_id=str(item.get("id") or ""))]
        text, summary = _shell_result(item)
        return [_event("tool_result", name="shell", text=text, summary=summary,
                       tool_use_id=str(item.get("id") or ""))]

    if itype == "mcp_tool_call":
        # 工具名带上 server 前缀：一个工作区可能挂多个 MCP server
        name = f"{item.get('server') or 'mcp'}/{item.get('tool') or '?'}"
        raw_args = item.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}
        if not completed:
            return [_event("tool_call", name=name, args=args,
                           tool_use_id=str(item.get("id") or ""))]
        text = _mcp_text(item)
        return [_event("tool_result", name=name, text=text,
                       summary=text[:_SUMMARY_LEN],
                       tool_use_id=str(item.get("id") or ""))]

    if itype == "error":
        # ⚠️ 非致命（见模块 docstring 第 1 点）：走 system，不走 error
        return [_event("system", data={"level": "error",
                                       "message": str(item.get("message") or "")})]

    # 未做一阶映射的 item（file_change / todo_list / web_search / 未来新增）：
    # 不静默丢弃，降级成 system 帧（出站会脱敏 + 截断），
    # 免得 codex 升级后新增的过程数据凭空消失。
    if not completed:
        return []
    return [_event("system", data={"item_type": itype, "item": item})]


def parse_codex_line(raw: str) -> list[dict[str, Any]]:
    """一行 ``codex exec --json`` 输出 → 归一化事件列表（不可解析则空列表）。"""
    line = raw.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []

    etype = obj.get("type")

    if etype == "thread.started":
        # codex 在正文之前就给出 thread_id，正好可以提前落库（claude 挂在 done 上）
        return [_event("session", session_id=str(obj.get("thread_id") or ""))]

    if etype == "turn.completed":
        return [_event("done")]

    if etype in ("turn.failed", "thread.failed"):
        return [_event("error", text=_failure_text(obj))]

    if etype in ("item.started", "item.completed"):
        return _parse_item(obj, completed=(etype == "item.completed"))

    # turn.started / item.updated / 未来新增的行级事件：静默跳过
    # （item.updated 不触发，且中间态对最终正文没有增量信息）
    return []
