"""tushare daily_basic 回填：市值/估值/股本，一天一请求覆盖全市场。

补日线湖 total_mv/float_mv/pe_ttm 等空缺列（baostock 不出总市值，
daily 湖这些列常年 NULL）。两条入库路径：
- daily_basic 湖（write_daily_basic）保留 tushare 原始口径，随时可重算；
- merge 路径把市值/估值 coalesce 回日线湖：只填 NULL，不覆盖主源值。

checkpoint 按天断点续传；单日失败不标记完成，重跑自动补。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.types import today_cn
from lquant.data.ingest.checkpoint import Checkpoint

# 合并回日线湖的列：只填 NULL，不覆盖主源（baostock pe/pb 等）。
COALESCE_COLS = ("pe_ttm", "pb_mrq", "ps_ttm", "total_mv", "float_mv")

_DAILY_BASIC_LAKE = "daily_basic_lake"


def coalesce_daily_basic(
    daily: pl.DataFrame, basic: pl.DataFrame,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """把 basic 的估值/市值列 coalesce 进 daily：daily 值为 NULL 才填。

    返回 (合并后帧, 各列实际填充行数)。纯函数，不改入参帧。
    basic 辅助列以 `__` 前缀临时加入，返回前全部剔除。
    """
    filled = {c: 0 for c in COALESCE_COLS}
    if basic.height == 0 or daily.height == 0:
        return daily, filled

    daily = daily.clone()
    schema_daily = _schema()
    for c in COALESCE_COLS:
        if c not in daily.columns:
            daily = daily.with_columns(
                pl.lit(None, dtype=schema_daily[c]).alias(c)
            )
    j = basic.select(["symbol", "trade_date", *COALESCE_COLS]).with_columns(
        pl.col(c).alias(f"__{c}") for c in COALESCE_COLS
    )
    out = daily.join(j, on=["symbol", "trade_date"], how="left")
    filled = dict(zip(COALESCE_COLS, out.select(
        # 更新前先数：daily NULL 且 basic 非空的行数（更新后 col(c) 不再 NULL）
        (pl.col(c).is_null() & pl.col(f"__{c}").is_not_null()).sum().alias(c)
        for c in COALESCE_COLS
    ).row(0), strict=True))
    for c in COALESCE_COLS:
        hit = pl.col(c).is_null() & pl.col(f"__{c}").is_not_null()
        out = out.with_columns(
            pl.when(hit).then(pl.col(f"__{c}")).otherwise(pl.col(c)).alias(c)
        )
    return out.drop([c for c in out.columns if c.startswith("__")]), filled


def _schema() -> dict[str, pl.DataType]:
    """惰性取 daily_bar schema（模块导入期避免循环依赖）。"""
    from lquant.data.schema import SCHEMAS

    return SCHEMAS["daily_bar"]


def backfill_daily_basic(
    start: str = "2024-01-01",
    end: str | None = None,
    merge: bool = True,
) -> dict:
    """按交易日回填 tushare daily_basic 并（可选）合并回日线湖。

    返回统计 dict（CLI 逐行 echo）。
    """
    from loguru import logger

    from lquant.data.providers import get_provider
    from lquant.data.store.catalog import TradeCalendarRepo
    from lquant.data.store.parquet import write_daily_basic

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end) if end else today_cn()
    dates = TradeCalendarRepo().range(start_d, end_d)
    if not dates:
        raise RuntimeError(
            "trade_calendar 为空或区间无交易日 —— 先跑 `lq data reference` 建日历"
        )

    cp = Checkpoint(_DAILY_BASIC_LAKE)
    todo = [d for d in dates if not cp.is_done(d.isoformat())]
    logger.info(f"daily_basic 回填 {len(todo)}/{len(dates)} 个交易日 {start_d}~{end_d}")

    ts = [p for p in get_provider().providers if p.name == "tushare"]
    if not ts:
        raise RuntimeError("tushare provider 不可用（缺 token 或未启用）")
    provider = ts[0]

    rows = 0
    filled_total: dict[str, int] = {}
    for d in todo:
        df = provider.daily_basic(d)
        if len(df):
            write_daily_basic(df)
            rows += len(df)
        cp.mark([d.isoformat()])  # 空结果也标记：非交易日/无数据日不必重拉
        if merge and len(df):
            filled = _merge_into_daily(df, d)
            for k, v in filled.items():
                filled_total[k] = filled_total.get(k, 0) + v

    out = {
        "days": len(todo),
        "rows": rows,
        "merge_filled": filled_total if merge else {},
    }
    logger.info(f"daily_basic 回填完成: {out}")
    return out


def _merge_into_daily(df: pl.DataFrame, d: date) -> dict[str, int]:
    """把单日 basic 合并进日线湖当日数据，返回各列填充行数。"""
    from lquant.data.store.parquet import read_daily, write_daily

    daily = read_daily(start=d, end=d).collect()
    if not len(daily):
        return {c: 0 for c in COALESCE_COLS}
    merged, filled = coalesce_daily_basic(daily, df)
    changed = any(v > 0 for v in filled.values())
    if changed:
        write_daily(merged)
    return filled


__all__ = ["COALESCE_COLS", "backfill_daily_basic", "coalesce_daily_basic"]
