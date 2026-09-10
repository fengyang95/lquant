"""滚动窗口 IC 指标。"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.evaluate.rolling import rolling_ic


def _df(days: int = 120, n_stocks: int = 6) -> pl.DataFrame:
    """因子与前瞻收益完全正相关的合成面板，IC 恒为 1。"""
    rows = []
    for d in range(days):
        for i in range(n_stocks):
            rows.append({"trade_date": d, "symbol": f"S{i}",
                         "f": float(i) + d * 0.01, "fwd_ret_1": float(i) + d * 0.01})
    return pl.DataFrame(rows)


def test_rolling_ic_perfect_corr_is_one():
    s = rolling_ic(_df(), "f", "fwd_ret_1", window=20)
    # 完全相关 → 每个窗口 ic_mean / rank_ic_mean 都是 1，ir 无标准差 → NaN
    assert len(s) == 120 - 20 + 1
    assert abs(s["ic_mean"][0] - 1.0) < 1e-9
    assert abs(s["rank_ic_mean"][0] - 1.0) < 1e-9
    assert s["positive_rate"][0] == 1.0
    assert s["n_days"][0] == 20


def test_rolling_ic_window_moves():
    s = rolling_ic(_df(days=30), "f", "fwd_ret_1", window=10)
    assert len(s) == 21
    # 第 i 个完整窗口终点是第 window-1+i 天
    assert s["trade_date"][0] == 9 and s["trade_date"][-1] == 29


@pytest.mark.parametrize("window", [0, -5])
def test_rolling_ic_bad_window_raises(window: int):
    with pytest.raises(ValueError):
        rolling_ic(_df(), "f", "fwd_ret_1", window=window)


def test_rolling_ic_short_series():
    # 数据不足一个窗口 → 空表
    assert rolling_ic(_df(days=5), "f", "fwd_ret_1", window=20).is_empty()


def test_rolling_ic_handles_null_ic_days():
    # 某天只有 1 只股票 → ic_series 因 min_obs 剔除该日；有效日 24 天，
    # 滚动在有效日序列上进行 → 窗口数 = 24 - 10 + 1 = 15
    df = _df(days=25)
    thin = df.filter(~((pl.col("trade_date") == 10) & (pl.col("symbol") != "S0")))
    s = rolling_ic(thin, "f", "fwd_ret_1", window=10)
    assert len(s) == 15
    # 缺日窗口的 n_days 仍可能 < window
    assert s["n_days"].min() <= 10
