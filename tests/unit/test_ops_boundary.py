"""TS/CS 算子边界行为：短窗口 → null，不产半窗值。"""
from __future__ import annotations

import polars as pl

from lquant.factors.engine import FactorEngine


def _df(symbols=("A", "B"), n=30) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [s for s in symbols for _ in range(n)],
        "trade_date": list(range(1, n + 1)) * len(symbols),
        "close": [10.0 + (i % 7) for i in range(n)] * len(symbols),
    })


def test_short_window_is_null_not_partial():
    """窗口不足 n 的头部必须是 null —— 半窗值会让 IC 虚高。"""
    out = FactorEngine(_df().lazy()).compute("Ts_Mean($close,5)", name="f")
    head = out.filter(pl.col("symbol") == "A").sort("trade_date")["f"].head(4)
    assert head.null_count() == 4, "前 4 行（窗口不足）必须是 null"


def test_n1_window_is_identity():
    out = FactorEngine(_df().lazy()).compute("Ts_Mean($close,1)", name="f")
    src = out.filter(pl.col("symbol") == "A").sort("trade_date")["close"]
    got = out.filter(pl.col("symbol") == "A").sort("trade_date")["f"]
    assert (got - src).abs().max() < 1e-12


def test_all_nan_input():
    df = _df().with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    out = FactorEngine(df.lazy()).compute("Ts_Mean($close,3)", name="f")
    assert out["f"].null_count() == len(out)


def test_single_symbol():
    out = FactorEngine(_df(symbols=("A",)).lazy()).compute("Ts_Std($close,5)", name="f")
    assert len(out) == 30
    assert out["f"].null_count() == 4      # 前 4 行窗口不足


def test_cs_rank_no_lookahead_across_dates():
    """Rank 按日独立：改动未来日期的值不影响过去日期的 Rank。"""
    df = _df(n=10)
    a = FactorEngine(df.lazy()).compute("Rank($close)", name="f")
    df2 = df.with_columns(pl.when(pl.col("trade_date") > 5).then(pl.col("close") * 10)
                          .otherwise(pl.col("close")).alias("close"))
    b = FactorEngine(df2.lazy()).compute("Rank($close)", name="f")
    ha = a.filter(pl.col("trade_date") <= 5).sort(["symbol", "trade_date"])["f"]
    hb = b.filter(pl.col("trade_date") <= 5).sort(["symbol", "trade_date"])["f"]
    assert (ha - hb).abs().max() < 1e-12
