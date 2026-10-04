"""归因：行业 / 市值 / 流动性暴露分解。

回答两个问题：
1. 这个因子到底在赌什么？（暴露）
2. 收益是从哪来的？（贡献）

多空组合的行业暴露若不接近 0，说明因子收益其实是行业 beta，
中性化没做干净 —— 这类因子一旦行业轮动就会大幅回撤。
"""
from __future__ import annotations

import numpy as np
import polars as pl

__all__ = ["exposure", "group_exposure", "return_contribution", "attribution_summary",
           "pure_exposure", "portfolio_exposure", "exposure_views", "ExposureDivergence"]


def exposure(df: pl.DataFrame, factor: str, by: str = "industry_sw1",
             *, date_col: str = "trade_date", top: float = 0.2,
             bottom: float = 0.2) -> pl.DataFrame:
    """多头组 vs 空头组在 `by` 维度上的权重差（暴露）。

    按分位取前 top 与后 bottom 比例的持仓，比较两组的行业分布。
    """
    if by not in df.columns:
        return pl.DataFrame()

    # 分类未知的行必须剔除：既不能算进行业暴露（未知不是行业 beta），
    # 也会让下面的 sorted(set(dh) | set(dl)) 在 None 与 str 之间比较时直接抛错。
    d = df.drop_nulls(by)
    if not len(d):
        return pl.DataFrame()

    ranked = d.with_columns(
        rk=pl.col(factor).rank("ordinal").over(date_col) / pl.col(factor).count().over(date_col)
    )
    hi = ranked.filter(pl.col("rk") > 1 - top)
    lo = ranked.filter(pl.col("rk") <= bottom)

    def dist(part: pl.DataFrame) -> dict[str, float]:
        g = part.group_by(by).agg(pl.len().alias("n")).with_columns(
            w=pl.col("n") / pl.col("n").sum())
        return dict(zip(g[by].cast(pl.Utf8).to_list(), g["w"].to_list(), strict=False))

    dh, dl = dist(hi), dist(lo)
    keys = sorted(set(dh) | set(dl))
    return pl.DataFrame({
        by: keys,
        "weight_long": [dh.get(k, 0.0) for k in keys],
        "weight_short": [dl.get(k, 0.0) for k in keys],
    }).with_columns(exposure=pl.col("weight_long") - pl.col("weight_short")).sort(
        pl.col("exposure").abs(), descending=True)


def group_exposure(df: pl.DataFrame, factor: str, cols: list[str],
                   *, date_col: str = "trade_date",
                   n_groups: int = 5) -> pl.DataFrame:
    """每个因子分位组在若干连续变量上的均值（市值、换手率等）。

    看的是「第 10 组是不是全是小票」这类问题。
    """
    from lquant.factors.evaluate.quantile import add_quantile

    d = add_quantile(df, factor, n_groups, date_col=date_col)
    aggs = [pl.col(c).cast(pl.Float64, strict=False).mean().alias(c) for c in cols if c in df.columns]
    if not aggs:
        return pl.DataFrame()
    return d.group_by("q").agg([pl.len().alias("n"), *aggs]).sort("q")


def return_contribution(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                        by: str = "industry_sw1", *, date_col: str = "trade_date",
                        n_groups: int = 10) -> pl.DataFrame:
    """按维度分解收益贡献：组内权重 × 组内平均收益。"""
    from lquant.factors.evaluate.quantile import add_quantile

    if by not in df.columns:
        return pl.DataFrame()
    d = add_quantile(df, factor, n_groups, date_col=date_col).drop_nulls([ret_col, by])
    if not len(d):
        return pl.DataFrame()

    total = d[ret_col].mean()
    g = d.group_by(by).agg([
        pl.len().alias("n"),
        pl.col(ret_col).mean().alias("mean_ret"),
    ]).with_columns(
        weight=pl.col("n") / pl.col("n").sum(),
        total_ret=pl.lit(total),
    )
    return g.with_columns(
        contribution=pl.col("weight") * (pl.col("mean_ret") - pl.col("total_ret"))
    ).sort("contribution", descending=True)


