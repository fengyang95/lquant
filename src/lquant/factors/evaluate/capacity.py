"""容量 / 流动性分析：这个策略能装多少钱。

口径全部写在报告里，读者可以自己复算：

- **组合 ADV**：多空两端持仓（等权）的日均成交额。先按日截面取两端会员的
  ``amount`` 均值，再对交易日取均值。
- **日均换手**：``factor_turnover`` 的两端均值（日频，小数）。
- **参与率**：``AUM × 日均换手 / 组合 ADV`` —— 每天要换掉的名义金额占这些票
  当日成交额的比例。
- **容量上限**：参与率不超过 ``max_participation`` 的最大 AUM，闭式解
  ``max_participation × ADV / 日均换手``。
- **冲击成本**：平方根模型 ``impact_coef × sqrt(参与率)``（收益口径），
  与 ``base_bps`` 相加得到单边总成本（bps）。
- **年化成本**：``年化换手 × 单边总成本``；净收益 = 毛收益 − 年化成本。

这些都是**估算**，不是实测冲击。目的是给出量级与「多大规模开始明显吃亏」的
拐点，而不是精确预测成交价 —— 报告里也这么写。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.backtest.metrics import perf_from_returns
from lquant.factors.evaluate.costs import factor_turnover
from lquant.factors.evaluate.quantile import add_quantile, long_short_nav

__all__ = ["capacity_summary", "portfolio_adv", "DEFAULT_AUM_LIST", "PERIODS_PER_YEAR"]

DEFAULT_AUM_LIST = [1e8, 5e8, 1e9, 5e9, 1e10]
PERIODS_PER_YEAR = 252

_AMOUNT_CANDIDATES = ("amount", "turnover_amount", "turnover_value")


def portfolio_adv(df: pl.DataFrame, factor: str, n_groups: int = 10, *,
                  amount_col: str = "amount", date_col: str = "trade_date") -> float:
    """多空两端持仓的日均成交额（等权：先按日截面取均值，再对交易日取均值）。"""
    d = add_quantile(df, factor, n_groups, date_col=date_col)
    legs = d.filter(
        pl.col("q").is_not_null()
        & ((pl.col("q") == 1) | (pl.col("q") == n_groups))
    )
    if not len(legs):
        return float("nan")
    per_day = legs.group_by(date_col).agg(pl.col(amount_col).mean().alias("_adv"))
    adv = per_day["_adv"].drop_nulls().mean()
    return float(adv) if adv is not None else float("nan")


def capacity_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                     n_groups: int = 10,
                     aum_list: list[float] | None = None,
                     amount_col: str | None = None,
                     max_participation: float = 0.1,
                     base_bps: float = 5.0,
                     impact_coef: float = 0.1,
                     date_col: str = "trade_date") -> dict:
    """容量 / 流动性估算。

    ``max_participation`` 是「每天换手名义金额 / 组合当日成交额」的上限；
    ``base_bps`` 是显性成本（佣金+印花税+滑点的固定部分，单边）；
    ``impact_coef`` 是平方根冲击模型的系数。

    返回 dict，``rows`` 为各 AUM 档位的参与率、成本与净收益。
    """
    if amount_col is None:
        amount_col = next((c for c in _AMOUNT_CANDIDATES if c in df.columns), None)
    if amount_col is None:
        raise KeyError(f"容量分析需要成交额列（{ '/'.join(_AMOUNT_CANDIDATES) }）；当前面板没有")
    if aum_list is None:
        aum_list = list(DEFAULT_AUM_LIST)

    adv = portfolio_adv(df, factor, n_groups, amount_col=amount_col, date_col=date_col)

    to = factor_turnover(df, factor, n_groups, date_col=date_col)
    to_mean = float(to["turnover_avg"].drop_nulls().mean()) if len(to) else float("nan")
    annual_turnover = to_mean * PERIODS_PER_YEAR if math.isfinite(to_mean) else float("nan")

    ls = long_short_nav(df, factor, ret_col, n_groups, date_col=date_col)
    gross = (perf_from_returns(ls["ret_long_short"].to_numpy(),
                               periods_per_year=PERIODS_PER_YEAR)["annual_return"]
             if len(ls) else float("nan"))

    # 换手未知或 ADV 不可用 → 容量无法评估，全部记 NaN（而不是编一个数出来）
    tradable = math.isfinite(adv) and adv > 0 and math.isfinite(to_mean) and to_mean > 0
    capacity = (max_participation * adv / to_mean) if tradable else float("nan")

    rows: list[dict] = []
    for aum in aum_list:
        if tradable:
            part = float(aum) * to_mean / adv
            impact_bps = impact_coef * math.sqrt(part) * 1e4
            total_bps = base_bps + impact_bps
            annual_cost = (annual_turnover * total_bps / 1e4
                           if math.isfinite(annual_turnover) else float("nan"))
            net = gross - annual_cost if math.isfinite(gross) and math.isfinite(annual_cost) \
                else float("nan")
        else:
            part = impact_bps = total_bps = annual_cost = net = float("nan")
        rows.append({
            "aum": float(aum),
            "participation": part,
            "impact_bps": impact_bps,
            "total_cost_bps": total_bps,
            "annual_cost": annual_cost,
            "net_annual": net,
            "viable": bool(math.isfinite(net) and net > 0),
        })

    return {
        "amount_col": amount_col,
        "portfolio_adv": adv,
        "turnover_avg": to_mean,
        "annual_turnover": annual_turnover,
        "max_participation": float(max_participation),
        "capacity_aum": capacity,
        "base_bps": float(base_bps),
        "impact_coef": float(impact_coef),
        "gross_annual": gross,
        "rows": rows,
    }
