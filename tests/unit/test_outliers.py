"""截面异常收益过滤（移植自 alphalens / ferric-alpha 的 filter_zscore）。"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.evaluate.outliers import filter_zscore, zscore_filter_stats


def _df() -> pl.DataFrame:
    """3 天 × 20 只：除 S0 外都是 ~0/1% 的正常收益，S0 被注入极端值。

    样本要够多：只有 5 只票时，那个极端值自己就把截面 std 抬起来，
    z 反而压不回阈值以上 —— 这本身就说明为什么默认阈值是 20 而不是 3。
    """
    rows = []
    for d in range(1, 4):
        for i in range(20):
            v = 1.0 if i == 0 else 0.001 * i
            rows.append({"trade_date": d, "symbol": f"S{i:02d}", "fwd_ret_1": v})
    return pl.DataFrame(rows)


def test_drops_extreme_rows():
    kept = filter_zscore(_df(), threshold=3.0)
    assert "S00" not in kept["symbol"].to_list()
    assert len(kept) == 57          # 每天删 1 行


def test_zero_variance_cross_section_kept():
    """全样本同值（停牌/一字板场景）不能让 |z| 变成 inf 而删光。"""
    df = pl.DataFrame({"trade_date": [1, 1, 1, 2, 2, 2],
                       "symbol": list("abcdef"),
                       "fwd_ret_1": [0.0] * 6})
    assert len(filter_zscore(df, threshold=3.0)) == 6


def test_high_threshold_is_noop():
    df = _df()
    kept = filter_zscore(df, threshold=20.0)
    assert len(kept) == len(df)


def test_auto_detects_fwd_cols_and_stats():
    st = zscore_filter_stats(_df(), threshold=3.0)
    assert st["cols"] == ["fwd_ret_1"]
    assert st["n_in"] == 60 and st["n_out"] == 57
    assert st["n_dropped"] == 3
    assert st["dropped_rate"] == pytest.approx(0.05)


def test_custom_cols_and_missing_col_error():
    df = _df().rename({"fwd_ret_1": "other"})
    assert len(filter_zscore(df, ["other"], threshold=3.0)) == 57
    with pytest.raises(KeyError):
        filter_zscore(df, ["nope"], threshold=3.0)


def test_invalid_threshold_rejected():
    with pytest.raises(ValueError):
        filter_zscore(_df(), threshold=0.0)


def test_multiple_cols_drop_if_any_extreme():
    """任一列极端即删该行；只极端在另一列的行也不会被放过。"""
    df = _df().with_columns(pl.lit(0.002).alias("fwd_ret_5"))
    df = df.with_columns(
        pl.when(pl.col("symbol") == "S01").then(pl.lit(9.0))
        .otherwise(pl.col("fwd_ret_5")).alias("fwd_ret_5"))
    kept = filter_zscore(df, ["fwd_ret_1", "fwd_ret_5"], threshold=3.0)
    syms = set(kept["symbol"].to_list())
    assert "S00" not in syms and "S01" not in syms