def attribution_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                        *, date_col: str = "trade_date",
                        cat_col: str = "industry_sw1",
                        num_cols: list[str] | None = None) -> dict:
    """归因汇总：行业暴露 + 分组特征 + 收益贡献。"""
    num_cols = num_cols or [c for c in ("market_cap", "turnover_rate") if c in df.columns]
    exp = exposure(df, factor, by=cat_col, date_col=date_col)
    gross = float(exp["exposure"].abs().sum()) if len(exp) else float("nan")
    return {
        "factor": factor,
        "industry_exposure": exp,
        "gross_exposure": gross,            # 越接近 0 说明行业越中性
        "group_profile": group_exposure(df, factor, num_cols, date_col=date_col),
        "contribution": return_contribution(df, factor, ret_col, by=cat_col, date_col=date_col),
    }


# ---------------------------------------------------------------- 双视角暴露

def _style_cols(df: pl.DataFrame, covs: list[str] | None) -> list[str]:
    if covs:
        return [c for c in covs if c in df.columns]
    return [c for c in ("market_cap", "float_mv", "turnover_rate", "volatility",
                        "mom20", "bp", "ep") if c in df.columns]


def pure_exposure(df: pl.DataFrame, factor: str, covs: list[str] | None = None,
                  *, date_col: str = "trade_date") -> pl.DataFrame:
    """**纯暴露**：把因子本身当成一个组合（权重 ``w_i = f_i / Σ|f_i|``）看它载荷在什么上。

    与 :func:`portfolio_exposure` 的区别是这个模块存在的理由：

    - 纯暴露回答「**信号本身**在赌什么」—— 不做分箱、不做多空、不设持仓约束，
      所以它反映的是因子的**内在**风格倾向；
    - 组合暴露回答「**实际持仓**表现得像什么」—— 含分箱、权重方案、约束。

    两者背离时，说明「可交易的组合」与「信号」不是一回事：例如信号本身
    只轻微偏向小市值，但取 Top 10% 后组合变成极端小市值 —— 这种背离在
    只看组合暴露时完全不可见。

    权重用 ``Σ|f_i|`` 归一（而不是 ``Σf_i``）：因子可能有负值，
    用净和归一会让权重爆炸或符号翻转。
    """
    cols = _style_cols(df, covs)
    if not cols:
        return pl.DataFrame()
    d = df.drop_nulls([factor, *cols])
    if not len(d):
        return pl.DataFrame()

    def _per_day(g: pl.DataFrame) -> pl.DataFrame:
        f = g[factor].cast(pl.Float64, strict=False)
        denom = float(f.abs().sum())
        if denom <= 1e-12:
            return pl.DataFrame({date_col: [], **{c: [] for c in cols}})
        w = f / denom
        return pl.DataFrame({
            date_col: [g[date_col][0]],
            **{c: [float((g[c].cast(pl.Float64, strict=False) * w).sum())] for c in cols},
        })

    parts = [_per_day(g) for _, g in d.group_by(date_col, maintain_order=True)]
    parts = [p for p in parts if len(p)]
    if not parts:
        return pl.DataFrame()
    # 返回**逐期**暴露（不在这里做汇总）：汇总口径留给调用方，
    # 因为「均值」和「均值/t 值」在不同场景下的解读不同（见 exposure_views）。
    return pl.concat(parts).sort(date_col)


