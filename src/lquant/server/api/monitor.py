"""监控 API：实时快照 + 历史聚合（统一信封）。"""
from __future__ import annotations

from fastapi import HTTPException, Query

from lquant.monitor import queries as q
from lquant.server.envelope import make_router

router = make_router(prefix="/monitor", tags=["monitor"])


@router.get("/summary")
def summary() -> dict:
    return q.summary()


@router.get("/workers")
def workers() -> dict:
    return {"procs": q.proc_statuses()}


@router.get("/api-latency")
def api_latency(range: str = Query("1h")) -> dict:
    try:
        return {"series": q.api_latency_series(range),
                "slowest": q.slowest_routes(range)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/tasks")
def tasks(range: str = Query("1h")) -> dict:
    try:
        return {"series": q.task_latency_series(range)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/data-pulls")
def data_pulls() -> dict:
    return q.data_pulls()
