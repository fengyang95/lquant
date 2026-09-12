"""M2.5 中性化专项 N1-N6 + CovariateProvider 回归。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import FactorError
from lquant.factors.covariates import build_covariates
from lquant.factors.ops import cs_ops, el_ops, ts_ops  # noqa: F401
from lquant.factors.preprocess.pipeline import run as pipeline_run


def _panel(n_days=60, n_sym=8, seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "turnover_rate": 0.5 + 0.01 * i, "float_mv": 5e8 + 1e7 * s})
    return pl.DataFrame(rows)


def _industry():
    rows = []
    for s in range(8):
        rows.append({"symbol": f"S{s:02d}", "std": "SW", "code": f"I{s % 3}",
                     "name": f"行业{s % 3}", "std_date": dt.date(2024, 12, 1)})
    return pl.DataFrame(rows)



def test_n2_self_neutralize_zero():
    """N2: neutralize(x, [x]) == 0 —— 一句话验证整条回归链路。"""
    d = _panel().with_columns(pl.col("close").pct_change(5).over("symbol").alias("f"))
    steps = [{"op": "neutralize", "method": "ols", "factors": ["f"]}]
    out = pipeline_run(d.drop_nulls(["f"]), "f", steps)
    resid = out["f"].drop_nulls()
    assert resid.abs().max() < 1e-8


def test_n6_all_covariates_missing_raises():
    """N6: 协变量全缺失必须报错，不得静默去均值。"""
    d = _panel().with_columns(pl.col("close").pct_change(5).over("symbol").alias("f"))
    steps = [{"op": "neutralize", "method": "ols", "factors": ["market_cap", "industry_sw1"]}]
    with pytest.raises(FactorError):
        pipeline_run(d.drop_nulls(["f"]), "f", steps)


def test_n4_residual_orthogonal():
    """N4: 残差与设计矩阵正交（OLS 一阶条件）。"""
    d = _panel().with_columns(pl.col("close").pct_change(5).over("symbol").alias("f"))
    d, report = build_covariates(d, ["momentum_1m"])
    d = d.drop_nulls(["f", "cov_momentum_1m"])
    steps = [{"op": "neutralize", "method": "ols", "factors": ["cov_momentum_1m"]}]
    out = pipeline_run(d, "f", steps)
    j = out.drop_nulls(["f", "cov_momentum_1m"])
    r = np.corrcoef(j["f"].to_numpy(), j["cov_momentum_1m"].to_numpy())[0, 1]
    assert abs(r) < 1e-6



def test_industry_pit_asof():
    """N3/PIT: 行业按 std_date as-of 关联 —— std_date 之后才用该分类。"""
    d = _panel(n_days=10)
    ind = _industry()
    # 生效日推到面板第 8 天 → 前 7 天行业应为 null
    late = _industry().with_columns(pl.col("std_date") + pl.duration(days=39))
    _, rep_early = build_covariates(d, ["industry_sw1"], industry_df=ind)
    _, rep_late = build_covariates(d, ["industry_sw1"], industry_df=late)
    assert rep_early[0]["coverage"] == pytest.approx(1.0)
    assert rep_late[0]["coverage"] < 1.0


def test_industry_asof_no_sortedness_warning():
    """join_asof 带 by 组时 polars 无法跨组校验排序，曾每次调用刷一条
    UserWarning 噪音。修复后（显式 check_sortedness=False，组内有序由
    sort 保证）不得再出现该告警 —— 告警会让 Agent 误判成数据问题。"""
    import warnings

    d = _panel(n_days=10)
    ind = _industry()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out, rep = build_covariates(d, ["industry_sw1"], industry_df=ind)
    assert rep[0]["coverage"] == pytest.approx(1.0)


def test_missing_covariate_never_filled_zero():
    """硬约束 3: 缺失整行剔除/留 null，绝不填 0。"""
    d = _panel(n_days=10)
    d, rep = build_covariates(d, ["industry_sw1"], industry_df=None)
    assert "cov_industry_sw1" not in d.columns
    assert rep[0]["coverage"] == 0.0


def test_market_cap_is_log():
    """N5/#13: market_cap 协变量 = log1p(市值)，不是原值。"""
    d = _panel(n_days=10)
    out, _ = build_covariates(d, ["market_cap"])
    raw = out["float_mv"].to_numpy()
    got = out["cov_market_cap"].to_numpy()
    assert np.allclose(got, np.log1p(raw))
