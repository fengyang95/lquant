"""换手率与成本敏感性：很多高 IC 因子扣完双边万 15 就没了，这张表就是照妖镜。

换手定义：每日多空两端持仓与上一日的重合率 → turnover = 1 - overlap。
成本：net = gross 扣 turnover_avg × (bps/1e4) × 2（双边）。
"""
from __future__ import annotations

import polars as pl

from lquant.backtest.metrics import perf_from_returns
from lquant.factors.evaluate.quantile import add_quantile, long_short_nav

__all__ = ["factor_turnover", "cost_adjusted_nav", "cost_matrix"]


def factor_turnover(df: pl.DataFrame, factor: str, n_groups: int = 10, *,
                    date_col: str = "trade_date", top: int | None = None,
                    bottom: int | None = None) -> pl.DataFrame:
    """每日多空两端组会员跨日重合率 → 换手率。

    turnover = 1 - |当日组 ∩ 上日组| / |当日组|（以当日组为基数）。
    返回列：date, turnover_long, turnover_short, turnover_avg。
    首日无上一日持仓可比，直接不计入（输出从第 2 个交易日起）。
    """
    top = top or n_groups
    bottom = bottom or 1
    d = add_quantile(df, factor, n_groups, date_col=date_col)
    d = d.select([date_col, "q"] + (["symbol"] if "symbol" in d.columns else []))
    if "symbol" not in d.columns:
        raise KeyError("factor_turnover 需要 symbol 列标识会员")
    d = d.drop_nulls(subset=["q"])

    date_dtype = df[date_col].dtype
    empty = pl.DataFrame(schema={"date": date_dtype,
                                 "turnover_long": pl.Float64,
                                 "turnover_short": pl.Float64,
                                 "turnover_avg": pl.Float64})
    n_dates = d[date_col].n_unique()
    if n_dates < 2:
        return empty

    def _members(sub: pl.DataFrame, q: int) -> set:
        return set(sub.filter(pl.col("q") == q)["symbol"].to_list())

    prev_long: set | None = None
    prev_short: set | None = None
    rows = []
    for dt_raw, day_raw in d.group_by(date_col, maintain_order=True):
        dt = dt_raw[0] if isinstance(dt_raw, (list, tuple)) else dt_raw
        day = day_raw.sort("symbol")
        cur_long = _members(day, top)
        cur_short = _members(day, bottom)
        if prev_long is None:      # 首日无可比持仓，不产生换手记录
            prev_long, prev_short = cur_long, cur_short
            continue
        # 某端当日为空（如股票数 < n_groups）时记 null（不用 NaN，避免毒化均值），avg 只对有效端取均值
        tl = 1.0 - len(cur_long & prev_long) / len(cur_long) if cur_long else None
        ts = 1.0 - len(cur_short & prev_short) / len(cur_short) if cur_short else None
        vals = [v for v in (tl, ts) if v is not None]
        avg = sum(vals) / len(vals) if vals else None
        rows.append({date_col: dt, "turnover_long": tl, "turnover_short": ts,
                     "turnover_avg": avg})
        prev_long, prev_short = cur_long, cur_short
    out = pl.DataFrame(rows).rename({date_col: "date"}).sort("date")
    return out.select(["date", "turnover_long", "turnover_short", "turnover_avg"])


def cost_adjusted_nav(ls_nav: pl.DataFrame, turnover_df: pl.DataFrame,
                      bps_list: list[float], *,
                      date_col: str = "trade_date") -> dict[float, pl.DataFrame]:
    """多空净值扣成本：净收益 = ret_long_short - turnover_avg × (bps/1e4) × 2。

    返回 {bps: 净值 DataFrame}，每帧含 date, ret_net, nav_net。
    """
    j = ls_nav.join(turnover_df.rename({"date": date_col}), on=date_col, how="inner")
    out: dict[float, pl.DataFrame] = {}
    for bps in bps_list:
        cost = pl.col("turnover_avg") * (bps / 1e4) * 2.0
        net = j.with_columns(
            (pl.col("ret_long_short") - cost).alias("ret_net")
        ).select([
            pl.col(date_col).alias("date"),
            "ret_net",
            (1.0 + pl.col("ret_net")).cum_prod().alias("nav_net"),
        ])
        out[float(bps)] = net
    return out


def cost_matrix(df: pl.DataFrame, factor: str, ret_col: str, *,
                bps_list: list[float] | None = None, n_groups: int = 10,
                date_col: str = "trade_date",
                periods_per_year: int = 252) -> pl.DataFrame:
    """每行一个 bps 的成本敏感性表。

    列：bps, gross_annual, net_annual, annual_turnover, viable。
    viable = net_annual > 0。
    """
    if bps_list is None:
        bps_list = [0.0, 5.0, 10.0, 15.0, 30.0, 50.0]
    ls = long_short_nav(df, factor, ret_col, n_groups, date_col=date_col)
    to = factor_turnover(df, factor, n_groups, date_col=date_col)
    gross = perf_from_returns(ls["ret_long_short"].to_numpy(),
                              periods_per_year=periods_per_year)["annual_return"]
    mean_t = to["turnover_avg"].drop_nulls().mean() if len(to) else None
    ann_turnover = float(mean_t) * periods_per_year if mean_t is not None else float("nan")
    rows = []
    for bps in bps_list:
        # 换手未知（全部端组为空）→ 无法评估可用性，net 记 NaN、viable=False（保守）
        cost_per_day = float(mean_t) * (bps / 1e4) * 2.0 if mean_t is not None else None
        if cost_per_day is None:
            rows.append({"bps": float(bps), "gross_annual": gross, "net_annual": float("nan"),
                         "annual_turnover": float("nan"), "viable": False})
            continue
        net = perf_from_returns(
            (ls["ret_long_short"] - cost_per_day).to_numpy(),
            periods_per_year=periods_per_year)["annual_return"]
        rows.append({"bps": float(bps), "gross_annual": gross, "net_annual": net,
                     "annual_turnover": ann_turnover, "viable": bool(net > 0)})
    return pl.DataFrame(rows)
