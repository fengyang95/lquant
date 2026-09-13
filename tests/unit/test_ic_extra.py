"""IC 汇总补充指标：|IC|>0.02 阈值胜率、峰度、IC 自相关。"""
from __future__ import annotations

import math
import random

import polars as pl
import pytest

from lquant.factors.evaluate.ic import ic_autocorr, ic_summary


def _perfect_df() -> pl.DataFrame:
    """因子与前瞻收益完全正相关 → 每日 IC = 1，无波动。"""
    rows = []
    for d in range(30):
        for i in range(6):
            v = float(i) + d * 0.01
            rows.append({"trade_date": d, "symbol": f"S{i}",
                         "f": v, "fwd_ret_1": v})
    return pl.DataFrame(rows)


def test_summary_has_threshold_rate_and_kurtosis():
    res = ic_summary(_perfect_df(), "f", "fwd_ret_1")
    ic = res["ic"]
    # 完全正相关：|IC|>0.02 全部成立；恒定 IC=1 无方差 → 峰度无定义（NaN）
    assert ic["ic_gt_002_rate"] == 1.0
    assert not math.isfinite(ic["kurtosis"])
    assert "ic_gt_002_rate" in res["rank_ic"]


def test_summary_threshold_rate_low_when_noise():
    random.seed(7)
    rows = []
    for d in range(40):
        for i in range(8):
            rows.append({"trade_date": d, "symbol": f"S{i}",
                         "f": random.random(),
                         "f_with_noise": random.random()})
    df = pl.DataFrame(rows)
    res = ic_summary(df, "f", "f_with_noise")
    # 纯噪声 → 阈值胜率应明显低于完美因子的 1.0
    assert res["ic"]["ic_gt_002_rate"] < 0.9


# ────────────────────────── IC 自相关 ──────────────────────────

def test_ic_autocorr_monotone_series_is_one():
    """严格递增序列的 lag-1 自相关恰好为 1（手算：协方差 = 方差）。"""
    s = pl.Series("ic", [1.0, 2.0, 3.0, 4.0, 5.0])
    assert ic_autocorr(s, 1) == pytest.approx(1.0)


def test_ic_autocorr_alternating_series_is_negative():
    s = pl.Series("ic", [1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
    assert ic_autocorr(s, 1) == pytest.approx(-1.0)


def test_ic_autocorr_handles_degenerate_inputs():
    assert not math.isfinite(ic_autocorr(pl.Series("ic", [1.0]), 1))
    assert not math.isfinite(ic_autocorr(pl.Series("ic", [0.5] * 20), 1))
    assert not math.isfinite(ic_autocorr(pl.Series("ic", []), 1))
    assert not math.isfinite(ic_autocorr(pl.Series("ic", [1.0] * 10), 0))


def test_ic_autocorr_ignores_nulls():
    s = pl.Series("ic", [1.0, None, 2.0, 3.0, 4.0, 5.0])
    # 去空后是严格递增序列
    assert ic_autocorr(s, 1) == pytest.approx(1.0)


def test_ic_summary_reports_autocorr():
    res = ic_summary(_perfect_df(), "f", "fwd_ret_1")
    # 恒定 IC 无方差 → 自相关无定义，但字段必须在
    assert "ic_autocorr" in res["ic"]
    assert "ic_autocorr" in res["rank_ic"]
