"""诊断：全市场回填首批标的的量纲比例（unit_mismatch 根因）。"""
from datetime import date

import polars as pl


def main() -> None:
    from lquant.data.providers.baostock import BaoStockProvider
    from lquant.data.store.catalog import SecurityRepo

    pl.Config.set_tbl_rows(25)
    pl.Config.set_tbl_width_chars(220)

    syms = SecurityRepo().active_symbols()[:16]
    print("前16只:", syms)
    p = BaoStockProvider()
    df = p.daily_bars(syms, date(2026, 1, 1), date(2026, 9, 12))
    print("rows:", len(df))
    if len(df):
        g = df.with_columns(
            ratio=pl.when((pl.col("volume") > 0) & (pl.col("close") > 0) & (pl.col("amount") > 0))
            .then(pl.col("amount") / (pl.col("close") * pl.col("volume")))
            .otherwise(None)
        )
        s = g.group_by("symbol").agg(
            pl.len().alias("n"),
            pl.col("ratio").median().alias("ratio_med"),
            pl.col("volume").median().alias("vol_med"),
            pl.col("amount").median().alias("amt_med"),
            (pl.col("is_suspended") == True).sum().alias("susp"),  # noqa: E712
        ).sort("symbol")
        print(s)
        # 看一只指数的原始行
        idx = [s for s in syms if s.endswith(".SH")][:1]
        if idx:
            print(df.filter(pl.col("symbol") == idx[0]).head(3))


if __name__ == "__main__":
    main()
