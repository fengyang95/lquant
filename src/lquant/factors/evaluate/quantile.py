"""分层回测：十分位分组，看单调性和多空收益。

分层比 IC 更直观：IC 是一个数，分层是一张图。
一个因子哪怕 IC 一般，只要分组收益**单调**，就说明它是稳定的线性信号；
反过来，IC 高但分组不单调（只有第 10 组特别高），往往是几只极端股撑起来的，
实盘一上就被打回原形。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.backtest.metrics import perf_from_returns

__all__ = ["add_quantile", "group_returns", "quantile_nav", "quantile_summary",
           "long_short_nav"]


def add_quantile(df: pl.DataFrame, factor: str, n_groups: int = 10,
                 *, date_col: str = "trade_date", out: str = "q") -> pl.DataFrame:
    """按日截面打分位，1 = 因子值最低，n_groups = 最高。

    用 rank 而不是 qcut：qcut 遇到大量重复值（比如停牌、新股一字）会报错
    或产生不均匀分组，rank 分位则稳定得多。
    """
    cnt = pl.col(factor).count().over(date_col)
    q = (pl.col(factor).rank("ordinal").over(date_col) * n_groups / cnt).ceil().clip(1, n_groups)
    return df.with_columns(q.cast(pl.Int32).alias(out))


def group_returns(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                  n_groups: int = 10, *, date_col: str = "trade_date",
                  weighted: bool = False) -> pl.DataFrame:
    """每期每组的平均收益（组内等权）。"""
    d = add_quantile(df, factor, n_groups, date_col=date_col)
    d = d.select([date_col, "q", factor, ret_col]).drop_nulls()
    if weighted:
        agg = (pl.col(ret_col) * pl.col(factor).abs()).sum() / pl.col(factor).abs().sum()
    else:
        agg = pl.col(ret_col).mean()
    return (
        d.group_by([date_col, "q"])
        .agg([agg.alias("ret"), pl.len().alias("n")])
        .sort([date_col, "q"])
    )


def quantile_nav(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                 n_groups: int = 10, *, date_col: str = "trade_date") -> pl.DataFrame:
    """各组净值曲线。返回宽表：date + q1..qN + long_short。

    注意：这里的「收益」是**当期因子暴露对应的前瞻收益**，
    等权组合的换仓已经在收益定义里，不做额外的复利错位。
    """
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(g):
        return g
    navs = {}
    for q in range(1, n_groups + 1):
        sub = g.filter(pl.col("q") == q).sort(date_col)
        navs[f"q{q}"] = np_cumprod(sub["ret"].to_numpy())
    out = pl.DataFrame({date_col: g.filter(pl.col("q") == 1).sort(date_col)[date_col].to_list()})
    for k, v in navs.items():
        out = out.with_columns(pl.Series(k, v))
    out = out.with_columns((pl.col(f"q{n_groups}") - pl.col("q1")).alias("long_short"))
    return out


def np_cumprod(arr) -> list[float]:
    import numpy as np
    a = np.asarray(arr, dtype=float)
    a = np.nan_to_num(a, nan=0.0)
    return list(np.cumprod(1.0 + a))


def long_short_nav(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                   n_groups: int = 10, *, date_col: str = "trade_date",
                   top: int | None = None, bottom: int | None = None) -> pl.DataFrame:
    """多空组合净值（做多 top 组、做空 bottom 组，日度再平衡）。

    默认 1/N 十分位多空。常用替代：top=1, bottom=10（十分位）。
    """
    top = top or n_groups
    bottom = bottom or 1
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(g):
        return g
    dates = sorted(g[date_col].unique().to_list())
    hi, lo = {}, {}
    for d in dates:
        sub = g.filter(pl.col(date_col) == d)
        hi_d = sub.filter(pl.col("q") == top)
        lo_d = sub.filter(pl.col("q") == bottom)
        hi[d] = float(hi_d["ret"][0]) if len(hi_d) else 0.0
        lo[d] = float(lo_d["ret"][0]) if len(lo_d) else 0.0
    ls = [hi[d] - lo[d] for d in dates]
    return pl.DataFrame({
        date_col: dates,
        "ret_long": [hi[d] for d in dates],
        "ret_short": [lo[d] for d in dates],
        "ret_long_short": ls,
        "nav_long_short": np_cumprod(ls),
    })


def quantile_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                     n_groups: int = 10, *, date_col: str = "trade_date",
                     periods_per_year: int = 252) -> dict:
    """分层汇总：每组年化 + 多空绩效 + 单调性。"""
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(g):
        return {"factor": factor, "n_groups": n_groups, "groups": []}

    groups = []
    for q in range(1, n_groups + 1):
        sub = g.filter(pl.col("q") == q).sort(date_col)
        # 股票数 < n_groups 时部分分位组可能为空 —— mean 为 None，按 NaN 处理
        m = sub["ret"].mean()
        r = sub["ret"].to_numpy() if len(sub) else []
        p = perf_from_returns(r, periods_per_year=periods_per_year) if len(sub) else {}
        groups.append({
            "q": q,
            "mean_ret": float(m) if m is not None else float("nan"),
            "annual_return": p.get("annual_return", float("nan")),
            "sharpe": p.get("sharpe", float("nan")),
            "max_drawdown": p.get("max_drawdown", float("nan")),
            "n_periods": len(sub),
        })

    ls = long_short_nav(df, factor, ret_col, n_groups, date_col=date_col)
    ls_perf = perf_from_returns(ls["ret_long_short"].to_numpy(),
                                periods_per_year=periods_per_year) if len(ls) else {}

    # 空分位组的 mean_ret 是 NaN —— 只用有效组算单调性与 spread，否则 NaN 毒化结果
    pairs = [(g["q"], g["mean_ret"]) for g in groups
             if isinstance(g["mean_ret"], float) and not math.isnan(g["mean_ret"])]
    mono = _spearman([q for q, _ in pairs], [m for _, m in pairs]) if len(pairs) >= 2 \
        else float("nan")
    spread = pairs[-1][1] - pairs[0][1] if len(pairs) >= 2 else float("nan")

    return {
        "factor": factor,
        "ret_col": ret_col,
        "n_groups": n_groups,
        "groups": groups,
        "long_short": {k: ls_perf.get(k) for k in
                       ("total_return", "annual_return", "annual_vol", "sharpe",
                        "max_drawdown", "win_rate", "calmar")},
        "monotonicity": mono,
        "top_bottom_spread": spread,
    }


def _spearman(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return float("nan")
    ra = _ranks(a)
    rb = _ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=False))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return num / (da * db) if da > 0 and db > 0 else float("nan")


def _ranks(x: list[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r
