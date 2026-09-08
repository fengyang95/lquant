"""数据血缘与版本（细化方案 D12 / §3.6）。

data_version 是缓存失效的锚点：数据湖每次同步登记版本，
因子缓存键、回测快照都引用它 —— 「用了新数据却拿了旧因子值」
这类静默错误的唯一防线。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from lquant.core.db import reader, writer
from lquant.data.store.ddl import DDL_DATA_VERSION as _DDL

__all__ = ["new_version", "register", "latest"]


def new_version(now: datetime | None = None) -> str:
    """版本号 = 日期 + 当日序号（YYYYMMDD.n），同日多次同步不互相覆盖。"""
    now = now or datetime.now()
    day = now.strftime("%Y%m%d")
    with writer() as con:
        _ensure(con)
        n = con.execute(
            "SELECT count(*) FROM data_version WHERE version LIKE ?", [f"{day}.%"]
        ).fetchone()[0]
        return f"{day}.{n + 1}"


def register(version: str, dataset: str, row_count: int,
             trade_date: date | None = None) -> None:
    with writer() as con:
        _ensure(con)
        con.execute(
            "INSERT OR REPLACE INTO data_version VALUES (?, ?, ?, ?, now())",
            [version, dataset, trade_date, row_count],
        )


def latest(dataset: str) -> str | None:
    with reader() as con:
        _ensure(con)
        r = con.execute(
            "SELECT version FROM data_version WHERE dataset = ? "
            "ORDER BY created_at DESC LIMIT 1", [dataset]).fetchone()
    return r[0] if r else None


def _ensure(con) -> None:
    con.execute(_DDL)


def stamp(df: pl.DataFrame, source: str, version: str | None = None) -> pl.DataFrame:
    """给批次补齐血缘三件套：source / ingested_at / data_version。"""
    version = version or new_version()
    return df.with_columns(
        source=pl.lit(source),
        ingested_at=pl.lit(datetime.now(), dtype=pl.Datetime),
        data_version=pl.lit(version),
    )
