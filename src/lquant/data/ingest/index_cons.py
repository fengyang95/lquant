"""指数成分快照同步：tushare index_weight → index_cons 表。

一个 eff_date = 一次完整成分快照。研究态取「最新快照」做股票池过滤；
历史回测要防前视的话走 as_of（IndexConsRepo 已实现）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import polars as pl

from lquant.core.types import today_cn

# 常用宽基指数：沪深300 / 中证500 / 中证800 / 中证1000
DEFAULT_INDEXES: list[str] = ["000300.SH", "000905.SH", "000906.SH", "000852.SH"]


def _latest_snapshot(api, index_code: str, lookback_days: int = 45) -> pl.DataFrame:
    """拉该指数近期成分，截取最新 trade_date 的整批快照。

    end 用 today_cn()：服务器/容器时区非 Asia/Shanghai 时 date.today() 会
    与业务日错位一天，查询窗口整体偏移（core/types 的业务日期约定）。
    """
    end = today_cn()
    start = end - timedelta(days=lookback_days)
    df = api(
        "index_weight",
        index_code=index_code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if df.is_empty():
        return pl.DataFrame()
    df = df.with_columns(pl.col("trade_date").cast(pl.Utf8))
    latest = df["trade_date"].max()
    snap = df.filter(pl.col("trade_date") == latest)
    return snap.select(
        pl.lit(index_code).alias("index_code"),
        pl.col("con_code").alias("symbol"),
        pl.col("weight").cast(pl.Float64, strict=False),
        pl.col("trade_date").str.to_date("%Y%m%d").alias("eff_date"),
    )


def sync_index_cons(index_codes: list[str] | None = None) -> dict:
    """同步各指数最新成分快照。返回 {synced, rows, empty}。"""
    from lquant.data.providers.tushare import TushareProvider

    codes = index_codes or DEFAULT_INDEXES
    provider = TushareProvider()
    frames: list[pl.DataFrame] = []
    empty: list[str] = []
    for code in codes:
        try:
            snap = _latest_snapshot(provider._call, code)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            empty.append(code)
            logging.getLogger(__name__).warning("index_cons %s 拉取失败: %s", code, e)
            continue
        if snap.is_empty():
            empty.append(code)
            continue
        frames.append(snap)
    rows = 0
    if frames:
        from lquant.core.db import writer
        from lquant.data.store.catalog import IndexConsRepo
        from lquant.data.store.ddl import DDL_STATEMENTS

        # 自建表：全新库（未跑 init_db）也能直接同步
        stmt = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS index_cons" in s)
        with writer() as con:
            con.execute(stmt)
        all_df = pl.concat(frames, how="vertical")
        rows = IndexConsRepo().upsert(all_df)
    return {"synced": len(codes) - len(empty), "rows": rows, "empty": empty}
