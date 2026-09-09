"""协变量一等公民（方案 5.2）：CovariateProvider 注册表 + PIT 行业 + 覆盖率上报。

三条硬约束：
1. 必须 PIT：行业按 std_date as-of 关联（用今天的分类回测十年前 = 前视偏差）
2. 覆盖率显式上报 —— 覆盖率本身是结论的一部分
3. 缺失整行剔除，绝不填 0（缺失市值填 0 = 当成最小市值）
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import FactorError
from lquant.core.registry import Registry

PROVIDERS: Registry = Registry("covariate_providers")


def provider(name: str, label: str, kind: str = "num"):
    return PROVIDERS.register(name, {"label": label, "kind": kind})


def build_covariates(panel, names, industry_df=None):
    out = panel
    report = []
    for n in names:
        fn = PROVIDERS.get(n)
        try:
            cov = fn(panel, industry_df=industry_df)
            if cov is None:
                report.append({"covariate": n, "coverage": 0.0, "note": "provider returned None"})
                continue
            col = f"cov_{n}"
            value_col = [c for c in cov.columns if c not in ("trade_date", "symbol")][0]
            slim = cov.select(["trade_date", "symbol", value_col]).rename({value_col: col})
            out = out.join(slim, on=["trade_date", "symbol"], how="left")
            coverage = 1 - out[col].null_count() / max(len(out), 1)
            report.append({"covariate": n, "coverage": round(coverage, 4), "note": ""})
        except CovariateUnavailable as e:
            report.append({"covariate": n, "coverage": 0.0, "note": str(e)})
    return out, report



class CovariateUnavailable(Exception):
    """provider 数据不可用：调用方上报 coverage=0，绝不静默。"""




def _panel_sorted(panel):
    return panel.sort(["symbol", "trade_date"])


@provider("industry_sw1", label="shenwan L1 industry, PIT as-of join")
def _p_industry(panel, industry_df=None):
    if industry_df is None or not len(industry_df):
        raise CovariateUnavailable("industry classify data not provided")
    ind = industry_df.filter(pl.col("std") == "SW").select(["symbol", "std_date", "code"]).sort("std_date")
    d = _panel_sorted(panel).select(["trade_date", "symbol"]).unique()
    out = d.join_asof(ind.rename({"code": "industry_sw1", "std_date": "ind_date"}),
                      left_on="trade_date", right_on="ind_date", by="symbol", strategy="backward")
    return out.select(["trade_date", "symbol", "industry_sw1"])


@provider("turnover_1m", label="20d mean turnover")
def _p_turnover(panel, industry_df=None):
    if "turnover_rate" not in panel.columns:
        raise CovariateUnavailable("daily_bar has no turnover_rate column")
    v = _panel_sorted(panel).with_columns(
        pl.col("turnover_rate").rolling_mean(20).over("symbol").alias("turnover_1m"))
    return v.select(["trade_date", "symbol", "turnover_1m"])


@provider("momentum_1m", label="20d momentum")
def _p_momentum(panel, industry_df=None):
    v = _panel_sorted(panel).with_columns(
        pl.col("close").pct_change(20).over("symbol").alias("momentum_1m"))
    return v.select(["trade_date", "symbol", "momentum_1m"])


@provider("market_cap", label="log(1+float_mv) (#13)")
def _p_mcap(panel, industry_df=None):
    if "float_mv" not in panel.columns:
        raise CovariateUnavailable("daily_bar has no float_mv (shares) data")
    v = _panel_sorted(panel).with_columns(pl.col("float_mv").log1p().alias("market_cap"))
    return v.select(["trade_date", "symbol", "market_cap"])
