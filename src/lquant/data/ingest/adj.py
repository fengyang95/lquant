"""复权因子刷新。

复权因子的特性：只在除权除息日变化，历史段几乎永远不变 ——
所以日常只需要刷「最近窗口」，全量刷新仅在首次回填/换源时做。

写入方式：把湖里对应 (symbol, trade_date) 的行取出来，替换 adj_factor
列后整体 write_daily（key 覆盖合并）。因子表里多出来的行（除权日当根
bar 不在湖里）跳过 —— 湖是唯一真源。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.core.types import today_cn
from lquant.data.store.parquet import read_daily, write_daily


def refresh_adj_factors(*, symbols: list[str] | None = None,
                        days: int = 60, provider=None,
                        start: str | None = None, end: str | None = None) -> int:
    """增量刷新复权因子，返回更新的湖行数。

    days 窗口从今天往回算；传 start/end 覆盖（全量刷新时 start="2016-01-01"）。
    provider 可注入（测试用），缺省取注册链的第一个数据源。
    """
    from datetime import datetime

    end_d = date.fromisoformat(end) if end else today_cn()
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=days)

    lake = read_daily(symbols=symbols, start=start_d, end=end_d).select(
        ["symbol", "trade_date"]).collect()
    if not len(lake):
        return 0
    syms = sorted(lake["symbol"].unique().to_list())

    if provider is None:
        from lquant.data.providers import get_provider

        p = get_provider()
        provider = p.providers[0] if hasattr(p, "providers") else p

    fac = provider.adj_factors(syms, start_d, end_d)
    if not len(fac):
        return 0

    merged = (lake.join(fac.select(["symbol", "trade_date", "factor"]),
                        on=["symbol", "trade_date"], how="inner"))
    if not len(merged):
        return 0

    # 取湖里这些 key 的整行 → 替换 adj_factor → 覆盖写回
    rows = (read_daily(symbols=syms, start=start_d, end=end_d).collect()
            .join(merged.select(["symbol", "trade_date"]),
                  on=["symbol", "trade_date"], how="semi")
            .drop("adj_factor")
            .join(fac.select(["symbol", "trade_date", "factor"]),
                  on=["symbol", "trade_date"], how="left")
            .with_columns(pl.col("factor").fill_null(1.0).alias("adj_factor"))
            .drop("factor"))

    if not len(rows):
        return 0
    rows = rows.with_columns(
        ingested_at=pl.lit(datetime.now().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit(datetime.now().strftime("%Y%m%d")),
    )
    write_daily(rows)
    return len(rows)