def portfolio_exposure(df: pl.DataFrame, factor: str, covs: list[str] | None = None,
                       *, date_col: str = "trade_date", n_groups: int = 10,
                       side: str = "long_short") -> pl.DataFrame:
    """**组合暴露**：实际可交易组合在风格因子上的暴露（逐期）。

    ``side``：``long_short`` = 第 N 组减第 1 组（等权）；``long`` = 只取第 N 组。
    """
    from lquant.factors.evaluate.quantile import add_quantile

    cols = _style_cols(df, covs)
    if not cols:
        return pl.DataFrame()
    d = add_quantile(df, factor, n_groups, date_col=date_col).drop_nulls(cols)

    def _per_day(g: pl.DataFrame) -> pl.DataFrame:
        hi = g.filter(pl.col("q") == n_groups)
        lo = g.filter(pl.col("q") == 1)
        row: dict = {date_col: g[date_col][0]}
        for c in cols:
            h = hi[c].cast(pl.Float64, strict=False)
            l = lo[c].cast(pl.Float64, strict=False)
            row[c] = float(h.mean() - l.mean()) if side == "long_short" else float(h.mean())
        return pl.DataFrame({k: [v] for k, v in row.items()})

    parts = [_per_day(g) for _, g in d.group_by(date_col, maintain_order=True)]
    parts = [p for p in parts if len(p)]
    return pl.concat(parts).sort(date_col) if parts else pl.DataFrame()


def _style_scale(df: pl.DataFrame, cols: list[str], *, date_col: str) -> dict[str, float]:
    """各风格变量的**截面标准差**（逐日均值）—— 背离的天然量纲。

    用它而不是「纯暴露的时间序列标准差」：后者会随耦合强度一起变大，
    导致「耦合越强、标准化背离越小」的反直觉结果（实测 coupling 从 0.6
    升到 1.2，gap_z 反而从 0.98 掉到 0.09）。截面标准差是稳定的外生尺度，
    含义也直白：「组合暴露比信号暴露多出几个截面标准差」。
    """
    out: dict[str, float] = {}
    for c in cols:
        if c not in df.columns:
            continue
        g = (df.drop_nulls(c).group_by(date_col)
             .agg(pl.col(c).cast(pl.Float64, strict=False).std().alias("s")))
        vals = [float(x) for x in g["s"].to_list() if x is not None and np.isfinite(x)]
        out[c] = float(np.mean(vals)) if vals else 0.0
    return out


def exposure_views(df: pl.DataFrame, factor: str, covs: list[str] | None = None,
                   *, date_col: str = "trade_date", n_groups: int = 10,
                   side: str = "long_short") -> dict:
    """双视角对照：返回 ``{pure, portfolio, compare, divergences, ...}``。

    ``compare`` 逐风格列给出背离：

    - ``gap = portfolio_mean - pure_mean``（原始量纲）；
    - ``gap_z = gap / 该风格变量的截面标准差``（标准化，跨列可比）。

    ``|gap_z| ≥ 1`` 记为一个 ``divergence``：组合在该风格上的暴露比信号本身
    多出一个截面标准差 —— 「信号与组合不是一回事」的量化门槛。
    """
    pure = pure_exposure(df, factor, covs, date_col=date_col)
    port = portfolio_exposure(df, factor, covs, date_col=date_col,
                              n_groups=n_groups, side=side)
    cols = [c for c in (covs or [c for c in pure.columns if c != date_col])
            if c in pure.columns and c in port.columns]
    scale = _style_scale(df, cols, date_col=date_col)
    rows: list[dict] = []
    for c in cols:
        p = pure[c].cast(pl.Float64, strict=False).to_numpy()
        q = port[c].cast(pl.Float64, strict=False).to_numpy()
        if not len(p) or not len(q):
            continue
        pm, qm = float(np.nanmean(p)), float(np.nanmean(q))
        sc = scale.get(c, 0.0)
        rows.append({
            "cov": c,
            "pure_mean": pm,
            "portfolio_mean": qm,
            "pure_std": float(np.nanstd(p, ddof=1)) if len(p) > 1 else 0.0,
            "style_scale": sc,
            "gap": qm - pm,
            "gap_z": (qm - pm) / sc if sc > 1e-12 else None,
            "n_days": int(min(len(p), len(q))),
        })
    divergences = [r for r in rows if r["gap_z"] is not None and abs(r["gap_z"]) >= 1.0]
    return {"pure": pure, "portfolio": port, "compare": rows,
            "divergences": divergences, "side": side, "n_groups": n_groups}


class ExposureDivergence(dict):
    """双视角背离的结论包装（``dict`` 子类，便于直接进 API 响应）。"""

    @property
    def has_divergence(self) -> bool:
        return bool(self.get("divergences"))
