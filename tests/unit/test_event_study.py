"""事件式分层收益：事前发散 = 描述既有趋势，事后发散 = 预测性信号。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.factors.evaluate.event_study import event_study, event_study_summary


def _panel(mode: str, n_days: int = 60, n_sym: int = 30) -> pl.DataFrame:
    """iid 日收益的随机游走，factor 的口径由 mode 决定。

    - "momentum"：factor = 过去 5 日收益 —— 因子值直接由事件日**之前**的
      收益构成 → 事前就发散（描述既成趋势）
    - "predictive"：factor = 未来 5 日收益 —— 因子值由事件日**之后**的
      收益构成 → 事后才发散（真正的预测信号）
    """
    rng = np.random.default_rng(7)
    rows = []
    for i in range(n_sym):
        px = 100.0
        for d in range(n_days):
            px *= (1 + rng.normal(0, 0.01))
            rows.append({"trade_date": d, "symbol": f"S{i:03d}", "close": px})
    df = pl.DataFrame(rows).sort(["symbol", "trade_date"])
    if mode == "momentum":
        return df.with_columns(
            (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("factor")
        ).drop_nulls("factor")
    return df.with_columns(
        (pl.col("close").shift(-5).over("symbol") / pl.col("close") - 1).alias("factor")
    ).drop_nulls("factor")


def test_rel_period_axis_and_shape():
    curve = event_study(_panel("momentum"), "factor", "close", n_groups=5,
                        before=3, after=4)
    assert curve["rel_period"].to_list() == [-3, -2, -1, 0, 1, 2, 3, 4]
    assert set(curve.columns) == {"rel_period", "q1", "q2", "q3", "q4", "q5", "spread"}
    # 事件日当天（k=0）所有组的累计收益都是 0（除以自身）
    row0 = curve.filter(pl.col("rel_period") == 0)
    assert abs(float(row0["q1"][0])) < 1e-12


def test_demeaned_curve_is_cross_sectionally_centered():
    """demean 后同一天各组的均值应接近 0（曲线不发散于市场 beta）。"""
    curve = event_study(_panel("predictive"), "factor", "close", n_groups=4,
                        before=2, after=2, demeaned=True)
    raw = event_study(_panel("predictive"), "factor", "close", n_groups=4,
                      before=2, after=2, demeaned=False)
    qcols = ["q1", "q2", "q3", "q4"]
    dm_mean = float(curve.select(pl.mean_horizontal(qcols).alias("m"))["m"].abs().mean())
    raw_mean = float(raw.select(pl.mean_horizontal(qcols).alias("m"))["m"].abs().mean())
    assert dm_mean < raw_mean


def test_look_ahead_ratio_separates_descriptive_from_predictive():
    """动量型因子在事件日之前就分岔（比值 > 1），预测型因子集中在事件日之后（< 1）。"""
    mom = event_study_summary(_panel("momentum"), "factor", "close", n_groups=5,
                              before=10, after=10)
    pre = event_study_summary(_panel("predictive"), "factor", "close", n_groups=5,
                              before=10, after=10)
    assert mom["look_ahead_ratio"] > 1.0
    assert pre["look_ahead_ratio"] < 1.0


def test_left_half_means_return_ending_at_event_date():
    """k<0 段是「截止到事件日」的累计收益：动量因子的赢家组在事件前应是正收益。

    如果口径写成 p(t-10)/p(t)-1（倒着看），动量因子会显示成事件前在反转 —— 读图会反。
    """
    curve = event_study(_panel("momentum"), "factor", "close", n_groups=5,
                        before=5, after=1, demeaned=False)
    pre = curve.filter(pl.col("rel_period") == -5)
    assert float(pre["q5"][0]) > 0      # 高动量组：事件日之前已上涨
    assert float(pre["q1"][0]) < 0      # 低动量组：事件日之前已下跌


def test_spread_column_is_top_minus_bottom():
    curve = event_study(_panel("predictive"), "factor", "close", n_groups=5,
                        before=1, after=3)
    for r in curve.iter_rows(named=True):
        assert r["spread"] == pytest.approx(r["q5"] - r["q1"])


def test_rejects_bad_window():
    with pytest.raises(ValueError):
        event_study(_panel("momentum"), "factor", "close", before=-1)
    with pytest.raises(ValueError):
        event_study(_panel("momentum"), "factor", "close", after=10**4)


def test_missing_column_raises():
    df = _panel("momentum").drop("close")
    with pytest.raises(KeyError):
        event_study(df, "factor", "close")


def test_null_factor_rows_do_not_corrupt_price_path():
    """回归：factor 为空的行被剔除后，shift 收益不得跨过被剔的行。

    3 天价格 100→110→121（日收益 10%），中间日 factor 置 null。
    旧实现先 drop_nulls([price, factor]) 再 shift，`shift(-1)` 会把
    「次 1 个交易日」错算成「次 1 个剩余行」（100→121 = +21%）；
    行完整面板上算则恒为 +10%。
    """
    df = pl.DataFrame({
        "trade_date": [0, 1, 2],
        "symbol": ["S0", "S0", "S0"],
        "close": [100.0, 110.0, 121.0],
        "factor": [1.0, None, 2.0],
    })
    curve = event_study(df, "factor", "close", n_groups=1, before=0, after=2,
                        demeaned=False)
    r1 = curve.filter(pl.col("rel_period") == 1)["q1"].drop_nulls().to_list()
    # 事件日 0 的 r1 = +10%；事件日 1 无次日价格，不入聚合
    assert r1 == pytest.approx([0.10]), f"r1 应为 +10%，实得 {r1}"
