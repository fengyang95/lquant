"""IC / RankIC / IR 与显著性检验。

IC = 截面因子值与前瞻收益的相关系数。它是因子评价的第一指标：
- |IC| > 0.02 就算有效（A 股日频，别被 0.05 的回测骗了）
- IR = IC均值 / IC标准差，衡量稳定性，> 0.3 可用，> 0.5 很优秀
- t 检验判断 IC 是否显著不为 0：t = IR × √N，|t| > 2 才算数

只有 IC 均值没有 IR 和 t 值，等于没看 ——
一个均值 0.03 但标准差 0.15 的因子，实盘上是没法用的。
"""

from __future__ import annotations

import math

import polars as pl

# 零方差判定阈值：polars 常数序列的 std 是 ~7e-18（非 0），
# 凡 std < STD_EPS 一律按零方差处理（t/IR 无定义，不能进显著性门槛）
STD_EPS = 1e-9

__all__ = ["ic_series", "ic_summary", "ic_by_year", "ic_by_horizon",
           "newey_west_tstat", "ic_autocorr"]


def ic_series(
    df: pl.DataFrame,
    factor: str,
    ret_col: str = "fwd_ret_1",
    *,
    date_col: str = "trade_date",
    min_obs: int = 5,
) -> pl.DataFrame:
    """逐日截面 IC / RankIC。

    每天样本数少于 min_obs 时该日不计入（相关性在极小样本下没有意义）。
    """
    need = [factor, ret_col, date_col]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise KeyError(f"缺少列 {miss}")

    d = (
        df.select([date_col, factor, ret_col])
        .with_columns(
            [
                pl.col(factor).cast(pl.Float64, strict=False),
                pl.col(ret_col).cast(pl.Float64, strict=False),
            ]
        )
        .drop_nulls()
        # NaN 不是 null：drop_nulls 拦不住，单个 NaN 会让当日 corr 变 NaN
        # （进而毒化整个均值/IR），且被 pl.len() 计入 min_obs 门槛
        .filter(pl.col(factor).is_finite() & pl.col(ret_col).is_finite())
    )

    out = (
        d.group_by(date_col)
        .agg(
            [
                pl.len().alias("n"),
                pl.corr(factor, ret_col, method="pearson").alias("ic"),
                pl.corr(factor, ret_col, method="spearman").alias("rank_ic"),
            ]
        )
        .filter(pl.col("n") >= min_obs)
        .sort(date_col)
    )
    return out


def _t_stat(mean: float, std: float, n: int) -> float:
    """t = mean / (std/√n)。std 为 0/近零或 n 过小时返回 nan。

    近零判定用 STD_EPS：polars 对常数序列返回的 std 是 ~7e-18 而非 0，
    不挡住的话近常数因子会算出 ~1e16 的 t 硬闯显著性门槛。
    """
    if n < 2 or std <= STD_EPS or not math.isfinite(std):
        return float("nan")
    return mean / (std / math.sqrt(n))


def ic_autocorr(series: pl.Series, lag: int = 1) -> float:
    """IC 序列的 lag 阶自相关：高自相关 = 信号可预测且稳定，近零/负 = 噪声主导。

    与 ICIR 互补：ICIR 说「平均强度/波动」，自相关说「这种强度能不能延续」。
    一个 ICIR 0.4 但自相关 −0.2 的因子，很可能只是在一个个独立的行情片段上碰运气。

    手算 Pearson 而不是调库：polars 各版本对 ``Series.autocorr`` 的支持并不一致
    （缺失时静默降级会把这一项变成 NaN，看着像「数据不够」）。
    """
    s = series.drop_nulls() if isinstance(series, pl.Series) else pl.Series(series).drop_nulls()
    n = len(s)
    if lag < 1 or n <= lag + 1:
        return float("nan")
    a = s.cast(pl.Float64, strict=False).to_numpy()
    x, y = a[lag:], a[:-lag]
    xm, ym = x - x.mean(), y - y.mean()
    den = math.sqrt(float((xm**2).sum()) * float((ym**2).sum()))
    if den <= 0:
        return float("nan")
    v = float((xm * ym).sum()) / den
    return v if math.isfinite(v) else float("nan")


