"""隔夜 / 日内收益切分（借 AlphaPurify ``overnight=on/off/only`` 的语义）。

## 为什么必须切开看

A 股「隔夜跳空」与「日内连续竞价」是两套完全不同的成交机制：

- **隔夜**（昨收 → 今开）：由集合竞价 + 消息面驱动。信号若主要靠隔夜跳空赚钱，
  实盘上你**必须在开盘前挂单**，滑点与排队风险完全不同，而且很多隔夜跳空
  在开盘瞬间就被吃掉；
- **日内**（今开 → 今收）：连续竞价可得，回测里 ``next_open`` 成交的收益
  主要来自这里。

一个 IC 0.05 的因子，若超额全在隔夜段，而你的执行是开盘后成交 ——
那就是「回测赚钱、实盘不赚钱」的经典成因。

## 恒等式（本模块的正确性锚点）

对任意 h，两段收益必须能**精确复利还原**总收益：

    (1 + overnight_h) × (1 + intraday_h) = 1 + total_h

因为 ``overnight`` 的分子分母与 ``intraday`` 首尾相接（``close_t`` 被约掉）。
测试里逐点断言这个恒等式 —— 它一破就说明对齐写错了。

注意 ``total_h`` 在本模块里由 ``close_{t+h}/close_t`` **独立算出**，
不是两段相乘的结果 —— 这样恒等式才是一次真正的交叉验证，
而不是同义反复。
"""
from __future__ import annotations

import polars as pl

__all__ = ["session_returns", "session_ic", "session_ic_summary", "SESSION_LABELS"]

#: 会话标签
SESSION_LABELS = ("total", "overnight", "intraday")


def _safe_log(e: pl.Expr) -> pl.Expr:
    """log，非正值归 null（停牌 0 价会让 log 变 -inf，进而毒化整段求和）。"""
    return pl.when(e > 0).then(e.log()).otherwise(None)


def _fwd_log_sum(col: str, h: int, by: str) -> pl.Expr:
    """未来 h 项的对数和。

    ``rolling_sum(h)`` 在行 i 取「含 i 的前 h 项」，``shift(-h)`` 把它搬到
    i+h → 于是行 i 拿到 i+1..i+h 的和。这样避免 ``rolling_map`` 的 Python
    回调（逐窗调用，长序列上慢几个数量级）。
    """
    return (pl.col(col).rolling_sum(h, min_periods=h).shift(-h).over(by))


def session_returns(
    df: pl.DataFrame,
    *,
    periods: list[int] | None = None,
    open_col: str = "open",
    close_col: str = "close",
    by: str = "symbol",
    date_col: str = "trade_date",
    suffix: str = "fwd_ret",
) -> pl.DataFrame:
    """追加三段前瞻收益列（T 日收盘出信号，T+1 起持有 h 日）::

        fwd_ret_{h}              = close_{t+h} / close_t − 1        （总）
        fwd_ret_overnight_{h}    = Π_{i=t+1..t+h} open_i/close_{i-1} − 1
        fwd_ret_intraday_{h}     = Π_{i=t+1..t+h} close_i/open_i     − 1

    三段满足 ``(1+隔夜)(1+日内) = 1+总``（见模块 docstring）。
    """
    hs = list(periods) if periods is not None else [1, 5, 10, 20]
    need = {by, date_col, open_col, close_col}
    miss = need - set(df.columns)
    if miss:
        raise KeyError(f"缺少列: {sorted(miss)}")
    if not hs:
        raise ValueError("periods 不能为空")
    if any(h < 1 for h in hs):
        raise ValueError(f"periods 必须为正整数: {hs}")

    d = df.sort([by, date_col])
    o = pl.col(open_col).cast(pl.Float64, strict=False)
    c = pl.col(close_col).cast(pl.Float64, strict=False)
    # 逐日两段的对数收益：隔夜 = log(open/prev_close)，日内 = log(close/open)
    d = d.with_columns(
        _safe_log(o / c.shift(1).over(by)).alias("__lo"),
        _safe_log(c / o).alias("__ld"),
    )
    exprs: list[pl.Expr] = []
    for h in hs:
        exprs.append((_fwd_log_sum("__lo", h, by).exp() - 1.0)
                     .alias(f"{suffix}_overnight_{h}"))
        exprs.append((_fwd_log_sum("__ld", h, by).exp() - 1.0)
                     .alias(f"{suffix}_intraday_{h}"))
        # 总收益独立算（不用两段相乘）—— 恒等式才是真正的交叉验证
        exprs.append(((c.shift(-h).over(by) / c) - 1.0).alias(f"{suffix}_{h}"))
    return d.with_columns(exprs).drop(["__lo", "__ld"])


