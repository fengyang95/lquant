"""A2A Task 领域层：状态机 + 记录读取。

薄 SQL 落在 ``SessionStore``（同一个 ask.db / 同一个 aiosqlite 连接），
这里只放语义：终态不可再流转、不存在即 TaskNotFoundError。
"""
from __future__ import annotations

from dataclasses import dataclass

from lquant.agent.a2a.errors import TASK_NOT_FOUND, A2AError
from lquant.agent.a2a.types import TERMINAL_STATES, TaskState
from lquant.agent.sessions import SessionStore


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    context_id: str
    state: str
    message_id: str
    created_at: str
    updated_at: str

    @staticmethod
    def of(row: dict) -> TaskRecord:
        return TaskRecord(**row)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class A2ATaskStore:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def create(self, task_id: str, context_id: str, *,
                     message_id: str = "") -> TaskRecord:
        await self._store.create_a2a_task(
            task_id, context_id, TaskState.SUBMITTED, message_id)
        return await self.require(task_id)

    async def get(self, task_id: str) -> TaskRecord | None:
        row = await self._store.get_a2a_task(task_id)
        return TaskRecord.of(row) if row else None

    async def require(self, task_id: str) -> TaskRecord:
        rec = await self.get(task_id)
        if rec is None:
            raise A2AError(TASK_NOT_FOUND, f"任务不存在: {task_id}")
        return rec

    async def set_state(self, task_id: str, state: str) -> TaskRecord | None:
        """流转状态；**已是终态则忽略**（终态不可覆盖）。

        并发场景很实在：用户 cancel 与正常 done 可能几乎同时到达，
        先到终态者胜，后到的写入必须被丢弃，否则任务会「完成后再变失败」。
        """
        rec = await self.get(task_id)
        if rec is None or rec.terminal:
            return rec
        await self._store.set_a2a_task_state(task_id, state)
        return await self.get(task_id)

    async def list_by_context(self, context_id: str, limit: int = 50) -> list[TaskRecord]:
        rows = await self._store.list_a2a_tasks(context_id, limit)
        return [TaskRecord.of(r) for r in rows]
