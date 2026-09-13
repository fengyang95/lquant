"""tushare daily_basic 回填与日线湖合并。

解决两个硬缺口：
- total_mv / float_mv：baostock 日线根本不出市值，日线湖恒 NULL；
- pe_ttm / pb_mrq / ps_ttm：2026 分区回填早于估值扩列（5bf569b），
  整年为空——扩列修复之外，本模块可对任意区间做 null 填充。

数据流：
  1. backfill：交易日粒度调 pro.daily_basic（一天一请求覆盖全市场，
     1 qps 限流下 ~640 交易日约 11 分钟），落 daily_basic 湖
     （data/parquet/daily_basic/year=YYYY/），checkpoint 按天断点续传；
  2. merge：把 daily_basic 湖 join 回日线湖，**只填 NULL，不覆盖已有值**
     （baostock 已有的 pe_ttm 是主源，tushare 只做补洞；两源混写会导致
     时间序列跳变，crosscheck 原则「比对不取值」同理）。
"""
from __future__ import annotations

from datetime import date

from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.parquet import (
    _daily_basic_path,
    _daily_path,
    read_daily,
    read_daily_basic,
    write_daily_basic,
)

# 日线湖允许由 daily_basic 填充的列（merge 时只填 NULL）
FILL_COLS = ("pe_ttm", "pb_mrq", "ps_ttm", "total_mv", "float_mv")

_CHUNK = 60  # 每写一次湖的最多天数：断点丢失有界，避免逐日重写年文件


def _tushare_provider():
    """从 Fallback 链里取 tushare 源（daily_basic 直连不走能力路由）。"""
    from lquant.data.providers import get_provider

    chain = get_provider()
    for p in chain.providers:
        if p.name == "tushare":
            return p
    raise RuntimeError(
        "tushare 源不可用：检查 TUSHARE_TOKEN（src/lquant/.env 或环境变量）"
    )


def lake_trade_dates(start: date, end: date) -> list[date]:
    """日线湖内 [start, end] 的去重交易日（merge 的 join 目标就是这些行）。"""
    lf = read_daily(start=start, end=end)
    days = (
        lf.select("trade_date").unique().sort("trade_date").collect()
        .get_column("trade_date").to_list()
    )
    return [d for d in days if isinstance(d, date)]


def backfill_daily_basic(
    start: date | str = "2024-01-01",
    end: date | str | None = None,
    merge: bool = True,
) -> dict:
    """回填 daily_basic 湖，可选合并回日线湖。返回统计 dict。"""
    from loguru import logger

    from lquant.core.types import today_cn

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else (date.fromisoformat(end) if end else today_cn())

    days = lake_trade_dates(start_d, end_d)
    if not days:
        raise RuntimeError(f"日线湖内 {start_d}~{end_d} 无数据，先跑日线回填")
    cp = Checkpoint("daily_basic")
    cp.set_meta(start=str(start_d), end=str(end_d))
    todo = [d for d in days if d.isoformat() not in cp.done]
    logger.info(f"daily_basic 回填 {len(todo)}/{len(days)} 个交易日 {start_d}~{end_d}")

    provider = _tushare_provider()
    fetched = 0
    buf: list = []
    for i, d in enumerate(todo, 1):
        df = provider.daily_basic(d)
        if len(df):
            buf.append(df)
            fetched += len(df)
        cp.mark({d.isoformat()})
        if len(buf) >= _CHUNK or i == len(todo):
            if buf:
                write_daily_basic(_concat(buf))
                buf = []
            logger.info(f"  daily_basic 进度 {i}/{len(todo)}（{d}）")

    out = {"days": len(days), "fetched_days": len(todo), "rows": fetched}
    if merge:
        out.update(merge_daily_basic(start_d, end_d))
    return out


def _concat(frames: list) -> object:
    import polars as pl

    from lquant.data.schema import SCHEMAS

    return pl.concat(frames, how="diagonal").select(list(SCHEMAS["daily_basic"]))


def coalesce_daily_basic(daily: object, basic: object) -> tuple[object, dict[str, int]]:
    """把 daily_basic 按 (symbol, trade_date) 合入日线帧，**只填 NULL**。

    纯函数（不触湖），便于单测。返回 (新日线帧, 每列实际填充的单元格数)。
    """
    import polars as pl

    if not basic.height:
        return daily, {c: 0 for c in FILL_COLS}
    keys = ["symbol", "trade_date"]
    for c in FILL_COLS:
        if c not in daily.columns:  # 老年文件缺列：先补空列再 join
            daily = daily.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    daily = daily.with_columns(pl.col(k).cast(pl.Utf8) if k == "symbol" else pl.col(k)
                               for k in keys)
    basic = basic.select(
        *(pl.col(k).cast(daily.schema[k]) for k in keys),
        *(pl.col(c) for c in FILL_COLS),
    ).rename({c: f"__{c}" for c in FILL_COLS})
    joined = daily.join(basic, on=keys, how="left")
    filled: dict[str, int] = {}
    exprs = []
    for c in FILL_COLS:
        n = joined.select(
            (pl.col(c).is_null() & pl.col(f"__{c}").is_not_null()).sum()
        ).item()
        filled[c] = int(n or 0)
        exprs.append(
            pl.when(pl.col(c).is_null())
            .then(pl.col(f"__{c}"))
            .otherwise(pl.col(c))
            .alias(c)
        )
    out = joined.with_columns(exprs).drop([f"__{c}" for c in FILL_COLS])
    return out, filled


def merge_daily_basic(start: date | str, end: date | str) -> dict:
    """daily_basic 湖 → 日线湖合并（按年文件读改写，只填 NULL）。"""
    import polars as pl
    from loguru import logger

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else date.fromisoformat(end)

    basic = read_daily_basic(start_d, end_d)
    if not len(basic):
        logger.warning("daily_basic 湖为空，跳过 merge")
        return {"years": [], "filled": {}}
    total: dict[str, int] = {c: 0 for c in FILL_COLS}
    years: list[int] = []
    for year in sorted(set(basic.get_column("trade_date").dt.year().to_list())):
        dp, _bp = _daily_path(year), _daily_basic_path(year)
        if not dp.exists():
            continue
        daily = pl.read_parquet(dp)
        basic_y = basic.filter(pl.col("trade_date").dt.year() == year)
        merged, filled = coalesce_daily_basic(daily, basic_y)
        if not merged.height:
            continue
        merged = merged.sort(["symbol", "trade_date"])
        from lquant.data.store.parquet import _atomic_write_parquet, _file_lock

        with _file_lock(dp):
            _atomic_write_parquet(merged, dp)
        years.append(year)
        for c, n in filled.items():
            total[c] = total.get(c, 0) + n
        logger.info(f"  merge year={year}: {filled}")
    logger.info(f"merge 完成 {years}，填充 {total}")
    return {"years": years, "filled": total}


__all__ = [
    "FILL_COLS",
    "backfill_daily_basic",
    "merge_daily_basic",
    "coalesce_daily_basic",
    "lake_trade_dates",
]
