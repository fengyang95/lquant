"""参考数据：交易日历 + 标的基础表 + 上市/退市日期。

这三样是"地基"：没有日历，回测的每根 K 线都错位；
没有 list_date/delist_date，样本里就混进未来股 + 幸存者偏差。

分两级：
- 快路径 sync_securities()：一次请求拿全市场 code/name/status
- 慢路径 sync_security_details()：逐只补 ipoDate/outDate，走 checkpoint 增量
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.db import reader
from lquant.core.types import now_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.catalog import SecurityRepo, TradeCalendarRepo

_CAL_START = date(1990, 12, 19)   # 上交所开市
_CAL_END = date(2035, 12, 31)


def sync_calendar(start: date | str = _CAL_START, end: date | str = _CAL_END) -> int:
    """交易日历。幂等，重跑直接覆盖。

    没有它，后面所有对齐（回测日历、因子窗口、复权基准日）全是错的。
    """
    from loguru import logger

    from lquant.data.providers import get_provider

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else date.fromisoformat(end)

    df = get_provider().trade_calendar(start_d, end_d)
    if not len(df):
        logger.warning("交易日历返回为空")
        return 0
    n = TradeCalendarRepo().upsert(df)
    logger.info(f"交易日历 {n} 天（开市 {int(df['is_open'].sum())} 天）{start_d}~{end_d}")
    return n


def sync_securities(day: date | None = None) -> int:
    """快路径：全市场标的清单。库里已有 list_date 要保住，不能被覆盖成 NULL。"""
    from loguru import logger

    from lquant.data.providers import get_provider

    df = get_provider().securities(day)
    if not len(df):
        logger.warning("标的清单返回为空")
        return 0
    df = _merge_existing_details(df).with_columns(
        source=pl.lit("baostock"),
        updated_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
    )
    n = SecurityRepo().upsert(df)
    logger.info(f"标的清单 {n} 只")
    return n


def sync_security_details(limit: int | None = None, batch: int = 200) -> int:
    """慢路径：逐只补 ipoDate / outDate，断点续传。

    5000 只 × 每只一次请求 ≈ 20-40 分钟，中途被杀是常态，所以每批写 checkpoint。
    """
    from loguru import logger

    from lquant.data.providers import get_provider

    repo = SecurityRepo()
    pending = repo.pending_details()
    if limit:
        pending = pending[:limit]
    if not pending:
        logger.info("没有待补详情的标的")
        return 0

    cp = Checkpoint("security_details")
    todo = cp.remaining(pending)
    logger.info(f"待补详情 {len(todo)} 只（已完成 {len(cp)}）")
    if not todo:
        return 0

    provider = get_provider()
    target = provider.providers[0] if hasattr(provider, "providers") else provider
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        df = target.security_details(chunk)
        if len(df):
            repo.upsert(df.with_columns(
                source=pl.lit("baostock"),
                updated_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
            ).select([c for c in ("symbol", "name", "list_date", "delist_date",
                                  "sec_type", "source", "updated_at") if c in df.columns]))
        cp.mark(chunk)
        done += len(chunk)
        logger.info(f"  详情进度 {done}/{len(todo)}")
    return done


def sync_reference(skip_details: bool = False, detail_limit: int | None = None) -> dict:
    """一键补齐地基。返回各步写入行数，供 CLI/脚本打印。"""
    out = {"calendar": sync_calendar(), "securities": sync_securities()}
    if not skip_details:
        out["details"] = sync_security_details(limit=detail_limit)
    return out


def _merge_existing_details(df: pl.DataFrame) -> pl.DataFrame:
    """保留库里已有的 list_date / delist_date，避免快路径把它们冲成 NULL。

    注意：INSERT OR REPLACE 是整行覆盖，不合并就会丢失慢路径的成果。
    """
    try:
        with reader() as con:
            old = con.execute("SELECT symbol, list_date, delist_date FROM security").pl()
    except Exception:  # noqa: BLE001 - 表不存在等情况，直接跳过
        return df
    if not len(old):
        return df
    joined = df.join(old, on="symbol", how="left")
    return joined.with_columns(
        list_date=pl.coalesce(
            pl.col("list_date").cast(pl.Date, strict=False),
            pl.col("list_date_right").cast(pl.Date, strict=False),
        ),
        delist_date=pl.coalesce(
            pl.col("delist_date").cast(pl.Date, strict=False),
            pl.col("delist_date_right").cast(pl.Date, strict=False),
        ),
    ).drop("list_date_right", "delist_date_right")


__all__ = ["sync_calendar", "sync_securities", "sync_security_details", "sync_reference"]
