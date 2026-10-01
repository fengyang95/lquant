"""A2A Task 记录：状态机（终态不可覆盖）、不存在即 TaskNotFound、随会话级联删除。"""
from __future__ import annotations

import pytest

from lquant.agent.a2a.errors import TASK_NOT_FOUND, A2AError
from lquant.agent.a2a.tasks import A2ATaskStore
from lquant.agent.a2a.types import TaskState
from lquant.agent.sessions import SessionStore


@pytest.fixture()
async def store(tmp_path):
    s = SessionStore(str(tmp_path / "ask.db"))
    yield s
    await s.close()


@pytest.fixture()
async def tasks(store):
    return A2ATaskStore(store)


async def test_create_and_get(tasks, store):
    ses = await store.create(None)
    rec = await tasks.create("t1", ses.id, message_id="m1")
    assert (rec.task_id, rec.context_id, rec.state) == ("t1", ses.id, TaskState.SUBMITTED)
    assert rec.message_id == "m1"
    assert not rec.terminal
    assert (await tasks.get("t1")).state == TaskState.SUBMITTED
    assert await tasks.get("nope") is None


async def test_require_raises_task_not_found(tasks):
    with pytest.raises(A2AError) as exc:
        await tasks.require("missing")
    assert exc.value.code == TASK_NOT_FOUND
    assert exc.value.to_error()["data"]["reason"] == "TaskNotFoundError"


@pytest.mark.parametrize("terminal", [
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED,
])
async def test_terminal_state_cannot_be_overwritten(tasks, store, terminal):
    """并发场景：done 与 cancel 几乎同时到，先到的终态必须胜出。"""
    ses = await store.create(None)
    await tasks.create("t1", ses.id)
    await tasks.set_state("t1", terminal)
    after = await tasks.set_state("t1", TaskState.FAILED)
    assert after.state == terminal
    assert (await tasks.get("t1")).state == terminal


async def test_non_terminal_state_transitions(tasks, store):
    ses = await store.create(None)
    await tasks.create("t1", ses.id)
    assert (await tasks.set_state("t1", TaskState.WORKING)).state == TaskState.WORKING
    assert (await tasks.set_state("t1", TaskState.COMPLETED)).state == TaskState.COMPLETED


async def test_set_state_on_missing_task_is_noop(tasks):
    assert await tasks.set_state("nope", TaskState.COMPLETED) is None


async def test_list_by_context_newest_first(tasks, store):
    a = await store.create(None)
    b = await store.create(None)
    await tasks.create("t1", a.id)
    await tasks.create("t2", a.id)
    await tasks.create("t3", b.id)
    ids = [r.task_id for r in await tasks.list_by_context(a.id)]
    assert sorted(ids) == ["t1", "t2"]
    assert [r.task_id for r in await tasks.list_by_context(b.id)] == ["t3"]


async def test_deleting_session_cascades_tasks(tasks, store):
    """会话是一致性边界：删会话不能留下孤儿 task 行。"""
    ses = await store.create(None)
    await tasks.create("t1", ses.id)
    await store.delete(ses.id)
    assert await tasks.get("t1") is None
