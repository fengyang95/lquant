"""A2A 执行体：Message → Task → AgentService（内置 Claude Code）→ A2A 帧。

两条出口共用同一段编排：

- ``send()``    阻塞到终态，返回完整 Task（``message/send``）
- ``stream()``  逐帧 yield，直到终态（``message/stream``，SSE）

实现要点：

1. **A2A 的 contextId 就是 ``ask_sessions.id``** —— 共用事实源，不另起一套会话。
   ``tasks/get`` 的 history 也直接由落库消息重建。
2. **per-run 队列**而不是全局事件总线：A2A 的消费方可能中途断开，
   生产者（claude 子进程）不能因此被拖住；同时旁路发一份到 ``sink``，
   让 /ask 页面在外部 Agent 提问时也能实时看到。
3. ``_pump`` 吞掉 ``CancelledError``：用户取消走的是
   ``service.cancel(sid)`` → 取消的正是 ``_pump`` 自己，
   收尾（落终态、投 sentinel）必须照常完成。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from lquant.agent.a2a.errors import (
    INVALID_PARAMS,
    TASK_NOT_CANCELABLE,
    A2AError,
    SessionBusyError,
)
from lquant.agent.a2a.mapper import FrameBuilder, ask_message_to_a2a, prompt_from_message
from lquant.agent.a2a.tasks import A2ATaskStore
from lquant.agent.a2a.types import Artifact, Task, TaskState, TaskStatus, stream_task, text_part
from lquant.agent.a2a.types import Message as A2AMessage
from lquant.agent.schemas import AgentEvent
from lquant.agent.schemas import Message as AskMessage
from lquant.agent.service import AgentService, get_agent_service

_LOG = logging.getLogger(__name__)

#: 队列结束哨兵（None 可能是合法载荷，不能当哨兵用）
_SENTINEL = object()
#: tasks/get 回显 history 的条数上限
_HISTORY_LIMIT = 20
#: tasks/cancel 等待在跑任务收尾的上限（超时也返回，状态异步收敛）
_CANCEL_WAIT_SECONDS = 5.0

#: 旁路事件出口（session_id, event）→ 供 WS 事件总线复用
Sink = Callable[[str, AgentEvent], Awaitable[None]]


@dataclass
class _Run:
    task_id: str
    context_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    builder: FrameBuilder | None = None
    task: asyncio.Task | None = None
    user_message: AskMessage | None = None
    canceled: bool = False
    finished: bool = False


class A2AExecutor:
    def __init__(self, service: AgentService, *, sink: Sink | None = None) -> None:
        self.service = service
        self.sink = sink
        self._store = service.store
        self._tasks = A2ATaskStore(self._store)
        self._runs: dict[str, _Run] = {}

    def set_sink(self, sink: Sink | None) -> None:
        """接旁路出口（幂等；由 server 层注入，agent 层不反向依赖 server）。"""
        self.sink = sink

    # ---- 生命周期 ---------------------------------------------------------

    async def start(self, message: A2AMessage, context_id: str | None = None) -> _Run:
        """建会话 + 建 Task + 起后台 runner，返回可消费的 _Run。

        第一帧（Task 对象）在这里就入队：调用方拿到 run 时流已经符合
        「stream MUST begin with the Task object」。

        ``is_busy`` 预检查只是**快路径**（能干净地回 JSON-RPC 错误而不是发一个
        注定失败的任务）；真正权威的单飞守卫是 ``AgentService`` 的会话槽位
        ``claim`` —— 槽位由真正干活的 task 持有，谁先拿到谁跑，后到的拿 409。
        """
        prompt = prompt_from_message(message)
        session_id = await self._resolve_context(context_id or message.context_id or "")
        if self.service.is_busy(session_id):
            raise SessionBusyError("该 contextId 已有正在执行的回答，请等待完成或先取消")
        task_id = uuid.uuid4().hex
        await self._tasks.create(task_id, session_id, message_id=message.message_id)
        run = _Run(task_id=task_id, context_id=session_id)
        self._runs[task_id] = run
        # 先落 user 消息：/ask 页面与 tasks/get 的 history 立刻可见（事实源一致）
        run.user_message = await self.service.persist_user_message(session_id, prompt)
        await run.queue.put(stream_task(await self.build_task(task_id)))
        run.task = asyncio.create_task(self._pump(run, prompt, run.user_message))
        return run

    async def send(self, message: A2AMessage, context_id: str | None = None) -> Task:
        run = await self.start(message, context_id)
        async for _frame in self._drain(run):
            pass
        return await self.build_task(run.task_id)

    async def open_stream(self, message: A2AMessage,
                          context_id: str | None = None) -> AsyncIterator[dict]:
        """先 start（可能抛错），再把帧序列交出去。

        与「直接返回 async generator」的区别：``start()`` 的错误（contextId 不存在、
        会话忙）必须在**返回之前**抛出 —— SSE 响应头一旦发出，状态码就改不了了。
        """
        run = await self.start(message, context_id)
        return self._drain(run)

    async def get(self, task_id: str, *, history_length: int | None = None) -> Task:
        return await self.build_task(task_id, history_length=history_length)

    async def cancel(self, task_id: str) -> Task:
        rec = await self._tasks.require(task_id)
        if rec.terminal:
            raise A2AError(TASK_NOT_CANCELABLE,
                           f"任务已是终态（{rec.state}），无法取消")
        run = self._runs.get(task_id)
        if run is None or run.finished:
            # 进程重启后内存里没有 run：只把落库状态收敛到 CANCELED
            await self._tasks.set_state(task_id, TaskState.CANCELED)
        else:
            run.canceled = True
            if run.builder is not None:
                run.builder.mark_canceled()
            await self.service.cancel(rec.context_id)
            if run.task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(run.task, _CANCEL_WAIT_SECONDS)
        return await self.build_task(task_id)

    # ---- 内部 -------------------------------------------------------------

    async def _resolve_context(self, context_id: str) -> str:
        context_id = context_id.strip()
        if not context_id:
            return (await self._store.create({"source": "a2a"})).id
        if await self._store.get(context_id) is None:
            # 不是「请求体非法」而是「参数值指向不存在的资源」→ -32602
            raise A2AError(INVALID_PARAMS, f"contextId 不存在: {context_id}")
        return context_id

    async def _drain(self, run: _Run) -> AsyncIterator[dict]:
        while True:
            frame = await run.queue.get()
            if frame is _SENTINEL:
                return
            yield frame

    async def _pump(self, run: _Run, prompt: str, user_msg: AskMessage) -> None:
        builder = FrameBuilder(run.task_id, run.context_id)
        run.builder = builder
        try:
            await self._tasks.set_state(run.task_id, TaskState.WORKING)
            await run.queue.put(builder.working())

            async def on_event(ev: AgentEvent) -> None:
                if self.sink is not None:
                    try:
                        await self.sink(run.context_id, ev)
                    except Exception:  # noqa: BLE001 - 旁路上报失败不影响 A2A 主链路
                        _LOG.debug("A2A 事件旁路发布失败 sid=%s", run.context_id,
                                   exc_info=True)
                for frame in builder.on_event(ev):
                    await run.queue.put(frame)

            await self.service.send_message(
                run.context_id, prompt, on_event, user_msg=user_msg)
        except asyncio.CancelledError:
            # 用户取消：service 已发「已中断」事件（若事件没来得及发，这里补一帧）
            if not builder.canceled:
                builder.mark_canceled()
                for frame in builder.on_event(
                        AgentEvent(type="error", message="已中断")):
                    run.queue.put_nowait(frame)
        except Exception as e:  # noqa: BLE001 - 后台 runner 的异常只能落成终态帧
            _LOG.exception("A2A 任务执行失败 task=%s", run.task_id)
            if not builder.terminal:
                for frame in builder.on_event(
                        AgentEvent(type="error", message=str(e))):
                    run.queue.put_nowait(frame)
        finally:
            await self._finish(run, builder)

    async def _finish(self, run: _Run, builder: FrameBuilder) -> None:
        state = builder.state
        if not builder.terminal:
            state = TaskState.CANCELED if (builder.canceled or run.canceled) \
                else TaskState.FAILED
        with contextlib.suppress(Exception):  # 落库失败不挡住流收尾
            await self._tasks.set_state(run.task_id, state)
        run.finished = True
        self._runs.pop(run.task_id, None)
        # 无界队列 put_nowait 不会挂起：即使在取消上下文中也能投出哨兵
        run.queue.put_nowait(_SENTINEL)

    async def build_task(self, task_id: str, *,
                         history_length: int | None = None) -> Task:
        rec = await self._tasks.require(task_id)
        msgs = await self._store.messages(rec.context_id)
        limit = _HISTORY_LIMIT if history_length is None else max(0, int(history_length))
        history = [ask_message_to_a2a(m, context_id=rec.context_id)
                   for m in msgs if m.content.strip()][-limit:] if limit else []
        answer = next((m for m in reversed(msgs)
                       if m.role == "assistant" and m.content.strip()), None)
        artifacts = None
        if answer is not None:
            artifacts = [Artifact(artifactId=f"{task_id}-answer", name="answer",
                                  parts=[text_part(answer.content)])]
        return Task(
            id=task_id,
            status=TaskStatus(state=rec.state, timestamp=rec.updated_at),
            contextId=rec.context_id,
            artifacts=artifacts,
            history=history or None,
        )


_executor: A2AExecutor | None = None


async def get_a2a_executor(sink: Sink | None = None) -> A2AExecutor:
    """进程内单例（``_runs`` 必须跨请求存活，不能每次请求新建）。

    service 换了（测试清单例 / provider 切换）就重建，避免拿着已关闭的 store。
    """
    global _executor
    svc = await get_agent_service()
    if _executor is None or _executor.service is not svc:
        _executor = A2AExecutor(svc)
    if sink is not None:
        _executor.set_sink(sink)
    return _executor


def reset_a2a_executor() -> None:
    global _executor
    _executor = None
