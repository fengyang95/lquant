"""Agent 工具调用留痕：name / 入参 / 出参摘要 / 起止时间 / as-of / 是否降级。

**为什么另起一张表，而不是复用 `ask_messages.tool_calls_json`**

`tool_calls_json` 只有 `[{"name": ..., "args": {...}}]` —— 它记的是「调了什么」。
「这条结论能不能回溯」靠的是另外四件事：**结果摘要、数据时点、耗时、是否降级**，
一样都没记。而且它挂在消息上，消息被「重新生成」删掉时留痕一起消失；留痕需要
能独立查询、独立保留。

**as_of / degraded 是尽力而为，不是猜**

工具返回的是 JSON 文本，本模块只在**源数据里明确写了**的时候给结论：

- `as_of` ← 结果里显式的 `as_of` / `asOf` / `trade_date`；
- `degraded` ← 显式的 `degraded` / `unavailable` / `stale` / `fallback`。

读不到就是 `("", None)`：**未知不等于没降级**，填 `False` 会把「没查」说成
「查过了、没问题」。要让它有值，就由 MCP 工具在载荷里显式写出来。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["MAX_DETAIL", "MAX_SUMMARY", "ToolTrace", "TracedEmitter",
           "extract_meta", "truncate"]

_LOG = logging.getLogger("lquant.agent.trace")


def _now_iso() -> str:
    """与 sessions._now 同精度（毫秒）的 UTC ISO 串，落库可直接比大小。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")

#: 结果全文的落库上限（前端折叠展示；再长也不进库，避免一句话把库撑爆）
MAX_DETAIL = 4000
#: 摘要上限（流式事件已经截到 200 字，这里放宽一点给非流式来源）
MAX_SUMMARY = 400

_AS_OF_KEYS = ("as_of", "asOf", "trade_date", "tradeDate", "data_date")
_DEGRADED_KEYS = ("degraded", "unavailable", "stale", "fallback", "is_stale")


def truncate(text: str, limit: int = MAX_DETAIL) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（已截断，原长 {len(s)} 字）"


def _scopes(payload: dict) -> tuple[dict, ...]:
    """工具载荷的两种信封都看：``{"data": {...}, ...}`` 与直接平铺。"""
    inner = payload.get("data")
    return (inner, payload) if isinstance(inner, dict) else (payload,)


def _as_of_of(scopes: tuple[dict, ...]) -> str:
    for scope in scopes:
        for k in _AS_OF_KEYS:
            v = scope.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:32]
    return ""


def _degraded_of(scopes: tuple[dict, ...]) -> bool | None:
    """只在源数据**显式**写了布尔值时才给结论。

    `unavailable: false` 是「明确可用」，`degraded: true` 是「明确降级」，
    两者都算有结论；写的是非布尔值（字符串/None）一律当未知。
    """
    for scope in scopes:
        for k in _DEGRADED_KEYS:
            v = scope.get(k)
            if isinstance(v, bool):
                return v
    return None


def extract_meta(text: str) -> tuple[str, bool | None]:
    """从工具结果文本里提取 (as_of, degraded)。

    - 能解析成 JSON 对象 → 查显式字段（含 ``data`` 一层）；
    - 字符串形如 ``2026-10-08`` → 当作 as_of（有些工具直接回日期）；
    - 其余 → ``("", None)``。
    """
    if not text:
        return "", None
    raw = str(text).strip()
    if raw[:1] in "{[":
        try:
            payload = json.loads(raw)
        except ValueError:
            return "", None
        if isinstance(payload, dict):
            scopes = _scopes(payload)
            return _as_of_of(scopes), _degraded_of(scopes)
        return "", None
    if len(raw) == 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw, None
    return "", None


