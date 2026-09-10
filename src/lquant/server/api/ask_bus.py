"""问 AI 事件总线：send_message 后台任务发布，WS 订阅转发。内存实现，单进程。"""
from __future__ import annotations

import asyncio

from lquant.agent.schemas import AgentEvent


class AskEventBus:
    """按会话 ID 分组的内存事件总线（publish/subscribe/unsubscribe）。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, sid: str, event: AgentEvent) -> None:
        async with self._lock:
            subs = set(self._subs.get(sid, ()))
        for q in subs:
            await q.put(event)

    async def subscribe(self, sid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subs.setdefault(sid, set()).add(q)
        return q

    async def unsubscribe(self, sid: str, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.get(sid, set()).discard(q)
