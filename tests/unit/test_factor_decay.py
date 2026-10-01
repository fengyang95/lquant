"""IC 衰减：口径必须是「行完整帧上算好的前瞻收益」，不能被行过滤改写。

回归背景：API 评价 / HTML 报告会在 filter_zscore、drop_nonfinite 剔掉若干行
**之后**再调 decay_profile。若它无条件重算 forward_return，
``shift(-h).over(symbol)`` 会跨过被剔掉的行 —— 「h 个交易日后的收益」
被静默算成跨越更长区间的收益（删一行后前一日收益 0.0714 → 0.1429），
半衰期与调仓建议随之失真。
"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.evaluate import forward_return
from lquant.factors.evaluate.decay import (
    decay_profile,
    decay_summary,
    half_life,
    suggest_rebalance,
)
from lquant.factors.evaluate.ic import _summarize, ic_series


def _panel(n_days: int = 9, n_syms: int = 6) -> pl.DataFrame:
    dates = [d for d in pl.date_range(pl.date(2026, 1, 5), pl.date(2026, 3, 1), "1d",
                                      eager=True)
             if d.weekday() < 5][:n_days]
    rows = []
    for si, s in enumerate(f"s{i}" for i in range(n_syms)):
        px = 10.0 + si
        for i, d in enumerate(dates):
            px *= 1.0 + 0.05 * (((i * 7 + si * 3) % 11) - 5) / 5
            rows.append({"symbol": s, "trade_date": d, "close": px})
    return pl.DataFrame(rows)


def test_decay_profile_reuses_existing_forward_returns():
    """已有 fwd_ret_1 列时直接复用，不在行过滤后的帧上重算。"""
    full = forward_return(_panel(), "close", periods=[1])
    dts = full["trade_date"].unique().sort().to_list()
    hole = full.filter(pl.col("trade_date") != dts[4])      # 中间被剔一行

    correct = float(_summarize(ic_series(hole, "close", "fwd_ret_1")["ic"])["mean"])
    # 旧行为：在空洞帧上重算 → 被剔掉那天的「次日收益」变成跨日收益
    misaligned = float(_summarize(
        ic_series(forward_return(hole, "close", periods=[1]), "close",
                  "fwd_ret_1")["ic"])["mean"])
    assert correct != pytest.approx(misaligned)             # 前提：两条口径确实不同

    prof = decay_profile(hole, "close", [1])
    assert float(prof.filter(pl.col("horizon") == 1)["ic"][0]) == pytest.approx(correct)


def test_decay_profile_fills_missing_horizons_only():
    """只给部分 fwd_ret 列时，缺失的持有期补算出来、已有的原样复用。"""
    full = forward_return(_panel(), "close", periods=[1])
    prof = decay_profile(full, "close", [1, 3]).sort("horizon")
    assert prof["horizon"].to_list() == [1, 3]
    assert prof["ic"][0] == pytest.approx(
        float(_summarize(ic_series(full, "close", "fwd_ret_1")["ic"])["mean"]))
    assert prof["n_days"][1] > 0        # h=3 是补算出来的，不是空行


def test_decay_profile_without_precomputed_returns():
    """调用方没给 fwd_ret_* 时仍要自己算全（CLI audit / 通用 evaluate 路径）。"""
    prof = decay_profile(_panel(), "close", [1, 3])
    assert prof["horizon"].to_list() == [1, 3]
    assert prof["n_days"].to_list() == [8, 6]


def test_half_life_and_suggest_rebalance():
    prof = pl.DataFrame({"horizon": [1, 5, 20], "ic": [0.02, 0.01, 0.004]})
    assert half_life(prof) == pytest.approx(5.0)     # 峰值 0.02 → 半值 0.01 落在 h=5
    assert suggest_rebalance(5.0) == "weekly"
    assert suggest_rebalance(20.0) == "monthly"
    assert suggest_rebalance(float("nan")) == "unknown"
    # 全程不衰减一半 → 取最长持有期
    flat = pl.DataFrame({"horizon": [1, 5, 20], "ic": [0.02, 0.02, 0.02]})
    assert half_life(flat) == pytest.approx(20.0)


def test_decay_summary_shape():
    s = decay_summary(_panel(), "close", [1, 5])
    assert s["factor"] == "close"
    assert isinstance(s["half_life"], float)
    assert s["suggested_rebalance"] in ("daily", "weekly", "biweekly", "monthly",
                                        "quarterly", "unknown")
    assert set(s["profile"]["horizon"].to_list()) == {1, 5}
