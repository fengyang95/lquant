"""定时同步管理：作业 CRUD / 立即执行 / 运行历史 / 数据覆盖度。

后台常驻线程由 server.main 启动时拉起（LQ_SYNC_WORKER=0 可关），
这里只做管理与手动触发 —— 接口签名与前端按「列表 + 轮询历史」开发，
以后挪进 jobs 队列无感。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lquant.sync import manager
from lquant.sync.manager import _KINDS

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/jobs")
def get_jobs() -> list[dict]:
    """同步作业列表（含下次到期判断所需的 last_run 状态）。"""
    return manager.list_jobs()


# 与 manager._KINDS 对齐：reference/daily_basic/financial/news/backfill
# 也必须能经 API 编辑/手跑，不能只留旧三类。
_KIND_PATTERN = "^(?:" + "|".join(_KINDS) + ")$"


class JobIn(BaseModel):
    sync_id: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=_KIND_PATTERN)
    schedule_time: str = Field(pattern=r"^\d{2}:\d{2}(,\d{2}:\d{2})*$")
    weekdays: str = Field(default="1,2,3,4,5", max_length=20)
    params: dict = Field(default_factory=dict)
    enabled: bool = True


@router.post("/jobs")
def put_job(j: JobIn) -> dict:
    """新建/覆盖一个同步作业。"""
    try:
        return manager.upsert_job(j.sync_id, j.name, j.kind, j.schedule_time,
                                  j.weekdays, j.params, j.enabled)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/jobs/{sync_id}/toggle")
def toggle_job(sync_id: str, req: ToggleIn) -> dict:
    exists = [j for j in manager.list_jobs() if j["sync_id"] == sync_id]
    if not exists:
        raise HTTPException(404, f"作业不存在: {sync_id}")
    manager.set_enabled(sync_id, req.enabled)
    return {"sync_id": sync_id, "enabled": req.enabled}


@router.delete("/jobs/{sync_id}")
def remove_job(sync_id: str) -> dict:
    manager.delete_job(sync_id)
    return {"deleted": sync_id}


class RunIn(BaseModel):
    sync_id: str | None = None
    kind: str | None = Field(default=None, pattern=_KIND_PATTERN)
    schedule: str | None = Field(default=None,
                                 description="collect 时的采集时点（close/evening/preopen）")
    demo: bool = False


@router.post("/run")
def run_now(req: RunIn) -> dict:
    """立即执行：指定 sync_id，或按 kind 临时构造作业（不落 sync_job）。"""
    if req.sync_id:
        job = next((j for j in manager.list_jobs() if j["sync_id"] == req.sync_id), None)
        if not job:
            raise HTTPException(404, f"作业不存在: {req.sync_id}")
        if req.demo:
            job["params"] = {**job.get("params", {}), "demo": True}
    elif req.kind:
        params: dict = {"demo": req.demo}
        if req.kind == "collect" and req.schedule:
            params["schedule"] = req.schedule
        job = {"sync_id": f"adhoc_{req.kind}", "name": f"手动 {req.kind}",
               "kind": req.kind, "params": params}
    else:
        raise HTTPException(422, "需要 sync_id 或 kind")
    return manager.run_job(job)


@router.get("/history")
def get_history(limit: int = Query(default=50, le=200)) -> list[dict]:
    """运行历史（最新在前）。"""
    return manager.history(limit)


@router.get("/coverage")
def coverage() -> dict:
    """数据覆盖度总览：Parquet 湖 / DuckDB 参考表 / 看板表 三个视角。"""
    import polars as pl

    from lquant.core.db import reader
    from lquant.data.store.parquet import read_daily
    from lquant.market.scheduler import status as market_status

    lake = {"rows": 0, "symbols": 0, "first_day": None, "last_day": None}
    try:
        df = (read_daily().select(["trade_date", "symbol"]).collect())
        if len(df):
            lake = {"rows": len(df), "symbols": df["symbol"].n_unique(),
                    "first_day": str(df["trade_date"].min()),
                    "last_day": str(df["trade_date"].max())}
    except Exception:  # noqa: BLE001
        pass

    ref: list[dict] = []
    try:
        with reader() as con:
            # 单条 UNION ALL：逐表两次往返（COUNT + try/except 兜底）换成
            # 一次往返；无日期列的表 max_date 取 NULL。
            union_sql = " UNION ALL ".join(
                f"SELECT '{t}' AS table_name, COUNT(*) AS row_cnt, "
                f"MAX({col}) AS max_date FROM {t}"
                for t, col in [
                    ("security", "list_date"),
                    ("trade_calendar", "trade_date"),
                    ("etf_meta", "listing_date"),
                    ("adj_factor", "trade_date"),
                    ("financial_pit", "NULL"),
                    ("industry_classify", "NULL"),
                    ("factor_value", "NULL"),
                ])
            rows = con.execute(union_sql).fetchall()
        ref = [{"table": r[0], "rows": r[1] or 0,
                "last_day": str(r[2]) if r[2] else None} for r in rows]
    except Exception:  # noqa: BLE001
        ref = [{"table": t, "rows": 0, "last_day": None} for t in (
            "security", "trade_calendar", "etf_meta", "adj_factor",
            "financial_pit", "industry_classify", "factor_value")]

    board: list[dict] = []
    try:
        st = market_status()
        board = st.to_dicts() if isinstance(st, pl.DataFrame) else st
    except Exception:  # noqa: BLE001
        pass
    for r in board:
        if r.get("first_day"):
            r["first_day"] = str(r["first_day"])
        if r.get("last_day"):
            r["last_day"] = str(r["last_day"])

    return {"lake": lake, "reference": ref, "market_tables": board}
