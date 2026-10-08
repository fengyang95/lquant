"""CNE5 风格协变量：Beta / 残差波动率 / 非线性规模。

只对 Size + 行业中性化是「假中性化」—— 残差里还留着 Beta 与波动率暴露，
因子看着有效，赚的是风格轮动。这三个 provider 把风格维度补齐。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.factors.covariates import (
    BETA_WINDOW,
    PROVIDERS,
    CovariateUnavailable,
    build_covariates,
)


def _panel(n_days: int = 40, betas: tuple[float, ...] = (1.0, 2.0)) -> pl.DataFrame:
    """构造两只股票：收益 = beta × 市场收益 + 独立噪声（噪声让回归可辨识）。"""
    rows = []
    mkt = [0.01 * math.sin(i / 3.0) for i in range(n_days)]
    for k, beta in enumerate(betas):
        sym = f"60000{k}.SH"
        px = 10.0
        for i in range(n_days):
            r = beta * mkt[i] + 0.002 * math.cos(i * (k + 1))
            rows.append({
                "symbol": sym,
                "trade_date": date(2026, 1, 5) + timedelta(days=i),
                "close": px,
                "float_mv": 1e9 * (k + 1),
            })
            px = px * (1.0 + r)
    return pl.DataFrame(rows)


def test_new_style_providers_registered():
    for name in ("beta_1y", "resid_vol", "non_linear_size"):
        assert name in PROVIDERS, f"{name} 未注册，前端/配方枚举不到"


def test_beta_recovers_relative_exposure():
    """Beta 接近构造时的相对暴露（1.0 vs 2.0）—— 绝对值受市场代理口径影响，
    所以断言的是**相对关系**而不是具体数值。"""
    from lquant.factors.covariates import _p_beta

    out = _p_beta(_panel(n_days=BETA_WINDOW + 5), window=BETA_WINDOW)
    assert out.height == 2 * (BETA_WINDOW + 5)
    last = (out.sort(["symbol", "trade_date"]).group_by("symbol").last()
               .sort("symbol"))
    assert last["beta_1y"].null_count() == 0
    b0, b1 = last["beta_1y"].to_list()
    assert b1 > b0 > 0
    assert 1.5 < (b1 / b0) < 2.5


def test_resid_vol_positive_and_window_zero_before_full():
    """满窗口才出值：不足窗口的行必须是 null，不能拿部分窗口糊上去。"""
    from lquant.factors.covariates import _p_resid_vol

    out = _p_resid_vol(_panel(n_days=20), window=10)
    first_9 = out.sort(["symbol", "trade_date"]).filter(
        pl.col("trade_date") < date(2026, 1, 5) + timedelta(days=9))
    assert first_9["resid_vol"].null_count() == len(first_9)
    tail = out.filter(pl.col("resid_vol").is_not_null())
    assert len(tail) > 0 and (tail["resid_vol"] > 0).all()


def test_non_linear_size_is_cross_sectional_cube():
    from lquant.factors.covariates import _p_nonlinear_size

    out = _p_nonlinear_size(_panel(n_days=5))
    row = out.filter(pl.col("trade_date") == date(2026, 1, 5)).sort("symbol")
    vals = row["non_linear_size"].to_list()
    # 两只票：z 一正一负、等大 → 立方符号相反、绝对值相同；更大的市值取正
    assert abs(vals[0] + vals[1]) < 1e-9
    assert vals[1] > 0 > vals[0]


def test_beta_provider_needs_close():
    from lquant.factors.covariates import _p_beta

    df = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2026, 1, 5)],
                       "float_mv": [1e9]})
    with pytest.raises(CovariateUnavailable, match="close"):
        _p_beta(df, window=5)


def test_build_covariates_reports_coverage_for_new_factors():
    """覆盖率必须上报：beta 需要 252 日，早期样本为 null 要看得见。"""
    panel = _panel(n_days=12)
    out, report = build_covariates(
        panel, ["market_cap", "non_linear_size", "beta_1y"], industry_df=None)
    cov = {r["covariate"]: r["coverage"] for r in report}
    assert cov["market_cap"] == 1.0
    assert cov["non_linear_size"] == 1.0
    # 12 天远不足 252 日窗口 → 覆盖率为 0，且是**显式**的 0
    assert cov["beta_1y"] == 0.0
    assert "cov_beta_1y" in out.columns
    assert out["cov_beta_1y"].null_count() == len(out)


def test_covariates_for_steps_builds_only_referenced_names():
    """配方里引用的 cov_* 按需构建；默认列表与未知名字都不受影响。"""
    from lquant.factors.covariates import covariates_for_steps

    base = ["market_cap", "industry_sw1"]
    steps = [{"op": "neutralize", "method": "ols",
              "by": ["market_cap", "cov_beta_1y", "cov_resid_vol",
                     "cov_industry_sw1", "cov_typo_not_registered"]}]
    out = covariates_for_steps(steps, base)
    assert out[:2] == base                      # 默认列表顺序与内容不变
    assert "beta_1y" in out and "resid_vol" in out
    assert "industry_sw1" in out                # 已在 base 里，不重复
    assert out.count("beta_1y") == 1
    # 未注册的名字不构建 → 下游以「缺列」报错，而不是静默少中性化一维
    assert "typo_not_registered" not in out
    # 不引用 = 不加
    assert covariates_for_steps(None, base) == base
    assert covariates_for_steps([{"op": "standardize"}], base) == base