def session_ic(
    df: pl.DataFrame,
    factor: str,
    *,
    periods: list[int] | None = None,
    date_col: str = "trade_date",
    min_obs: int = 5,
    **kw,
) -> pl.DataFrame:
    """逐 (horizon, session) 的 IC / RankIC 汇总（长表）。

    三段用**同一份样本**（都要求当日因子与三段收益均非空），
    这样三段 IC 之差才可比 —— 样本不同的话差异里混着样本选择偏差。
    """
    hs = list(periods) if periods is not None else [1, 5, 10, 20]
    d = session_returns(df, periods=hs, date_col=date_col, **kw)
    if factor not in d.columns:
        raise KeyError(f"缺少因子列 {factor}")

    cols = {label: [f"fwd_ret_{label}_{h}" if label != "total" else f"fwd_ret_{h}"
                    for h in hs] for label in SESSION_LABELS}
    flat = [c for cs in cols.values() for c in cs]
    sel = d.select([date_col, factor, *flat]).with_columns(
        pl.col(factor).cast(pl.Float64, strict=False))
    for c in flat:
        sel = sel.with_columns(pl.col(c).cast(pl.Float64, strict=False))
    sel = sel.drop_nulls().filter(
        pl.col(factor).is_finite()
        & pl.all_horizontal([pl.col(c).is_finite() for c in flat]))

    aggs: list[pl.Expr] = [pl.len().alias("n")]
    for i, h in enumerate(hs):
        for label in SESSION_LABELS:
            c = cols[label][i]
            aggs.append(pl.corr(factor, c, method="pearson").alias(f"ic_{label}_{h}"))
            aggs.append(pl.corr(factor, c, method="spearman").alias(f"ric_{label}_{h}"))
    daily = sel.group_by(date_col).agg(aggs).filter(pl.col("n") >= min_obs).sort(date_col)

    from lquant.factors.evaluate.ic import _summarize

    rows: list[dict] = []
    for h in hs:
        for label in SESSION_LABELS:
            ic = _summarize(daily[f"ic_{label}_{h}"])
            ric = _summarize(daily[f"ric_{label}_{h}"])
            rows.append({
                "horizon": h, "session": label,
                "ic_mean": ic["mean"], "ic_ir": ic["ir"], "ic_t": ic["t_stat"],
                "rank_ic_mean": ric["mean"], "rank_ic_ir": ric["ir"],
                "positive_rate": ic["positive_rate"], "n_days": ic["n_days"],
            })
    return pl.DataFrame(rows)


def session_ic_summary(
    df: pl.DataFrame,
    factor: str,
    *,
    horizon: int = 1,
    date_col: str = "trade_date",
    min_obs: int = 5,
    **kw,
) -> dict:
    """单 horizon 的「信号靠哪一段赚钱」结论。

    返回 ``{horizon, total, overnight, intraday, dominant, overnight_share}``：

    - ``dominant``：**隔夜与日内**之间 |IC 均值| 更大的那一段。
      不参与和 ``total`` 比较 —— 总收益 = 两段复利，其 IC 天然接近两段之和，
      必然最大，把 total 放进来比就没有信息了；
    - ``overnight_share``：隔夜 |IC| 占（隔夜+日内）之和的比例（0~1）。
      接近 1 表示信号几乎全靠跳空 —— 执行时点必须前移到开盘前，
      否则回测里的收益拿不到。
    """
    tbl = session_ic(df, factor, periods=[horizon], date_col=date_col,
                     min_obs=min_obs, **kw)
    if not len(tbl):
        return {"horizon": horizon, "dominant": None, "overnight_share": None}
    by = {r["session"]: r for r in tbl.iter_rows(named=True)}
    # 空样本：session_ic 仍会返回 3 行（每段一行），但 n_days=0 —— 必须显式判，
    # 不能用 len(tbl)==0（那样永远为假，空样本会被当成有结论）。
    if not by["total"].get("n_days"):
        return {"horizon": horizon, "total": by.get("total"),
                "overnight": by.get("overnight"), "intraday": by.get("intraday"),
                "dominant": None, "overnight_share": None}
    ovn = abs(by["overnight"]["ic_mean"] or 0.0)
    idy = abs(by["intraday"]["ic_mean"] or 0.0)
    denom = ovn + idy
    dominant = max(("overnight", "intraday"),
                   key=lambda s: abs(by[s]["ic_mean"] or 0.0))
    return {
        "horizon": horizon,
        "total": by["total"], "overnight": by["overnight"],
        "intraday": by["intraday"],
        "dominant": dominant,
        "overnight_share": (ovn / denom) if denom > 1e-12 else None,
    }
