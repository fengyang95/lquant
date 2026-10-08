"""协变量一等公民（方案 5.2）：CovariateProvider 注册表 + PIT 行业 + 覆盖率上报。

三条硬约束：
1. 必须 PIT：行业按 std_date as-of 关联（用今天的分类回测十年前 = 前视偏差）
2. 覆盖率显式上报 —— 覆盖率本身是结论的一部分
3. 缺失整行剔除，绝不填 0（缺失市值填 0 = 当成最小市值）
"""
from __future__ import annotations

import polars as pl

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


def covariates_for_steps(steps, base: list[str]) -> list[str]:
    """在默认协变量之外，把**用户配方显式引用**的 ``cov_*`` 按需补上。

    为什么需要它：默认只构建 4 个协变量（口径稳定、可复现），但配方里写
    ``cov_beta_1y`` 时如果不构建，中性化会以「列不存在」失败 —— 能力注册了
    却调不到，正是本项目一直在防的那种退化。按需构建只影响显式引用者，
    默认口径一个字节都不变。

    名字不在注册表里就不加：让下游以「缺列」报错，而不是静默少中性化一个维度。
    """
    out = list(base)
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        raw = step.get("by") or step.get("factors") or []
        if isinstance(raw, str):
            raw = [raw]
        for name in raw:
            if not isinstance(name, str) or not name.startswith("cov_"):
                continue
            bare = name[len("cov_"):]
            if bare not in out and bare in PROVIDERS:
                out.append(bare)
    return out




def _panel_sorted(panel):
    return panel.sort(["symbol", "trade_date"])


@provider("industry_sw1", label="shenwan L1 industry, PIT as-of join")
def _p_industry(panel, industry_df=None):
    if industry_df is None or not len(industry_df):
        raise CovariateUnavailable("industry classify data not provided")
    ind = industry_df.filter(pl.col("std") == "SW").select(["symbol", "std_date", "code"]).sort("std_date")
    d = _panel_sorted(panel).select(["trade_date", "symbol"]).unique()
    # check_sortedness=False：带 by 组时 polars 无法跨组校验排序，每次调用都会
    # 刷一条 UserWarning 噪音。组内按 trade_date 有序由上面的 sort 保证。
    out = d.join_asof(ind.rename({"code": "industry_sw1", "std_date": "ind_date"}),
                      left_on="trade_date", right_on="ind_date", by="symbol",
                      strategy="backward", check_sortedness=False)
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


# ---------------------------------------------------------------- 风格因子
# 补齐 CNE5 的六个风格维度：原来只有 Size（market_cap）、Momentum（momentum_1m）、
# Liquidity（turnover_1m）+ 行业，缺 **Beta / 残差波动率 / 非线性规模**。
# 只做到 Size+Industry 的中性化是「假中性化」—— 残差里还留着 Beta 与波动率
# 暴露，因子看起来有效但赚的是风格轮动（见 docs/research/competitors/99-*）。

#: Beta / 残差波动率的滚动窗口（交易日）。CNE5 用 252 日 + 半衰期加权，
#: 这里用等权满窗口 —— 口径更简单，且与 lquant「满窗口才出值」的约定一致。
BETA_WINDOW = 252

#: 市场收益代理 = 当日截面等权平均收益（``_ret`` 的截面均值，见 _beta_frame）。
#: 没有指数日线也能算（指数表可能为空），且等权口径与 lquant 现有的
#: 「基准 = 全市场等权」归因口径一致（见 backtest/attribution.py）。
#: 代价是它把个股自己也包含进市场里（小票池尤其明显）—— 报告里写明。


def _beta_frame(panel, window: int) -> pl.DataFrame:
    """滚动 Beta 与残差波动率（同一窗口，一次算完）。"""
    if "close" not in panel.columns:
        raise CovariateUnavailable("panel has no close column")
    d = (_panel_sorted(panel)
         .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0)
                       .alias("_ret"))
         .with_columns(pl.col("_ret").mean().over("trade_date").alias("_mkt")))
    d = d.with_columns(
        pl.rolling_cov("_ret", "_mkt", window_size=window, min_samples=window)
        .over("symbol").alias("_cov"),
        pl.col("_mkt").rolling_var(window, min_samples=window).over("symbol").alias("_mvar"))
    d = d.with_columns(
        pl.when(pl.col("_mvar") > 0).then(pl.col("_cov") / pl.col("_mvar")).otherwise(None)
        .alias("beta_1y"))
    d = d.with_columns(
        (pl.col("_ret") - pl.col("beta_1y") * pl.col("_mkt")).alias("_resid"))
    d = d.with_columns(
        pl.col("_resid").rolling_std(window, min_samples=window).over("symbol")
        .alias("resid_vol"))
    return d.select(["trade_date", "symbol", "beta_1y", "resid_vol"])


@provider("beta_1y", label=f"rolling beta vs equal-weight market ({BETA_WINDOW}d)")
def _p_beta(panel, industry_df=None, window: int = BETA_WINDOW):
    return _beta_frame(panel, window).select(["trade_date", "symbol", "beta_1y"])


@provider("resid_vol", label=f"residual volatility ({BETA_WINDOW}d)")
def _p_resid_vol(panel, industry_df=None, window: int = BETA_WINDOW):
    return _beta_frame(panel, window).select(["trade_date", "symbol", "resid_vol"])


@provider("non_linear_size", label="cube of cross-sectional size z-score")
def _p_nonlinear_size(panel, industry_df=None):
    """非线性规模：Size 的截面 z-score 取立方。

    CNE5 用三次样条刻画「极小盘与极大盘的非线性收益」，这里用 ``z³`` 近似
    （同号、两端放大、单峰形状一致），口径写在 label 与报告里，不假装是
    原版样条。中性化时它与 Size 高度共线，两者一起放进回归是**有意**的：
    样条思想就是「线性 Size 之外还剩什么」。
    """
    v = _p_mcap(panel)
    v = v.with_columns(
        ((pl.col("market_cap") - pl.col("market_cap").mean().over("trade_date"))
         / pl.col("market_cap").std().over("trade_date")).alias("_z"))
    v = v.with_columns((pl.col("_z") ** 3).alias("non_linear_size"))
    return v.select(["trade_date", "symbol", "non_linear_size"])