@dataclass
class ToolTrace:
    """一条工具调用留痕。"""

    session_id: str
    run_id: str
    seq: int
    name: str
    args: dict = field(default_factory=dict)
    status: str = "running"          # running | ok | error
    summary: str = ""
    detail: str = ""
    as_of: str = ""
    degraded: bool | None = None     # None = 未知
    error: str = ""
    message_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int | None = None
    #: 单调起点，只用于算耗时；不落库
    _t0: float = field(default=0.0, repr=False, compare=False)

    def to_row(self) -> dict:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "name": self.name,
            "args_json": json.dumps(self.args or {}, ensure_ascii=False, default=str),
            "status": self.status,
            "summary": truncate(self.summary, MAX_SUMMARY),
            "detail": truncate(self.detail),
            "as_of": self.as_of,
            "degraded": None if self.degraded is None else int(self.degraded),
            "error": truncate(self.error, MAX_SUMMARY),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
        }


class TracedEmitter:
    """包一层 `on_event`：`tool_call` 开一条留痕，`tool_result` 收口。

    **配对按 FIFO**：CLI 的 result 块不一定带工具名（claude 靠 tool_use_id 回填，
    codex 的 shell 调用干脆没有），按「未闭合调用」的顺序配对最稳；只有结果、
    没有调用的事件也照样记一条（名字留空），过程不丢。

    留痕写库失败**绝不能**影响对话：``_observe`` 整体 try/except，出错只记日志，
    事件照原样往下发。
    """

    def __init__(self, sid: str, run_id: str, emit: Callable[[Any], Any],
                 sink: Callable[[dict], Any], *, msg_id: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sid = sid
        self._run_id = run_id
        self._emit = emit
        self._sink = sink
        self._msg_id = msg_id or (lambda: "")
        self._clock = clock
        self._open: list[ToolTrace] = []
        self._seq = 0

    async def __call__(self, ev: Any) -> None:
        try:
            await self.observe(ev)
        except Exception:  # noqa: BLE001 - 留痕不该影响对话
            _LOG.warning("工具留痕失败 name=%s", getattr(ev, "name", ""), exc_info=True)
        await self._emit(ev)

    async def observe(self, ev: Any) -> None:
        kind = getattr(ev, "type", "")
        if kind == "tool_call":
            self._seq += 1
            tr = ToolTrace(session_id=self._sid, run_id=self._run_id, seq=self._seq,
                           name=getattr(ev, "name", "") or "",
                           args=dict(getattr(ev, "args", None) or {}),
                           message_id=self._msg_id(),
                           started_at=_now_iso(), _t0=self._clock())
            self._open.append(tr)
            await self._sink(tr.to_row())
        elif kind == "tool_result":
            tr = self._take(getattr(ev, "name", "") or "")
            text = getattr(ev, "text", "") or ""
            summary = getattr(ev, "summary", "") or ""
            as_of, degraded = extract_meta(text or summary)
            is_error = bool((getattr(ev, "data", None) or {}).get("is_error"))
            tr.name = tr.name or (getattr(ev, "name", "") or "")
            tr.status = "error" if is_error else "ok"
            tr.summary = summary or truncate(text, MAX_SUMMARY)
            tr.detail = text
            tr.error = tr.detail if is_error else ""
            tr.as_of = as_of
            tr.degraded = degraded
            tr.message_id = tr.message_id or self._msg_id()
            tr.finished_at = _now_iso()
            tr.duration_ms = int(max(0.0, self._clock() - tr._t0) * 1000)
            await self._sink(tr.to_row())

    def _take(self, name: str) -> ToolTrace:
        """取一条未闭合的调用；名字对得上优先，否则按 FIFO。"""
        for i, tr in enumerate(self._open):
            if tr.name and name and tr.name == name:
                return self._open.pop(i)
        if self._open:
            return self._open.pop(0)
        # 只有结果没有调用：也记一条，别丢
        self._seq += 1
        return ToolTrace(session_id=self._sid, run_id=self._run_id, seq=self._seq,
                         name=name, started_at=_now_iso(), _t0=self._clock())
