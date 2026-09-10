"""IC 汇总补充指标：|IC|>0.02 阈值胜率、峰度。"""
from __future__ import annotations

import math
import random

import polars as pl

from lquant.factors.evaluate.ic import ic_summary


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
