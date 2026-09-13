"""事件式分层收益（average cumulative return by quantile）。

口径移植自 alphalens / ferric-alpha 的
`average_cumulative_return_by_quantile`，也就是因子研报里那张"V 形 /
喇叭形"图：

以每个交易日为**事件日**，看每只股票从事件日往前 `before` 天、往后
`after` 天的累计收益，再按事件日的因子分位组取平均。

它回答的问题和分层回测不同：
- 分层回测问「持有高分位组，之后赚多少」（只看向前持有期）
- 事件式收益问「资金在围绕什么信号进出」——分位组曲线如果在事件日
  之前就分岔，说明因子在捕捉**已经发生**的趋势（滞后/追涨）；
  如果事件日之后才分岔，才是真正的**预测性**信号。

横轴口径（容易读反，务必留意）：
- k > 0：事件日 → 事件日 + k 的累计收益（拿住能赚多少）
- k < 0：事件日 + k → 事件日 的累计收益（这只票"怎么走到这里"）
两边都锚定在事件日的价格上，所以曲线在 k=0 处必然过 0。

图上 group 曲线在 0 之前收敛、0 之后发散 = 干净的预测因子；
0 之前就发散 = 因子在事后描述（典型的动量类因子），换手成本会吃掉大部分收益。
"事前发散 / 事后发散" 的比值见 event_study_summary 的 look_ahead_ratio。

实现是纯 polars 的 shift-over，不做逐日 python 循环。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.quantile import add_quantile

__all__ = ["event_study", "event_study_summary"]

_MAX_WINDOW = 250  # before/after 上限，防止误传入导致 250+ 次 shift


def _rel_cols(before: int, after: int) -> list[str]:
    return [f"r{k}" for k in range(-before, after + 1)]


def event_study(
    df: pl.DataFrame,
    factor: str,
    price_col: str = "close",
    *,
    n_groups: int = 10,
    before: int = 10,
    after: int = 15,
    demeaned: bool = True,
    date_col: str = "trade_date",
    symbol_col: str = "symbol",
) -> pl.DataFrame:
    """事件式分层累计收益曲线。

    Parameters
    ----------
    df : 含 [date_col, symbol_col, price_col, factor] 的面板数据
    price_col : 用于计算累计收益的价格列
    before / after : 事件日前后各看多少个交易日
    demeaned : 是否先减去同日全市场截面均值（默认 True，与 alphalens 一致）。
        不 demean 时曲线的整体水平会被市场 beta 抬高，看不出分位组之间的差异。

    Returns
    -------
    polars.DataFrame
        列 `rel_period`（相对交易日，0 = 事件日）+ `q1..qN`（各组平均累计收益）
        + `spread`（最高组 − 最低组）。
    """
    if before < 0 or after < 0:
        raise ValueError("before / after 必须非负")
    if before > _MAX_WINDOW or after > _MAX_WINDOW:
        raise ValueError(f"before / after 不得超过 {_MAX_WINDOW}")
    need = [date_col, symbol_col, price_col, factor]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise KeyError(f"缺少列 {miss}")

    d = (
        df.select(need)
        .drop_nulls([price_col, factor])
        .sort([symbol_col, date_col])
    )
    d = add_quantile(d, factor, n_groups, date_col=date_col)

    rel = _rel_cols(before, after)
    # 第一趟：算各相对日的累计收益。k ≥ 0 → 事件日到 t+k 的收益（拿住能赚多少）；
    # k < 0 → 截止到事件日的那段窗口收益 p(t)/p(t+k)-1（这只票"怎么走到这里"）。
    # 左半段必须用「结束于事件日」的口径，否则动量因子会显示成事件前在反转，
    # 与"因子在描述既成趋势"的读图直觉正好相反。
    # 注意：窗口表达式不能嵌套不同 key 的 over —— demean 必须放到第二趟。
    d = d.with_columns([
        (
            (pl.col(price_col).shift(-k).over(symbol_col) / pl.col(price_col))
            if k >= 0 else
            (pl.col(price_col) / pl.col(price_col).shift(-k).over(symbol_col))
        ).sub(1.0).alias(f"r{k}")
        for k in range(-before, after + 1)
    ])
    if demeaned:
        d = d.with_columns([
            (pl.col(f"r{k}") - pl.col(f"r{k}").mean().over(date_col)).alias(f"r{k}")
            for k in range(-before, after + 1)
        ])

    long = (
        d.unpivot(
            index=[date_col, symbol_col, "q"],
            on=rel,
            variable_name="rel_period",
            value_name="cum_ret",
        )
        .with_columns(pl.col("rel_period").str.slice(1).cast(pl.Int32))
        .drop_nulls("cum_ret")
    )
    agg = (
        long.group_by(["q", "rel_period"])
        .agg(pl.col("cum_ret").mean())
        .drop_nulls("cum_ret")
    )
    if not len(agg):
        return pl.DataFrame(schema={"rel_period": pl.Int32})

    wide = agg.pivot(on="q", index="rel_period", values="cum_ret").sort("rel_period")
    # 补全缺失的分位组列（样本太小时某些组可能不存在），再统一命名
    for q in range(1, n_groups + 1):
        if str(q) not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(str(q)))
    wide = wide.select(
        [pl.col("rel_period")] + [pl.col(str(q)).alias(f"q{q}") for q in range(1, n_groups + 1)]
    )
    return wide.with_columns(
        (pl.col(f"q{n_groups}") - pl.col("q1")).alias("spread")
    )


def event_study_summary(
    df: pl.DataFrame,
    factor: str,
    price_col: str = "close",
    *,
    n_groups: int = 10,
    before: int = 10,
    after: int = 15,
    demeaned: bool = True,
    date_col: str = "trade_date",
    symbol_col: str = "symbol",
) -> dict:
    """曲线 + 元信息，便于直接塞进 API / 报告。"""
    curve = event_study(
        df, factor, price_col, n_groups=n_groups, before=before, after=after,
        demeaned=demeaned, date_col=date_col, symbol_col=symbol_col,
    )
    # 事件数在清洗后的帧上数：原始帧里 price/factor 为 NULL 的行不参与
    # 事件构造，用原始帧数会虚报支撑曲线的事件数
    clean = df.drop_nulls([price_col, factor])
    n_events = clean[date_col].n_unique() if date_col in clean.columns else 0
    pre = curve.filter(pl.col("rel_period") < 0)
    post = curve.filter(pl.col("rel_period") > 0)
    # 事前发散度 vs 事后发散度：事前就张开说明因子在描述既有趋势（滞后）
    pre_spread = float(pre["spread"].abs().mean()) if len(pre) else float("nan")
    post_spread = float(post["spread"].abs().mean()) if len(post) else float("nan")
    return {
        "factor": factor,
        "n_groups": n_groups,
        "before": before,
        "after": after,
        "demeaned": demeaned,
        "n_events": n_events,
        "pre_spread": pre_spread,
        "post_spread": post_spread,
        "look_ahead_ratio": pre_spread / post_spread if post_spread and post_spread > 0
        else float("nan"),
        "rel_periods": curve["rel_period"].to_list() if len(curve) else [],
        "curve": {f"q{q}": curve[f"q{q}"].to_list() for q in range(1, n_groups + 1)}
        if len(curve) else {},
        "spread": curve["spread"].to_list() if len(curve) else [],
    }
