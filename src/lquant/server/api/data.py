"""数据服务：覆盖度 / 标的搜索 / 日线 / 实时行情。

设计约定：
- 「没数据」是常态而不是错误 —— 空湖、空表返回空结构，不抛 500；
- 实时行情走东财 push2（经 em_get 令牌桶限流），网络不通时降级为
  available=false，前端用日线最后一根兜底显示；
- 日线一律读 Parquet 湖（研究态事实源），DuckDB 只存小表。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
import polars as pl
from pydantic import BaseModel, Field

from lquant.core.db import reader
from lquant.data.ingest.tasks import (
    TaskConflictError,
    claim_retry,
    create_task,
    execute_task,
    get_task,
    list_tasks,
    run_claimed_task,
)
from lquant.data.store.parquet import read_daily
from lquant.server.deps import bare_code, resolve_symbol
from lquant.server.jobs import enqueue

router = APIRouter(prefix="/data", tags=["data"])


class TaskIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    params: dict = Field(default_factory=dict)


class CrosscheckIn(BaseModel):
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    peers: list[str] | None = None
    limit: int = Field(default=200, ge=1, le=1000)


class ResolveIn(BaseModel):
    issue_id: str = Field(min_length=1, max_length=64)

# 覆盖度要看的表：名称 → (说明, 是否按 trade_date 取最新)
_COVER_TABLES: list[tuple[str, str]] = [
    ("security", "标的主档"),
    ("trade_calendar", "交易日历"),
    ("financial_pit", "财务 PIT"),
    ("factor_def", "因子定义"),
    ("limit_up_pool", "涨停池"),
    ("limit_down_pool", "跌停池"),
    ("money_flow", "个股资金流"),
    ("sector_daily", "板块日度"),
    ("sentiment_daily", "市场情绪"),
    ("dragon_tiger", "龙虎榜"),
    ("northbound_flow", "北向资金"),
    ("backtest_run", "回测记录"),
    ("ml_run", "ML 实验记录"),
]


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"pong": "data"}


@router.get("/coverage")
def coverage() -> dict:
    """数据覆盖度：湖 + 库一次看全，数据页/排障首屏。"""
    tables: list[dict] = []
    with reader() as con:
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        for t, label in _COVER_TABLES:
            if t not in existing:
                tables.append({"table": t, "label": label, "rows": None, "latest": None})
                continue
            try:
                rows = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                latest = None
                cols = {r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall()}
                if rows and "trade_date" in cols:
                    latest = con.execute(
                        f'SELECT max(trade_date) FROM "{t}"').fetchone()[0]
                tables.append({"table": t, "label": label, "rows": int(rows),
                               "latest": str(latest) if latest else None})
            except Exception:  # noqa: BLE001 - 单表坏了不影响整页
                tables.append({"table": t, "label": label, "rows": None,
                               "latest": None, "error": True})

    lake: dict = {"rows": 0, "symbols": 0, "start": None, "end": None}
    try:
        df = read_daily().select(
            ["symbol", "trade_date"]).collect()
        if len(df):
            lake = {"rows": len(df), "symbols": df["symbol"].n_unique(),
                    "start": str(df["trade_date"].min()),
                    "end": str(df["trade_date"].max())}
    except Exception:  # noqa: BLE001
        lake["error"] = True
    return {"tables": tables, "daily_lake": lake}


@router.get("/coverage/monthly")
def coverage_monthly(
    start: str | None = Query(default=None, max_length=10),
    end: str | None = Query(default=None, max_length=10),
) -> dict:
    """覆盖度按月聚合：每月实际标的数趋势（spec §7.3）。

    口径：avg_symbols = 当月各交易日 distinct symbol 数的算术平均，
    days = 当月有数据的交易日数。「应有标的数」无可靠 PIT 来源
    （security 是当前快照），不做对比 —— 缺口由前端按环比大幅下降
    （如 >30%）标橙，阈值归前端定。空湖返回空数组不报错。
    """
    from datetime import date as _date

    for name, val in (("start", start), ("end", end)):
        if isinstance(val, str) and val:
            try:
                _date.fromisoformat(val)
            except ValueError as e:
                raise HTTPException(422, f"{name} 日期非法: {val}") from e
    try:
        lf = read_daily(start=start, end=end).select(["symbol", "trade_date"])
        if not lf.collect_schema().names():
            return {"rows": []}
        per_day = lf.group_by("trade_date").agg(
            pl.col("symbol").n_unique().alias("n_symbols"))
        monthly = (
            per_day.group_by(
                pl.col("trade_date").dt.strftime("%Y-%m").alias("month"))
            .agg(pl.col("n_symbols").mean().alias("avg_symbols"),
                 pl.col("trade_date").len().alias("days"))
            .sort("month"))
        rows = [{"month": r["month"], "avg_symbols": round(r["avg_symbols"], 1),
                 "days": int(r["days"])} for r in monthly.collect().to_dicts()]
        return {"rows": rows}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - 湖读取异常降级为空，不打断首屏
        from loguru import logger

        logger.warning(f"coverage monthly 聚合失败: {e}")
        return {"rows": []}


@router.get("/securities")
def securities(
    q: str = Query(default="", max_length=20, description="代码/名称关键字"),
    sec_type: str | None = Query(default=None, description="stock/etf/lof/index"),
    limit: int = Query(default=50, le=500),
) -> list[dict]:
    """标的搜索：代码前缀或名称包含，自选股/个股页搜索框用。"""
    sql = ("SELECT symbol, name, sec_type, board, list_date, is_st FROM security "
           "WHERE (delist_date IS NULL OR delist_date > current_date)")
    params: list = []
    if q:
        sql += " AND (symbol LIKE ? OR name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if sec_type:
        sql += " AND sec_type = ?"
        params.append(sec_type)
    sql += f" ORDER BY symbol LIMIT {int(limit)}"
    try:
        with reader() as con:
            rows = con.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"symbol": r[0], "name": r[1], "sec_type": r[2], "board": r[3],
             "list_date": str(r[4]) if r[4] else None, "is_st": r[5]} for r in rows]


@router.get("/daily")
def daily(
    symbol: str = Query(min_length=6, max_length=12),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=250, le=2500),
) -> list[dict]:
    """个股日线（Parquet 湖）。K 线图与研究取数共用一个入口。"""
    sym = resolve_symbol(symbol)
    df = read_daily([sym], start=start, end=end).collect()
    if not len(df):
        return []
    cols = [c for c in ["trade_date", "open", "high", "low", "close",
                        "volume", "amount"] if c in df.columns]
    df = df.select(cols).sort("trade_date").tail(limit)
    out = df.to_dicts()
    for r in out:
        r["trade_date"] = str(r["trade_date"])
    return out


@router.get("/indicators")
def indicators(
    symbol: str = Query(min_length=6, max_length=16),
    limit: int = Query(default=120, le=500),
) -> list[dict]:
    """个股日线 + 技术指标（MA/MACD/RSI/BOLL，方案 M3）。

    指标有 warmup 期，所以先取 250 根再截尾 —— 返回的每行指标都是全量历史算出来的。
    """
    from lquant.factors.indicators import add_all

    sym = resolve_symbol(symbol)
    df = read_daily([sym]).collect()
    if not len(df):
        return []
    cols = [c for c in ["trade_date", "open", "high", "low", "close",
                        "volume"] if c in df.columns]
    df = df.select(cols).sort("trade_date")
    df = add_all(df)
    out = df.tail(limit).to_dicts()
    for r in out:
        r["trade_date"] = str(r["trade_date"])
        for k, v in r.items():
            if isinstance(v, float):
                r[k] = round(v, 4)
    return out


# 东财 push2 字段：fltt=2/invt=2 直接返回浮点
_QT_URL = ("https://push2.eastmoney.com/api/qt/stock/get?"
           "secid={secid}&fltt=2&invt=2&fields="
           "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f116,f169,f170")


def _secid(symbol: str) -> str:
    """`600519.SH` → `1.600519`（沪），`000001.SZ` → `0.000001`（深）。"""
    code = bare_code(symbol)
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


@router.get("/quote")
def quote(symbol: str = Query(min_length=6, max_length=16)) -> dict:
    """实时行情（东财）。不可用时 available=false，前端兜底日线末价。"""
    sym = resolve_symbol(symbol)
    try:
        from lquant.market.em_client import em_get

        r = em_get(_QT_URL.format(secid=_secid(sym)), qps=3.0)
        data = r.json().get("data") or {}
    except Exception:  # noqa: BLE001 - 代理/断网都走这里
        return {"symbol": sym, "available": False}
    if not data.get("f57"):
        return {"symbol": sym, "available": False}
    return {
        "symbol": sym,
        "name": data.get("f58"),
        "available": True,
        "price": data.get("f43"),
        "change": data.get("f169"),
        "change_pct": data.get("f170"),
        "open": data.get("f46"),
        "high": data.get("f44"),
        "low": data.get("f45"),
        "prev_close": data.get("f60"),
        "volume": data.get("f47"),
        "amount": data.get("f48"),
        "turnover_rate": data.get("f50"),
        "market_cap": data.get("f116"),
    }


# ---------- 数据任务（全量回填 / 每日增量，T4 执行器） ----------

@router.post("/tasks", status_code=202)
def create_data_task(req: TaskIn) -> dict:
    """创建数据任务并入队执行 → 202 + task_id。

    409：已有运行中任务（单任务互斥）；422：参数/前置校验失败（未知类型、
    日期非法、full_backfill 无退市股等）。
    """
    try:
        task = create_task(req.kind, req.params)
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    enqueue("lquant-ingest", execute_task, task["task_id"])
    return {"task_id": task["task_id"]}


@router.get("/tasks")
def list_data_tasks(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """数据任务列表（最新在前）。"""
    return list_tasks(limit)


@router.get("/tasks/{task_id}")
def get_data_task(task_id: str) -> dict:
    """任务详情：状态 / 进度 / 失败明细。WS 断开时前端轮询兜底。"""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return task


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry_data_task(task_id: str) -> dict:
    """retry 补漏：unmark 失败标的 checkpoint 后重新执行 → 202 + task_id。

    端点同步原子认领（claim_retry，消除预检-入队的 TOCTOU）：
    认领成功任务即置 running，再入队执行已认领任务。
    409：pending/running 或竞争抢先；422：终态 ok 不可 retry；404：不存在。
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if task["status"] == "ok":
        raise HTTPException(422, f"任务 {task_id} 状态 ok 不可 retry")
    try:
        claim_retry(task_id)
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    enqueue("lquant-ingest", run_claimed_task, task_id)
    return {"task_id": task_id}


