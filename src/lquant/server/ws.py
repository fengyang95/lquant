"""WebSocket：任务进度推送。

轮询 job 状态并推给前端，任务结束（finished/failed）推终态后关连接。
本地降级模式查进程内注册表，Redis 模式查 RQ Job —— 对外协议一致：
    {"job_id": ..., "status": "started|finished|failed", "result"/"error", "done": bool}
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from lquant.market import ticks as ticks_mod
from lquant.server.jobs import get_job

router = APIRouter()

_POLL_SECONDS = 0.5
_TICK_INTERVAL = 2.0
# 行情源瞬时抖动用连续失败计数兜底，不一次 TicksError 就掐断连接（避免恢复风暴）
_MAX_CONSECUTIVE_FAILURES = 3


@router.websocket("/ws/jobs/{job_id}")
async def job_progress(ws: WebSocket, job_id: str) -> None:
    await ws.accept()
    try:
        while True:
            loop = asyncio.get_event_loop()
            job = await loop.run_in_executor(None, get_job, job_id)
            if job is None:
                await ws.send_json({"job_id": job_id, "status": "not_found", "done": True})
                break
            status = job.get_status()
            payload: dict = {"job_id": job_id, "status": status, "done": status in ("finished", "failed")}
            if status == "finished":
                payload["result"] = _safe_result(job)
            elif status == "failed":
                payload["error"] = _safe_error(job)
            await ws.send_json(payload)
            if payload["done"]:
                break
            await asyncio.sleep(_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - 客户端断开等，静默收尾
        pass
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


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
        loop = asyncio.get_event_loop()
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
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - 客户端断开等，静默收尾
        pass
    finally:
        await _close(ws)


async def _close(ws: WebSocket) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass


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
