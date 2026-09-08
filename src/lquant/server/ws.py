"""WebSocket：任务进度推送。

轮询 job 状态并推给前端，任务结束（finished/failed）推终态后关连接。
本地降级模式查进程内注册表，Redis 模式查 RQ Job —— 对外协议一致：
    {"job_id": ..., "status": "started|finished|failed", "result"/"error", "done": bool}
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lquant.server.jobs import get_job

router = APIRouter()

_POLL_SECONDS = 0.5


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