# ---------- 跨源对拍 ----------

@router.post("/crosscheck")
def run_crosscheck_ep(req: CrosscheckIn) -> dict:
    """跨源对拍（同步端点：哨兵抽样小窗口，秒级）。返回 {summary, issues, flagged_rows}。"""
    from lquant.data.ingest.crosscheck import run_crosscheck

    try:
        return run_crosscheck(peers=req.peers, start=req.start,
                              end=req.end, limit=req.limit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/crosscheck/issues")
def crosscheck_issues(
    limit: int = Query(default=200, ge=1, le=1000),
    resolved: bool = False,
) -> list[dict]:
    """质量问题检索（data_quality_issue，默认未解决）。"""
    from lquant.data.quality.issues import latest_issues

    return latest_issues(limit=limit, resolved=resolved)


@router.post("/crosscheck/issues/resolve")
def resolve_crosscheck_issue(req: ResolveIn) -> dict:
    """标记问题已处理；不存在 → 404。"""
    from lquant.data.quality.issues import resolve_issue

    with reader() as con:
        try:
            row = con.execute(
                "SELECT issue_id FROM data_quality_issue WHERE issue_id = ?",
                [req.issue_id]).fetchone()
        except Exception as e:  # noqa: BLE001 - 表不存在等价于没有这条 issue
            from loguru import logger

            logger.warning(f"quality issue 查询失败（按不存在处理）: {e}")
            row = None
    if row is None:
        raise HTTPException(404, f"issue 不存在: {req.issue_id}")
    resolve_issue(req.issue_id)
    return {"issue_id": req.issue_id, "resolved": True}