def newey_west_tstat(x, lags: int | None = None) -> float:
    """NW 一致 t 值：日度 IC 强自相关下，朴素 t = IR·√N 会高估显著性 3~5 倍。"""
    s = pl.Series(x).drop_nulls() if not isinstance(x, pl.Series) else x.drop_nulls()
    n = len(s)
    if n < 2:
        return float("nan")
    lags = lags or int(4 * (n / 100) ** (2 / 9)) or 1
    a = s.to_numpy() - s.mean()
    s0 = float((a**2).sum()) / n
    if s0 <= STD_EPS * STD_EPS:  # 近常数序列（std < 1e-9）：t 无定义，不能放行
        return float("nan")
    lrv = s0
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)  # Bartlett 核
        gamma_l = float((a[lag:] * a[:-lag]).sum()) / n
        lrv += 2.0 * w * gamma_l
    if lrv <= 0:
        return float("nan")
    se = math.sqrt(lrv / n)
    return float(s.mean()) / se


def _summarize(series: pl.Series, annualize: bool = True, *, nw_lags: int | None = None) -> dict:
    s = series.drop_nulls()
    n = len(s)
    if n == 0:
        return {
            "mean": float("nan"),
            "std": float("nan"),
            "ir": float("nan"),
            "t_stat": float("nan"),
            "t_stat_nw": float("nan"),
            "positive_rate": float("nan"),
            "skew": float("nan"),
            "kurtosis": float("nan"),
            "ic_gt_002_rate": float("nan"),
            "ic_autocorr": float("nan"),
            "n_days": 0,
        }
    mean = float(s.mean())
    # std=0 是合法值（IC 恒定）——不能和「样本不足算不出 std」一起折成 NaN，
    # 否则下游分不清「完美稳定」和「没有数据」（评级会把最强因子判成 moderate）。
    std_raw = s.std()
    std = (float(std_raw) if std_raw is not None and math.isfinite(float(std_raw))
           else float("nan"))
    # std <= STD_EPS 视为零方差（含 polars 常数序列的 ~7e-18 伪影）：
    # IR 无定义 → nan，评级层再按「完美稳定」特殊处理
    ir = mean / std if std and std > STD_EPS else float("nan")
    pos = float((s > 0).sum() / n)
    # 阈值胜率：与均值同向、且幅度过 0.02 有效线的占比 —— 比单纯胜率更挑剔
    thr = 0.02 if mean >= 0 else -0.02
    sig = float((s > thr).sum() / n) if mean >= 0 else float((s < thr).sum() / n)
    return {
        "mean": mean,
        "std": std,
        "ir": ir,
        "ir_annual": ir * math.sqrt(252) if annualize and math.isfinite(ir) else float("nan"),
        "t_stat": _t_stat(mean, std, n),
        "t_stat_nw": newey_west_tstat(s, lags=nw_lags),
        "positive_rate": pos,
        "skew": float(s.skew()) if n > 2 else float("nan"),
        "kurtosis": float(s.kurtosis()) if n > 3 else float("nan"),
        "ic_gt_002_rate": sig,
        "ic_autocorr": ic_autocorr(s, lag=1),
        "n_days": n,
    }


def ic_summary(
    df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *, method: str = "both", **kw
) -> dict:
    """汇总：IC / RankIC 的均值、标准差、IR、t 值、正比例。

    method="pearson" 时结果只含普通 IC，"spearman" 只含 RankIC，
    "both"（默认）都算。注意：ic_series 始终同时计算两种相关，
    method 只影响输出哪些汇总，不减少计算量。
    """
    if method not in ("pearson", "spearman", "both"):
        raise ValueError(f"method 必须是 pearson/spearman/both，得到: {method}")
    s = ic_series(df, factor, ret_col, **kw)
    out = {
        "factor": factor,
        "ret_col": ret_col,
        "method": method,
        "ic": _summarize(s["ic"]) if len(s) else {},
    }
    if method in ("spearman", "both"):
        out["rank_ic"] = _summarize(s["rank_ic"]) if len(s) else {}
    return out


