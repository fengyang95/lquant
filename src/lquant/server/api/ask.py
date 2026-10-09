"""问 AI：会话 CRUD + 发消息（agent 后台运行，事件走 WS）。

能力配置的两档口径（``PATCH /sessions/{sid}/config``）：

- ``provider`` **不可改**：CLI 侧会话 id（claude 的 session_id / codex 的
  thread_id）共用一列，换 provider 后续接的是另一个 CLI 的会话，上下文会串。
  会话级操作一律按锁定的 provider 路由（``get_service_for_session``）。
  想换后端接着问，走 ``POST /sessions/{sid}/fork`` **另开一条**会话。
- ``skills`` / ``mcp_tools`` **可改**：不参与会话寻址，每轮回答都按当前配置
  重建工作区（``CliAgentService._workspace_for``），改完下一轮生效。
- ``timeout_seconds`` / ``skip_permissions`` **可改**：会话级运行参数，
  权限与超时都是**每次运行**解析的（``CliAgentService._run``），下一轮生效。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from lquant.agent.capabilities import (
    MUTABLE_SESSION_FIELDS,
    PROVIDERS,
    CapabilityError,
    normalize_agent_config,
    normalize_capability_update,
)
from lquant.agent.schemas import AgentEvent
from lquant.agent.service import (
    get_agent_service,
    get_service_for_session,
    running_runs_all,
)
from lquant.agent.sessions import render_briefing
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


@router.get("/sessions/{sid}/trace")
async def get_trace(sid: str, limit: int = 200):
    """已落库的工具调用留痕（过程轨的事实源）。

    与前端内存里的过程轨（``web/src/lib/ask-stream.ts``）分工不同：那边是
    **这一轮**的可视化，刷新即失；这里是**可回溯**的落库记录。
    """
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    return await svc.store.list_tool_trace(sid, limit=limit)


@router.get("/sessions/{sid}/verdicts")
async def get_verdicts(sid: str, limit: int = 50):
    """本会话提交过的结构化结论（新→旧）。"""
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    return await svc.store.list_verdicts(sid, limit=limit)


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

    # 同步落库 user 消息，再起后台任务跑 agent。
    # 旧写法是「先起任务、再轮询 get_messages 等 user 消息落库」，两个毛病：
    # 同会话并发时会取到别人那条 user 消息；轮询上限到点就 500 并把任务掐掉。
    user_msg = await svc.persist_user_message(sid, content)
    _start_run(svc, sid, content, user_msg)
    return JSONResponse(
        status_code=202,
        content={"user_message": user_msg.model_dump(), "agent_task": "started"},
    )


@router.post("/sessions/{sid}/regenerate")
async def regenerate_session(sid: str, body: dict | None = None):
    """重跑最后一条提问（保留那条 user 消息，删掉它后面的回答）。

    ``body.provider`` 不支持 —— 换后端是 ``/fork``。这里只重跑，不换人。

    为什么删掉旧答案而不是留着再摞一条：同一条问题下面并存两个答案，界面上
    像是「它答了两遍」，而落库的事实源也分不清哪个才是当前答案（下一轮
    ``--resume`` 续接时更是两边都带着）。重跑就是**替换**，语义才唯一。
    """
    b = body or {}
    if b.get("provider"):
        raise HTTPException(400, "重新生成不换后端；换后端请用「另开会话」")
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    if svc.is_busy(sid):
        raise HTTPException(409, "该会话已有正在执行的回答，请等待完成或先取消")
    msgs = await svc.store.messages(sid)
    last_user = next((m for m in reversed(msgs) if m.role == "user"), None)
    if last_user is None:
        raise HTTPException(400, "该会话还没有提问，无法重新生成")
    removed = await svc.store.delete_assistant_after(sid, last_user.id)
    _start_run(svc, sid, last_user.content, last_user)
    return JSONResponse(
        status_code=202,
        content={"user_message": last_user.model_dump(),
                 "agent_task": "started", "replaced_messages": removed},
    )


@router.post("/sessions/{sid}/fork")
async def fork_session(sid: str, body: dict | None = None):
    """复制上下文到**另一个 provider** 的新会话（老会话原样不动）。

    为什么要另开会话而不是改 provider：CLI 侧会话 id 共用一列，中途换 provider
    就是拿着 claude 的 session_id 去 ``codex exec resume``，续接的是别人家的
    会话（串台）。所以「换个后端接着问」只能是新会话。

    上下文怎么带过去：CLI 的历史在**另一个 CLI 那边**，带不过去，所以把原会话
    的消息渲染成一段简报放进新会话的 ``context.briefing``，由
    ``CliAgentService._with_briefing`` 在**第一次调用**时拼进 prompt。新会话的
    界面上仍然是干净的对话（简报不是一条 user 消息），用户在界面上看不到
    一大段转述。
    """
    target = str((body or {}).get("provider") or "").strip()
    if target not in PROVIDERS:
        raise HTTPException(400, f"未知 provider: {target}（可选：{'/'.join(PROVIDERS)}）")
    src_svc = await get_service_for_session(sid)
    src = await src_svc.store.get(sid)
    if src is None:
        raise HTTPException(404, "会话不存在")
    current = src.agent_config.get("provider") or (await get_agent_service()).provider
    if current == target:
        raise HTTPException(400, f"目标 provider 与会话相同（{current}），换一个才有意义")

    msgs = await src_svc.store.messages(sid)
    # 只搬「配置」，不搬 provider 之外的历史事实：skills / mcp_tools 是这一路
    # 会话攒下来的能力集，run 参数（超时/权限）同理 —— 都是用户对新会话的预期。
    # provider 换成目标值，注意**不要**带 cli session id（那属于另一个 CLI）。
    keep = (*MUTABLE_SESSION_FIELDS, "provider")
    cfg = {k: v for k, v in src.agent_config.items() if k in keep}
    cfg["provider"] = target
    context = dict(src.context or {})
    context["forked_from"] = sid
    briefing = render_briefing(msgs)
    if briefing:
        context["briefing"] = briefing
    dst_svc = await get_agent_service(target)
    created = await dst_svc.create_session(
        context, cfg, title=f"{src.title} → {target}")
    return {
        **created.model_dump(),
        "source_session_id": sid,
        "source_provider": current,
        "copied_messages": len([m for m in msgs if m.role in ("user", "assistant")]),
        "briefing_chars": len(briefing),
    }


@router.get("/runs")
async def list_runs():
    """正在跑的 agent 回答（跨 provider）。

    ``GET /sessions`` 只看得见会话，看不出「谁正在跑、跑了多久、pid 是多少」；
    用户在「问 AI」页发现转圈不停时，需要这份列表才能判断该等还是该杀。
    """
    from lquant.agent.service import max_concurrent_runs  # noqa: PLC0415

    return {"runs": running_runs_all(), "max_concurrent_runs": max_concurrent_runs()}


@router.post("/runs/{sid}/kill")
async def kill_run(sid: str):
    """终止某个会话正在跑的回答（等价于该会话的 /cancel，但按 sid 全局找）。

    存在意义是「运行中」列表的每一行都能直接点掉，不用先切到那条会话。
    """
    svc = await get_service_for_session(sid)
    if await svc.store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    ran = svc.is_busy(sid)
    await svc.cancel(sid)
    return {"ok": True, "killed": ran}


def _start_run(svc, sid: str, content: str, user_msg) -> None:
    """起后台任务跑 agent，事件走事件总线。

    ``send_message`` 与 ``regenerate`` 共用同一份实现：两条入口如果各起一套
    后台任务，就会出现「一条路径发 error 事件、另一条只往库里写」这种分叉，
    而前端只认事件。
    """
    bus = get_event_bus()

    async def on_event(e: AgentEvent) -> None:
        await bus.publish(sid, e)

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
