"""复权对账：复权收益率与未复权收益率×复权因子必须自洽。"""
from __future__ import annotations

import polars as pl

from lquant.data.quality.adjustment import check_adjustment
from lquant.data.quality.flags import ADJ_ANOMALY


def _df(closes, adj=None):
    n = len(closes)
    df = pl.DataFrame({
        "symbol": ["A"] * n, "trade_date": list(range(1, n + 1)),
        "close": closes, "volume": [100.0] * n,
    })
    if adj is not None:
        df = df.with_columns(pl.Series("adj_factor", adj))
    return df


def test_consistent_adj_factor_no_flag():
    # adj_factor 恒定 → 复权收益 == 原始收益，不应打标
    df = _df([10.0, 10.5, 11.0], adj=[1.0, 1.0, 1.0])
    out, issues = check_adjustment(df)
    assert (out["quality_flags"] & ADJ_ANOMALY == 0).all()
    assert issues == []


def test_adj_factor_jump_consistent_with_price_gap():
    # 除权日：原始价从 11 跳到 9.9（-10%），adj_factor 同步从 1.0 → 1.111...
    # 复权收益 = 9.9*1.111... / (11*1.0) - 1 = 0% → 不打标
    df = _df([10.0, 11.0, 9.9, 10.0], adj=[1.0, 1.0, 10 / 9, 10 / 9])
    out, issues = check_adjustment(df)
    assert (out["quality_flags"] & ADJ_ANOMALY == 0).all()
    assert issues == []


def test_inconsistent_adj_flagged():
    # 除权但 adj_factor 没动 → 复权收益与原始收益同步暴跌且无除权事实 → ADJ_ANOMALY
    # （-10% 未超默认 0.21 带宽，显式收紧 ret_limit 检出）
    df = _df([10.0, 11.0, 9.9, 10.0], adj=[1.0, 1.0, 1.0, 1.0])
    out, issues = check_adjustment(df, ret_limit=0.05)
    assert (out["quality_flags"] & ADJ_ANOMALY != 0).any()
    assert len(issues) == 1
    assert issues[0].rule == "ADJ_ANOMALY"
