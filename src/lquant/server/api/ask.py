"""问 AI：会话 CRUD + 发消息（agent 后台运行，事件走 WS）。

能力配置的两档口径（``PATCH /sessions/{sid}/config``）：

- ``provider`` **不可改**：CLI 侧会话 id（claude 的 session_id / codex 的
  thread_id）共用一列，换 provider 后续接的是另一个 CLI 的会话，上下文会串。
  会话级操作一律按锁定的 provider 路由（``get_service_for_session``）。
- ``skills`` / ``mcp_tools`` **可改**：不参与会话寻址，每轮回答都按当前配置
  重建工作区（``CliAgentService._workspace_for``），改完下一轮生效。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from lquant.agent.capabilities import (
    CapabilityError,
    normalize_agent_config,
    normalize_capability_update,
)
from lquant.agent.schemas import AgentEvent
from lquant.agent.service import get_agent_service, get_service_for_session
from lquant.server.api.ask_bus import AskEventBus
from lquant.server.envelope import make_router

_LOG = logging.getLogger(__name__)

router = make_router(prefix="/ask", tags=["ask"])

_bus = AskEventBus()

# 持有后台任务引用，防止协程被 GC（done 后自动移除）
_tasks: set[asyncio.Task] = set()


def get_event_bus() -> AskEventBus:
    return _bus


@router.post("/sessions")
async def create_session(body: dict | None = None):
    """建会话。可选带 ``provider`` / ``skills`` / ``mcp_tools`` 锁定本次能力集。

    三者都不传 = 走全局默认（与改造前行为一致，A2A 也走这条）。
    """
    b = body or {}
    try:
        cfg = normalize_agent_config(b)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    svc = await get_agent_service(cfg.get("provider"))
    return (await svc.create_session(b.get("context"), cfg or None)).model_dump()


@router.get("/sessions")
async def list_sessions():
    svc = await get_agent_service()
    return [s.model_dump() for s in await svc.list_sessions()]


@router.patch("/sessions/{sid}/config")
async def update_session_config(sid: str, body: dict | None = None):
    """会话内改能力配置（只允许 skills / mcp_tools；provider 锁定）。

    provider 为什么不给改：CLI 侧会话 id（claude 的 session_id / codex 的
    thread_id）共用一列，换 provider 后续接的是另一个 CLI 的会话，上下文直接串。
    所以按会话锁定的 provider 路由（``get_service_for_session``），不用全局默认实例。

    skills / mcp_tools 为什么能改：它们不参与会话寻址，每一轮回答都按当前
    配置**重建工作区**（``CliAgentService._workspace_for``），改完下一轮生效，
    不需要重开会话。
    """
    try:
        patch = normalize_capability_update(body or {})
    except CapabilityError as e:
        raise HTTPException(400, str(e)) from e
    svc = await get_service_for_session(sid)
    session = await svc.store.set_agent_config(sid, patch)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session.model_dump()


@router.delete("/sessions/{sid}")
async def delete_session(sid: str):
    svc = await get_service_for_session(sid)
    await svc.delete_session(sid)  # 内部先 cancel 后台任务
    return {"ok": True}


@router.post("/sessions/{sid}/cancel")
async def cancel_session(sid: str):
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    await svc.cancel(sid)
    return {"ok": True}


@router.get("/sessions/{sid}/messages")
async def get_messages(sid: str):
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    return [m.model_dump() for m in await svc.get_messages(sid)]


@router.post("/sessions/{sid}/messages")
async def send_message(sid: str, body: dict):
    # 按会话锁定的 provider 路由：运行时引用（任务/子进程）是**按 service 实例**
    # 存的，拿全局默认的实例去取消一个 codex 会话，打的是空表。
    svc = await get_service_for_session(sid)
    content = str((body or {}).get("content", ""))
    if not content.strip():
        raise HTTPException(400, "消息不能为空")
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    # 同会话单飞：已经在跑就 409。运行时引用按会话单槽存，放两条并发进来
    # 会互相覆盖 —— /cancel 打到错进程、先结束的把另一个 pop 成孤儿。
    if svc.is_busy(sid):
        raise HTTPException(409, "该会话已有正在执行的回答，请等待完成或先取消")

    bus = get_event_bus()

    async def on_event(e: AgentEvent) -> None:
        await bus.publish(sid, e)

    # 同步落库 user 消息，再起后台任务跑 agent。
    # 旧写法是「先起任务、再轮询 get_messages 等 user 消息落库」，两个毛病：
    # 同会话并发时会取到别人那条 user 消息；轮询上限到点就 500 并把任务掐掉。
    user_msg = await svc.persist_user_message(sid, content)

    async def run() -> None:
        try:
            await svc.send_message(sid, content, on_event, user_msg=user_msg)
        except asyncio.CancelledError:
            # MockAgentService 内部已发 error 事件；这里兜底意外取消（非 service 内）
            _LOG.info("agent 任务被取消 sid=%s", sid)
            raise
        except Exception as e:  # noqa: BLE001 - 后台任务异常只能走事件通道
            _LOG.exception("agent 后台任务失败 sid=%s", sid)
            await on_event(AgentEvent(type="error", message=str(e)))

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return JSONResponse(
        status_code=202,
        content={"user_message": user_msg.model_dump(), "agent_task": "started"},
    )
