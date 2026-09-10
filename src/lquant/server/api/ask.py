"""问 AI：会话 CRUD + 发消息（agent 后台运行，事件走 WS）。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from lquant.agent.schemas import AgentEvent
from lquant.agent.service import get_agent_service
from lquant.server.api.ask_bus import AskEventBus
from lquant.server.envelope import make_router

_LOG = logging.getLogger(__name__)

router = make_router(prefix="/ask", tags=["ask"])

_bus = AskEventBus()

# 持有后台任务引用，防止协程被 GC（done 后自动移除）
_tasks: set[asyncio.Task] = set()

# 等 user 消息落库的轮询参数：40 × 50ms = 2s 上限
_POLL_TIMES = 40
_POLL_INTERVAL = 0.05


def get_event_bus() -> AskEventBus:
    return _bus


@router.post("/sessions")
async def create_session(body: dict | None = None):
    svc = await get_agent_service()
    context = (body or {}).get("context")
    return (await svc.create_session(context)).model_dump()


@router.get("/sessions")
async def list_sessions():
    svc = await get_agent_service()
    return [s.model_dump() for s in await svc.list_sessions()]


@router.delete("/sessions/{sid}")
async def delete_session(sid: str):
    svc = await get_agent_service()
    await svc.delete_session(sid)  # 内部先 cancel 后台任务
    return {"ok": True}


@router.post("/sessions/{sid}/cancel")
async def cancel_session(sid: str):
    svc = await get_agent_service()
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    await svc.cancel(sid)
    return {"ok": True}


@router.get("/sessions/{sid}/messages")
async def get_messages(sid: str):
    svc = await get_agent_service()
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    return [m.model_dump() for m in await svc.get_messages(sid)]


@router.post("/sessions/{sid}/messages")
async def send_message(sid: str, body: dict):
    svc = await get_agent_service()
    content = str((body or {}).get("content", ""))
    if not content.strip():
        raise HTTPException(400, "消息不能为空")
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")

    bus = get_event_bus()

    async def on_event(e: AgentEvent) -> None:
        await bus.publish(sid, e)

    async def run() -> None:
        try:
            await svc.send_message(sid, content, on_event)
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

    # 等 user 消息落库后再返回（上限 2s），保证前端拿到完整 user_message
    user = None
    for _ in range(_POLL_TIMES):
        msgs = await svc.get_messages(sid)
        user = next((m for m in reversed(msgs) if m.role == "user"), None)
        if user is not None:
            break
        await asyncio.sleep(_POLL_INTERVAL)
    if user is None:
        raise HTTPException(500, "消息落库失败")
    return JSONResponse(
        status_code=202,
        content={"user_message": user.model_dump(), "agent_task": "started"},
    )
