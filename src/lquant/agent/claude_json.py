"""claude CLI stream-json 输出解析为统一事件。

事件 dict 键固定：kind / text / name / args / summary / session_id /
tool_use_id / data。

- ``thinking`` 与 ``system`` 属**过程数据**：解析层一律保留，是否外发交给上层
  （A2A 出站会先过脱敏，见 ``agent/redact.py``）。
- ``system`` 只取白名单字段（``_SYSTEM_FIELDS``）：init 行里还有 ``apiKeySource``、
  ``session_id``、``slash_commands``、``uuid`` 等，既不外发也不进事件。
  ``cwd`` 例外地保留 —— 它靠出站路径脱敏变成 ``<path>``（见 redact.py）。

为什么必须按 message id 去重（``--include-partial-messages``）
------------------------------------------------------------

打开 token 级流式后，claude 会把同一段正文发**两遍**：

1. ``stream_event/content_block_delta`` 逐 token 增量（打字机效果的来源）；
2. 紧随其后的 ``assistant`` 行，把这条消息的**完整正文**再给一次。

两条都落库就会正文翻倍（一个字面量重复，不是顺序问题）。所以
:class:`StreamParser` 记住「哪些 ``message.id`` 已经按增量下发过」，再遇到同一
id 的 ``assistant`` 行就跳过其中的 ``text`` / ``thinking`` 块。``tool_use`` 不参与
去重 —— 工具调用**永远只从 ``assistant`` 整块出**，增量里没有它。

没有 ``message_start``（拿不到 id）时不产增量，让 ``assistant`` 整块兜底：
宁可这一轮退化成整块，也不能因为「不知道 id」把内容发两遍。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

_SUMMARY_LEN = 200
_DEFAULT_ERROR_TEXT = "执行失败"

#: 「已增量下发」的 message id 只留最近这么多个：长会话里 id 会不断累积，
#: 但每条 id 只在「增量 → assistant 整块」这几行之内有用，留太多纯属浪费内存。
_TRACKED_IDS_MAX = 64

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


def _parse_assistant(obj: dict[str, Any], *,
                     skip_text: bool = False) -> list[dict[str, Any]]:
    """``assistant`` 整块 → 事件。

    ``skip_text=True``（该 message.id 的正文已经按增量下发过）时跳过 ``text`` /
    ``thinking`` 块，避免与增量重复；``tool_use`` 不受影响。
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    events: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            if skip_text:
                continue
            events.append(_event("delta", text=str(block.get("text") or "")))
        elif block_type == "thinking":
            if skip_text:
                continue
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
        data = {"is_error": True} if block.get("is_error") else None
        events.append(_event("tool_result", text=text, summary=summary,
                             tool_use_id=str(block.get("tool_use_id") or ""),
                             data=data))
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


def _parse_object(obj: dict[str, Any], *,
                  skip_text: bool = False) -> list[dict[str, Any]]:
    """按 ``type`` 分派非流式行（``skip_text`` 只对 ``assistant`` 有意义）。"""
    obj_type = obj.get("type")
    if obj_type == "assistant":
        return _parse_assistant(obj, skip_text=skip_text)
    if obj_type == "user":
        return _parse_user(obj)
    if obj_type == "system":
        return _parse_system(obj)
    if obj_type == "result":
        return _parse_result(obj)
    return []


class StreamParser:
    """**有状态**的 stream-json 行解析器：跨行去重 token 增量与整块正文。

    一次 :meth:`feed` 处理一行；同一个实例必须在**同一轮输出**内复用（见
    ``CliAgentService._make_line_parser``），否则记不住 ``message.id``。
    无状态的单行解析用模块级 :func:`parse_stream_line`。
    """

    def __init__(self) -> None:
        #: 当前正在流式的 message.id（由 message_start 建立）
        self._current_id: str = ""
        #: 已按增量下发过正文的 message.id（有界，保留最近 _TRACKED_IDS_MAX 个）
        self._streamed: OrderedDict[str, None] = OrderedDict()

    def _mark_streamed(self, mid: str) -> None:
        self._streamed[mid] = None
        self._streamed.move_to_end(mid)
        while len(self._streamed) > _TRACKED_IDS_MAX:
            self._streamed.popitem(last=False)

    def _emit_delta(self, kind: str, text: str) -> list[dict[str, Any]]:
        """增量只有拿得到 message.id 时才下发（否则丢弃，让整块兜底）。"""
        if not self._current_id:
            return []
        self._mark_streamed(self._current_id)
        return [_event(kind, text=text)]

    def _feed_stream_event(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        event = obj.get("event")
        if not isinstance(event, dict):
            return []
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            mid = message.get("id") if isinstance(message, dict) else None
            self._current_id = str(mid) if mid else ""
            return []
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return []
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return self._emit_delta("delta", str(delta.get("text") or ""))
            if delta_type == "thinking_delta":
                return self._emit_delta(
                    "thinking", str(delta.get("thinking") or ""))
            # input_json_delta / signature_delta 等：不是正文，不产事件
            return []
        # content_block_start / content_block_stop / message_delta / message_stop
        return []

    def feed(self, line: str) -> list[dict[str, Any]]:
        """把一行 stream-json 解析为 0..n 个统一事件。坏行/未知类型返回 []。"""
        if not line or not line.strip():
            return []
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(obj, dict):
            return []
        if obj.get("type") == "stream_event":
            return self._feed_stream_event(obj)
        mid = ""
        if obj.get("type") == "assistant":
            message = obj.get("message")
            if isinstance(message, dict):
                mid = str(message.get("id") or "")
        # 同一 message.id 已经按增量下发过 → 跳过整块正文，避开重复
        skip_text = bool(mid) and mid in self._streamed
        return _parse_object(obj, skip_text=skip_text)


def parse_stream_line(line: str) -> list[dict[str, Any]]:
    """无状态单行解析（向后兼容的薄封装）。跨行去重请用 :class:`StreamParser`。"""
    return StreamParser().feed(line)
