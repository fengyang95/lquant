"""tushare daily_basic 回填（市值/估值/股本）+ coalesce 合并回日线湖。

一天一请求覆盖全市场，checkpoint 按交易日断点续传；merge 只填 NULL 不覆盖
主源 —— baostock 日线是主源，tushare 只补估值空洞。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.types import today_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.schema import SCHEMAS
from lquant.data.store.parquet import read_daily, write_daily, write_daily_basic

_COALESCE_COLS: tuple[str, ...] = ("pe_ttm", "pb_mrq", "ps_ttm", "total_mv", "float_mv")

__all__ = ["coalesce_daily_basic", "backfill_daily_basic"]


def _provider():
    """tushare provider（模块函数，测试打桩点）。"""
    from lquant.data.providers.tushare import TushareProvider

    return TushareProvider()


def _open_days(start: date, end: date) -> list[date]:
    """区间内开市日（trade_calendar）。"""
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT trade_date FROM trade_calendar WHERE is_open "
            "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [start, end]).fetchall()
    return [r[0] for r in rows]


def coalesce_daily_basic(
    daily_df: pl.DataFrame,
    basic_df: pl.DataFrame,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """用 daily_basic 只填 daily_df 为 NULL 的估值列，不覆盖已有值。

    Returns:
        (合并帧, 填充计数 dict —— 键固定 _COALESCE_COLS，无值/无列时为 0)
    """
    filled = dict.fromkeys(_COALESCE_COLS, 0)
    if daily_df.height == 0 or basic_df.height == 0:
        return daily_df, filled

    # 老年文件缺列 → 先补全 NULL 列再 join，不炸
    daily_df = daily_df.with_columns(
        pl.lit(None, dtype=SCHEMAS["daily_bar"][c]).alias(c)
        for c in _COALESCE_COLS if c not in daily_df.columns
    )
    cols = [c for c in _COALESCE_COLS if c in basic_df.columns]
    b = basic_df.select(["symbol", "trade_date", *cols]).with_columns(
        pl.col("trade_date").cast(daily_df.schema["trade_date"])
    )
    b = b.rename({c: f"__{c}" for c in cols})
    out = daily_df.join(b, on=["symbol", "trade_date"], how="left")
    for c in cols:
        hk = f"__{c}"
        # 计数要在 coalesce 之前（基于旧列值），否则 was_null 失真
        n = out.select(
            (pl.col(c).is_null() & pl.col(hk).is_not_null()).sum()
        ).item()
        out = out.with_columns(pl.coalesce(c, hk).alias(c))
        filled[c] = int(n or 0)
        out = out.drop(hk)
    return out, filled


def backfill_daily_basic(
    start: date | str,
    end: date | str | None = None,
    merge: bool = True,
) -> dict:
    """按交易日回填 tushare daily_basic 并落湖；merge=True 时合并回日线湖。

    一天一请求全市场；checkpoint 按天断点续传（重跑跳过已完成日）。
    Returns:
        {"days": 本次拉取交易日数, "rows": 落湖行数,
         "merged": merge 时的填充计数 dict（未拉到新数据/merge=False → None）}
    """
    from loguru import logger

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else (
        date.fromisoformat(end) if end else today_cn())

    cp = Checkpoint("daily_basic")
    todo = [d for d in _open_days(start_d, end_d)
            if d.isoformat() not in cp.done]
    logger.info(f"daily_basic 回填 {start_d}~{end_d}: "
                f"{len(todo)} 个开市日待拉（已完成 {len(cp.done)}）")

    frames: list[pl.DataFrame] = []
    for d in todo:
        try:
            df = _provider().daily_basic(trade_date=d)
        except Exception as e:  # noqa: BLE001 - 单日失败不断整体，留待重跑
            logger.warning(f"daily_basic {d} 失败，跳过: {e}")
            continue
        if not df.is_empty():
            write_daily_basic(df)
            frames.append(df)
        cp.mark([d.isoformat()])

    merged = None
    if merge and frames:
        basic_all = pl.concat(frames)
        daily = read_daily().collect()
        merged_df, filled = coalesce_daily_basic(daily, basic_all)
        write_daily(merged_df)  # overlay 语义：同 key 整行替换 → 填充后的行落湖
        merged = filled
    return {"days": len(todo), "rows": sum(len(f) for f in frames),
            "merged": merged}
