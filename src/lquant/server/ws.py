"""WebSocket：任务进度推送。

轮询 job 状态并推给前端，任务结束（finished/failed）推终态后关连接。
本地降级模式查进程内注册表，Redis 模式查 RQ Job —— 对外协议一致：
    {"job_id": ..., "status": "started|finished|failed", "result"/"error", "done": bool}
"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from lquant.data.ingest.tasks import get_task
from lquant.market import ticks as ticks_mod
from lquant.server.api.ask import get_event_bus
from lquant.server.jobs import get_job, get_job_record
from lquant.server.progress import get_progress

router = APIRouter()

_POLL_SECONDS = 0.5
_TICK_INTERVAL = 2.0
# 行情源瞬时抖动用连续失败计数兜底，不一次 TicksError 就掐断连接（避免恢复风暴）
_MAX_CONSECUTIVE_FAILURES = 3


@router.websocket("/ws/jobs/{job_id}")
async def job_progress(ws: WebSocket, job_id: str) -> None:
    await ws.accept()
    try:
        loop = asyncio.get_running_loop()
        while True:
            job = await loop.run_in_executor(None, get_job, job_id)
            if job is None:
                # 兜底：job 队列查不到（本地降级注册表丢失 / Redis 清空 / 进程重启），
                # 退回 data_task 表 —— 长任务状态不丢，前端降级轮询同一协议。
                task = await loop.run_in_executor(None, get_task, job_id)
                if task is not None:
                    done = task["status"] in ("ok", "partial", "failed", "interrupted")
                    await ws.send_json({
                        "job_id": job_id,
                        "status": task["status"],
                        "progress": {"done": task["done_symbols"],
                                     "total": task["total_symbols"],
                                     "phase": task["phase"]},
                        "done": done,
                    })
                    if done:
                        break
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                # job_record 兜底：因子评价/回测扫参等非 data_task 任务的重启
                # 遗留记录 —— 发终态帧让前端显示「已中断」而非连接中断
                rec = await loop.run_in_executor(None, get_job_record, job_id)
                if rec is not None and rec["status"] in ("finished", "failed",
                                                         "canceled", "interrupted"):
                    await ws.send_json({
                        "job_id": job_id,
                        "status": rec["status"],
                        "error": rec["error"]
                        or ("任务因服务重启已中断，请重新发起"
                            if rec["status"] == "interrupted" else None),
                        "done": True,
                    })
                    break
                await ws.send_json({"job_id": job_id, "status": "not_found", "done": True})
                break
            status = job.get_status()
            payload: dict = {"job_id": job_id, "status": status,
                             "done": status in ("finished", "failed", "canceled")}
            progress = await loop.run_in_executor(None, get_progress, job_id)
            if progress:
                payload["progress"] = progress
            if status == "finished":
                payload["result"] = _safe_result(job)
            if status == "failed":
                payload["error"] = _safe_error(job)
            await ws.send_json(payload)
            if payload["done"]:
                break
            await asyncio.sleep(_POLL_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError：客户端断开后 send_json on closed socket，静默收尾不刷栈
        return
    finally:
        with contextlib.suppress(Exception):  # 客户端断开等，静默收尾
            await ws.close()


@router.websocket("/ws/ask/{session_id}")
async def ask_stream(ws: WebSocket, session_id: str) -> None:
    """问 AI 事件流：订阅事件总线，逐条转发 AgentEvent；done/error 后不关连接，可继续提问。"""
    await ws.accept()
    bus = get_event_bus()
    q = await bus.subscribe(session_id)
    try:
        while True:
            event = await q.get()
            await ws.send_json(event.model_dump())
    except (WebSocketDisconnect, RuntimeError):
        # 客户端断开：starlette 抛 WebSocketDisconnect 或 RuntimeError，均静默收尾
        pass
    finally:
        await bus.unsubscribe(session_id, q)
        await _close(ws)


@router.websocket("/ws/market/ticks")
async def market_ticks(ws: WebSocket, symbols: str = Query(default="", max_length=400)) -> None:
    """行情快照推送（设计 4.ws 契约）：周期性推 {available, quotes, ts}。

    实时源不可用（断网/未配置）→ 推 available=false + 错误后关连；前端用日线末价兜底。
    标的为空 → 直接降级关连，不挂空连接。
    """
    syms = [s.strip() for s in symbols.split(",") if s.strip()][:50]
    await ws.accept()
    if not syms:
        try:
            await ws.send_json({"available": False, "error": "symbols 为空", "quotes": []})
        finally:
            await _close(ws)
        return
    try:
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            try:
                # fetch_quotes 是同步阻塞（urllib+超时+回退链），必须丢线程池，
                # 否则每 2s 卡死整个事件循环（所有端点/其它 ws）。
                rows = await loop.run_in_executor(None, ticks_mod.fetch_quotes, syms)
                failures = 0
                await ws.send_json({"available": True, "quotes": rows,
                                    "ts": rows[0]["ts"] if rows else None})
            except ticks_mod.TicksError as e:
                failures += 1
                await ws.send_json({"available": False, "error": e.detail, "quotes": []})
                # 连续多次才放弃：源抖动的瞬断不触发恢复风暴
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    break
            await asyncio.sleep(_TICK_INTERVAL)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError：客户端断开后 send_json on closed socket，静默收尾不刷栈
        return
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _close(ws: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await ws.close()


def _safe_result(job) -> object:
    try:
        return job.result
    except Exception:  # noqa: BLE001
        return None


def _safe_error(job) -> str | None:
    try:
        return job.error
    except Exception:  # noqa: BLE001
        return None
