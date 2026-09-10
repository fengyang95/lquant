"""采集调度。

时点设计取决于数据源的**可回溯性**：
- 15:05 close   涨停池 / 跌停池 / 炸板池 / 资金流 / 板块 —— 盘中数据收盘后很快被覆盖
- 18:00 evening 龙虎榜 / 北向资金 —— 交易所盘后才发布
- 09:00 preopen 当日基础信息校验、昨日数据补齐

注意涨停池这类不可回溯的数据，采集失败等同于数据永久丢失，
所以 critical=True 的采集器失败时会抛异常（触发告警），而不是记 warning 了事。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from lquant.data.store.catalog import upsert
from lquant.market.collectors import COLLECTORS
from lquant.market.schema import TABLE_COLUMNS, ensure_market_tables

__all__ = ["SCHEDULES", "collect", "persist", "collect_and_save",
           "due_schedules", "status"]

SCHEDULES: dict[str, dict] = {
    "preopen": {"time": "09:00", "desc": "盘前：校验与补采"},
    "intraday": {"time": "14:30", "desc": "盘中：快照（可选）"},
    "close": {"time": "15:05", "desc": "收盘：涨停池/资金流/板块/情绪"},
    "evening": {"time": "18:00", "desc": "盘后：龙虎榜/北向"},
}


def due_schedules(now: datetime | None = None) -> list[str]:
    """当前时刻应该跑哪些时点（用于 cron 之外的常驻调度）。"""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    return [k for k, v in SCHEDULES.items() if v["time"] <= hhmm <= "23:59"]


def collect(schedule: str | None = None, trade_date=None, *,
            demo: bool = False, only: list[str] | None = None) -> dict[str, pl.DataFrame]:
    """执行采集，返回 {采集器名: DataFrame}。单个失败不影响其他。"""
    out: dict[str, pl.DataFrame] = {}
    errors: dict[str, str] = {}
    for k in (only or COLLECTORS.keys()):
        meta = COLLECTORS.meta(k)
        if schedule and meta.get("schedule") != schedule:
            continue
        fn = COLLECTORS.get(k)
        try:
            if meta["name"] in ("money_flow", "sector"):
                df = fn(trade_date=trade_date, demo=demo)
            else:
                df = fn(trade_date=trade_date, demo=demo)
        except TypeError:
            try:
                df = fn(demo=demo)
            except Exception as e:  # noqa: BLE001
                errors[k] = str(e)
                continue
        except Exception as e:  # noqa: BLE001
            errors[k] = str(e)
            if meta.get("critical"):
                raise RuntimeError(f"关键采集器 {k} 失败（数据不可回溯）: {e}") from e
            continue
        out[k] = df
    if errors:
        print(f"[warn] 采集失败: {errors}")
    return out


def persist(frames: dict[str, pl.DataFrame]) -> dict[str, int]:
    """写入 DuckDB。按目标表列对齐，缺列补 NULL。"""
    if not frames:
        return {}
    from lquant.core.db import writer
    with writer() as con:
        ensure_market_tables(con)

    meta = {k: COLLECTORS.meta(k) for k in frames}
    counts: dict[str, int] = {}
    for name, df in frames.items():
        table = meta.get(name, {}).get("table")
        if not table or df is None or not len(df):
            counts[name] = 0
            continue
        cols = [c for c in TABLE_COLUMNS.get(table, []) if c in df.columns]
        if not cols:
            counts[name] = 0
            continue
        try:
            counts[name] = upsert(table, df.select(cols))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 写入 {table} 失败: {e}")
            counts[name] = 0
    return counts


def collect_and_save(schedule: str | None = "close", trade_date=None, *,
                     demo: bool = False) -> dict:
    """采集 + 落库 + 健康度记录一步到位。CLI 与定时任务都调这个。"""
    started = datetime.now()
    d = trade_date or started.date()
    frames: dict[str, pl.DataFrame] = {}
    errors: dict[str, str] = {}
    for k in COLLECTORS:
        meta = COLLECTORS.meta(k)
        if schedule and meta.get("schedule") != schedule:
            continue
        t0 = datetime.now()
        try:
            frames[k] = COLLECTORS.get(k)(trade_date=trade_date, demo=demo)
            errs = ""
        except Exception as e:  # noqa: BLE001
            frames[k] = pl.DataFrame()
            errs = f"{type(e).__name__}: {e}"
            if meta.get("critical"):
                _log_one(k, d, t0, datetime.now(), 0, "failed", errs)
                raise RuntimeError(f"关键采集器 {k} 失败（数据不可回溯）: {e}") from e
            errors[k] = errs
            _log_one(k, d, t0, datetime.now(), 0, "failed", errs)
            continue
        _log_one(k, d, t0, datetime.now(), len(frames[k]),
                 "ok" if len(frames[k]) else "empty")

    counts = persist(frames)
    return {"collected": {k: len(v) for k, v in frames.items()},
            "persisted": counts, "errors": errors}


def _log_one(job: str, trade_date, started_at: datetime, finished_at: datetime,
             rows: int, status: str, message: str = "") -> None:
    """单采集器执行落 collect_log；记录失败不影响采集主流程。"""
    try:
        from lquant.market.collect_log import record

        record(job, trade_date, started_at, finished_at, rows, status, message)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] collect_log 记录失败 {job}: {e}")


def status() -> pl.DataFrame:
    """各看板表当前的数据覆盖情况。"""
    from lquant.core.db import reader
    rows = []
    with reader() as con:
        for table in TABLE_COLUMNS:
            try:
                r = con.execute(
                    f'SELECT COUNT(*) AS n, MIN(trade_date) AS d0, MAX(trade_date) AS d1 '
                    f'FROM "{table}"').fetchone()
                rows.append({"table": table, "rows": r[0] or 0,
                             "first_day": r[1], "last_day": r[2]})
            except Exception:  # noqa: BLE001
                rows.append({"table": table, "rows": 0, "first_day": None, "last_day": None})
    return pl.DataFrame(rows)
