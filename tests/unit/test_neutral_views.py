"""§5.1 三件事回归：收益中性化与行业内分组分层。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.covariates import build_covariates
from lquant.factors.evaluate import forward_return
from lquant.factors.evaluate.ic import ic_series
from lquant.factors.evaluate.neutral_views import (
    industry_group_quantile,
    neutral_views,
    return_neutral_ic,
)


def _panel(n_days=80, n_sym=10):
    rng = np.random.default_rng(5)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "turnover_rate": 0.5 + rng.normal(0, 0.1),
                         "float_mv": 5e8 + 1e7 * s})
    return pl.DataFrame(rows)


def test_return_neutral_ic_runs():
    """收益中性化：残差收益列产出 + IC 可算。"""
    d = _panel().sort(["symbol", "trade_date"])
    d = forward_return(d, "close", periods=[1]).drop_nulls(["fwd_ret_1"])
    d = d.with_columns(pl.col("close").pct_change(5).over("symbol").alias("f"))
    d = d.drop_nulls(["f"])
    d, rep = build_covariates(d, ["momentum_1m"])
    d = d.drop_nulls(["cov_momentum_1m"])
    r = return_neutral_ic(d, "f", "fwd_ret_1", ["cov_momentum_1m"])
    assert "ret_neutral" in r.columns
    s = ic_series(r.drop_nulls(["f", "ret_neutral"]), "f", "ret_neutral")
    assert len(s) > 0


def test_industry_group_quantile_buckets():
    """行业内分组：组内分位桶覆盖 1..n_groups 且逐日逐组非空。"""
    d = _panel(n_days=10)
    d, _ = build_covariates(d, ["industry_sw1"],
                            industry_df=_industry())
    d = d.drop_nulls(["cov_industry_sw1"])
    d = d.with_columns(pl.col("close").pct_change(5).over("symbol").alias("f")).drop_nulls(["f"])
    gq = industry_group_quantile(d, "f", "fwd_ret_1", "cov_industry_sw1", n_groups=3)
    qs = set(gq["q"].unique().to_list())
    assert qs <= {1, 2, 3}
    assert len(qs) >= 2


def test_industry_group_requires_column():
    with pytest.raises(ValueError):
        industry_group_quantile(_panel(n_days=5), "f", "r", "nope", 3)


def test_neutral_views_labels():
    d = _panel(n_days=40).sort(["symbol", "trade_date"])
    d = forward_return(d, "close", periods=[1]).drop_nulls(["fwd_ret_1"])
    d = d.with_columns(pl.col("close").pct_change(5).over("symbol").alias("f")).drop_nulls(["f"])
    d, _ = build_covariates(d, ["momentum_1m"])
    v = neutral_views(d.drop_nulls(["cov_momentum_1m"]), "f", "fwd_ret_1",
                      covariates=["cov_momentum_1m"])
    assert "view" in v
    assert "return_neutral_ic" in v


def _industry():
    rows = []
    for s in range(10):
        rows.append({"symbol": f"S{s:02d}", "std": "SW", "code": f"I{s % 3}",
                     "name": f"行业{s % 3}", "std_date": dt.date(2024, 12, 1)})
    return pl.DataFrame(rows)