def ic_by_year(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", **kw) -> pl.DataFrame:
    """分年度 IC。因子失效往往不是慢慢变差，而是某一年突然反转。"""
    s = ic_series(df, factor, ret_col, **kw)
    if not len(s):
        return s
    return (
        s.with_columns(pl.col(kw.get("date_col", "trade_date")).dt.year().alias("year"))
        .group_by("year")
        .agg(
            [
                pl.len().alias("n_days"),
                pl.col("ic").mean().alias("ic_mean"),
                pl.col("ic").std().alias("ic_std"),
                (pl.col("ic").mean() / pl.col("ic").std()).alias("ir"),
                (pl.col("ic") > 0).mean().alias("positive_rate"),
                pl.col("rank_ic").mean().alias("rank_ic_mean"),
            ]
        )
        .sort("year")
    )


def ic_by_horizon(
    df: pl.DataFrame,
    factor: str,
    horizons: list[int] | None = None,
    *,
    ret_suffix: str = "fwd_ret",
    date_col: str = "trade_date",
    min_obs: int = 5,
    price_col: str | None = None,
    nw_lags: int | None = None,
) -> pl.DataFrame:
    """一次扫描算完多个持有期的 IC（借 AlphaPurify 对 (1,5,10) 并行统计的做法）。

    **为什么不是循环调 ``ic_summary``**：每个 horizon 都跑一遍 ``ic_series``
    意味着 N 次全表扫描 + N 次 group_by。这里把全部 horizon 的相关性放进
    **同一个 group_by**，扫描次数与 horizon 数无关。

    ``horizons`` 缺省 ``[1, 5, 10, 20]``。收益列 ``fwd_ret_{h}`` 不存在时，
    若给了 ``price_col`` 就现算（复用 ``returns.forward_return``）。

    返回长表，每行一个 horizon：``horizon, ic_mean, ic_std, ic_ir, ic_t,
    rank_ic_mean, rank_ic_ir, rank_ic_t, positive_rate, n_days``。
    """
    # 显式 [] 与「未指定」必须区分：前者是用户明确说「不要任何 horizon」，
    # 用 `if horizons` 会让 [] 静默变成默认的 [1,5,10,20]（实测踩过）。
    hs = list(horizons) if horizons is not None else [1, 5, 10, 20]
    if not hs:
        raise ValueError("horizons 不能为空")
    if len(set(hs)) != len(hs):
        raise ValueError(f"horizons 有重复: {hs}")
    if any(h < 1 for h in hs):
        raise ValueError(f"horizons 必须为正整数: {hs}")
    if factor not in df.columns:
        raise KeyError(f"缺少因子列 {factor}")

    d = df
    cols = [f"{ret_suffix}_{h}" for h in hs]
    missing = [c for c in cols if c not in d.columns]
    if missing:
        if price_col is None:
            raise KeyError(f"缺少收益列 {missing}（或传 price_col 让本函数现算）")
        from lquant.factors.evaluate.returns import forward_return

        d = forward_return(d, price_col, periods=hs, date_col=date_col)
        missing = [c for c in cols if c not in d.columns]
        if missing:
            raise KeyError(f"现算后仍缺少收益列 {missing}")

    # 每个 horizon **各自**用自己的有效样本：只要求因子有限，收益按列置 null
    # 让 pl.corr 逐对跳过。这与逐 horizon 调 ic_summary 的样本完全一致 ——
    # 「合并扫描」只该省扫描，不该改变口径（早期版本用「全部 horizon 都有限」
    # 的公共样本，导致 h=1 的 IC 与 ic_summary 差一倍）。
    sel = d.select([date_col, factor, *cols]).with_columns(
        pl.col(factor).cast(pl.Float64, strict=False))
    sel = sel.filter(pl.col(factor).is_finite())
    for c in cols:
        sel = sel.with_columns(
            pl.when(pl.col(c).cast(pl.Float64, strict=False).is_finite())
            .then(pl.col(c).cast(pl.Float64, strict=False))
            .otherwise(None).alias(c))

    aggs: list[pl.Expr] = []
    for h in hs:
        c = f"{ret_suffix}_{h}"
        # 逐 horizon 的有效样本数（min_obs 必须按 horizon 判）
        aggs.append(pl.col(c).is_not_null().sum().alias(f"n_{h}"))
        aggs.append(pl.corr(factor, c, method="pearson").alias(f"ic_{h}"))
        aggs.append(pl.corr(factor, c, method="spearman").alias(f"ric_{h}"))
    daily = sel.group_by(date_col).agg(aggs).sort(date_col)

    rows: list[dict] = []
    for h in hs:
        sub = daily.filter((pl.col(f"n_{h}") >= min_obs) & pl.col(f"ic_{h}").is_not_null())
        ic = _summarize(sub[f"ic_{h}"], nw_lags=nw_lags)
        ric = _summarize(sub[f"ric_{h}"], nw_lags=nw_lags)
        rows.append({
            "horizon": h,
            "ic_mean": ic["mean"], "ic_std": ic["std"], "ic_ir": ic["ir"],
            "ic_t": ic["t_stat"], "ic_t_nw": ic["t_stat_nw"],
            "rank_ic_mean": ric["mean"], "rank_ic_ir": ric["ir"],
            "rank_ic_t": ric["t_stat"],
            "positive_rate": ic["positive_rate"],
            "ic_autocorr": ic["ic_autocorr"],
            "n_days": ic["n_days"],
        })
    return pl.DataFrame(rows)
