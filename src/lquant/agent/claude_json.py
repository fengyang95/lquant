"""claude CLI stream-json 输出解析为统一事件。

事件 dict 键固定：kind / text / name / args / summary / session_id /
tool_use_id / data。

- ``thinking`` 与 ``system`` 属**过程数据**：解析层一律保留，是否外发交给上层
  （A2A 出站会先过脱敏，见 ``agent/redact.py``）。
- ``system`` 只取白名单字段（``_SYSTEM_FIELDS``）：init 行里还有 ``apiKeySource``、
  ``session_id``、``slash_commands``、``uuid`` 等，既不外发也不进事件。
  ``cwd`` 例外地保留 —— 它靠出站路径脱敏变成 ``<path>``（见 redact.py）。
"""

from __future__ import annotations

import json
from typing import Any

_SUMMARY_LEN = 200
_DEFAULT_ERROR_TEXT = "执行失败"

#: system 行外发字段白名单 —— 黑名单挡不住「未知的敏感字段」，白名单才稳。
#: ``cwd`` 故意保留：它不是「不该存在的字段」，而是靠出站路径脱敏变成
#: ``<path>`` 才外发（见 agent/redact.py）。
_SYSTEM_FIELDS = ("subtype", "model", "permissionMode", "output_style",
                  "cwd", "tools", "mcp_servers")


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


def _parse_assistant(obj: dict[str, Any]) -> list[dict[str, Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    events: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            events.append(_event("delta", text=str(block.get("text") or "")))
        elif block_type == "thinking":
            events.append(_event("thinking", text=str(block.get("thinking") or "")))
        elif block_type == "tool_use":
            raw_input = block.get("input")
            args = raw_input if isinstance(raw_input, dict) else {}
            events.append(_event("tool_call", name=str(block.get("name") or ""),
                                 args=args,
                                 tool_use_id=str(block.get("id") or "")))
    return events


def _parse_user(obj: dict[str, Any]) -> list[dict[str, Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    events: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, default=str)
        summary = text[:_SUMMARY_LEN]
        events.append(_event("tool_result", text=text, summary=summary,
                             tool_use_id=str(block.get("tool_use_id") or "")))
    return events


def _parse_system(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """system 行：只保留白名单字段（无可用字段则视为不可解析）。"""
    payload = {k: obj[k] for k in _SYSTEM_FIELDS if k in obj}
    if not payload:
        return []
    return [_event("system", data=payload)]


def _parse_result(obj: dict[str, Any]) -> list[dict[str, Any]]:
    subtype = obj.get("subtype")
    session_id = str(obj.get("session_id") or "")
    if subtype == "success":
        return [_event("done", text=str(obj.get("result") or ""), session_id=session_id)]
    text = obj.get("result") or obj.get("error") or _DEFAULT_ERROR_TEXT
    return [_event("error", text=str(text), session_id=session_id)]


def parse_stream_line(line: str) -> list[dict[str, Any]]:
    """把一行 stream-json 解析为 0..n 个统一事件。坏行/未知类型返回 []。"""
    if not line or not line.strip():
        return []
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []
    obj_type = obj.get("type")
    if obj_type == "assistant":
        return _parse_assistant(obj)
    if obj_type == "user":
        return _parse_user(obj)
    if obj_type == "system":
        return _parse_system(obj)
    if obj_type == "result":
        return _parse_result(obj)
    return []
