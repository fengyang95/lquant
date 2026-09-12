"""中性化后风格相关性：残差因子不应再偷风格暴露。"""
from __future__ import annotations

import random

import polars as pl
import pytest

from lquant.factors.evaluate.style_corr import style_correlation


def _df() -> pl.DataFrame:
    """5 天 × 8 只：因子 = 风格 size 的强线性函数 + 微噪声 → 相关应接近 1。"""
    random.seed(3)
    rows = []
    for d in range(1, 6):
        for i in range(8):
            size = float(i) + random.uniform(-0.05, 0.05)
            rows.append({"symbol": f"S{i}", "trade_date": d,
                         "f": size + random.uniform(-0.01, 0.01),
                         "size": size,
                         "noise": random.gauss(0, 1)})
    return pl.DataFrame(rows)


def test_high_correlation_detected_and_fails_threshold():
    res = style_correlation(_df(), "f", ["size"], threshold=0.14)
    s = res["styles"][0]
    assert s["corr_mean"] > 0.99
    assert s["passed"] is False
    assert res["passed"] is False
    assert res["max_abs"] > 0.99


def test_independent_style_passes():
    # 40 只/日 × 5 天的独立变量：均值应接近 0，阈值 0.5 内应判通过
    random.seed(9)
    rows = []
    for d in range(1, 6):
        for i in range(40):
            rows.append({"symbol": f"S{i}", "trade_date": d,
                         "f": random.gauss(0, 1), "noise": random.gauss(0, 1)})
    df = pl.DataFrame(rows)
    res = style_correlation(df, "f", ["noise"], threshold=0.5)
    assert abs(res["styles"][0]["corr_mean"]) < 0.2
    assert res["passed"] is True


def test_industry_eta_categorical():
    # 组间均值差异悬殊 → eta 接近 1；组内差异主导 → eta 接近 0
    rows = [{"trade_date": 1, "f": 10.0, "ind": "A"}, {"trade_date": 1, "f": 10.2, "ind": "A"},
            {"trade_date": 1, "f": 0.0, "ind": "B"}, {"trade_date": 1, "f": 0.2, "ind": "B"}]
    strong = style_correlation(pl.DataFrame(rows), "f", [], group_col="ind")
    assert strong["styles"][0]["corr_abs_max"] > 0.99

    rows2 = [{"trade_date": 1, "f": 10.0, "ind": "A"}, {"trade_date": 1, "f": 0.0, "ind": "A"},
             {"trade_date": 1, "f": 10.2, "ind": "B"}, {"trade_date": 1, "f": 0.2, "ind": "B"}]
    weak = style_correlation(pl.DataFrame(rows2), "f", [], group_col="ind")
    assert weak["styles"][0]["corr_abs_max"] < 0.1


def test_missing_style_column_tolerated():
    res = style_correlation(_df(), "f", ["not_here"], threshold=0.14)
    assert res["styles"][0]["passed"] is None
    assert res["max_abs"] is None and res["passed"] is False
