"""分层回测：十分位分组，看单调性和多空收益。

分层比 IC 更直观：IC 是一个数，分层是一张图。
一个因子哪怕 IC 一般，只要分组收益**单调**，就说明它是稳定的线性信号；
反过来，IC 高但分组不单调（只有第 10 组特别高），往往是几只极端股撑起来的，
实盘一上就被打回原形。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import polars as pl

from lquant.backtest.metrics import perf_from_returns
from lquant.core.errors import DataQualityError

__all__ = ["DropAccounting", "account_samples", "add_quantile", "group_returns",
           "long_short_nav", "pivot_group_returns", "quantile_nav",
           "quantile_summary"]


def _group_key(date_col: str, by_group: str | None) -> list[str]:
    """分箱的截面键：按日，或（按日 × 组）做组内分箱。"""
    return [date_col] if not by_group else [date_col, by_group]


def add_quantile(df: pl.DataFrame, factor: str, n_groups: int = 10,
                 *, date_col: str = "trade_date", out: str = "q",
                 zero_aware: bool = False, by_group: str | None = None) -> pl.DataFrame:
    """按日截面打分位，1 = 因子值最低，n_groups = 最高。

    用 rank 而不是 qcut：qcut 遇到大量重复值（比如停牌、新股一字）会报错
    或产生不均匀分组，rank 分位则稳定得多。

    ``zero_aware``（默认关）：**以零为界**分组 —— 负号侧排在 1..k，正号侧
    （含恰好为 0 的值）排在 k+1..n_groups。反转 / 情绪这类**围绕零构造**的
    因子，正负号本身才是信号，纯按排名分组会把「全市场普涨时的一堆小负数」
    也塞进空头组，多空两端都不是真正的极端。

    ``by_group``（默认 None）：**组内分箱** —— 在 ``(交易日, by_group)`` 内
    各自打分位。行业中性化没做时，它能直接去掉「整个行业同涨同跌」对分组
    收益的污染。

    两者**不能同时用**（一个要按组、一个要跨组看符号），同时给会报错。
    """
    if zero_aware and by_group:
        raise ValueError("zero_aware 与 by_group 互斥：前者跨组看正负号，后者按组各分一箱")
    if by_group is not None and by_group not in df.columns:
        raise KeyError(f"by_group 列不存在: {by_group}")
    # n_groups=1 是既有用法（不做分箱、只看全体收益），不能拦；但零感知分箱
    # 至少要两组才有「两侧」可言。
    if n_groups < 1:
        raise ValueError(f"n_groups 至少为 1，收到 {n_groups}")
    if zero_aware and n_groups < 2:
        raise ValueError(f"zero_aware 需要至少 2 组，收到 {n_groups}")

    # NaN 不是 null：polars rank 会把 NaN 排在所有有限值之后（实测 1.44），
    # 不挡的话 NaN 行全进最高分位组且 count() 分母被计入，整体分组挤偏
    fin = pl.col(factor).is_finite()
    key = _group_key(date_col, by_group)

    if not zero_aware:
        cnt = fin.sum().over(key)
        q = (pl.when(fin)
             .then(pl.col(factor).rank("ordinal").over(key) * n_groups / cnt)
             .otherwise(None)
             .ceil().clip(1, n_groups))
        return df.with_columns(q.cast(pl.Int32).alias(out))

    # ---- zero_aware：两侧各自成箱 ----
    # 整段面板只有一侧有值时，zero_aware 是不可满足的：负号侧全空 → 空头组
    # 恒为 null，多空差会退化成「多头组自己的收益」这种看着有、其实错的数字。
    # 这里直接报错，而不是让它悄悄算出一个漂亮的多空收益。
    if zero_aware:
        f = df[factor]
        has_neg = bool((f < 0).any())
        has_pos = bool((f >= 0).any())
        if not (has_neg and has_pos):
            raise ValueError(
                "zero_aware=True 要求因子以零为中心，但本次面板"
                f"{'没有负值' if not has_neg else '没有非负值'} —— "
                "请改用 zero_aware=False，或先确认因子构造口径")
    k_neg = n_groups // 2
    k_pos = n_groups - k_neg
    neg = pl.col(factor) < 0
    val_neg = pl.when(fin & neg).then(pl.col(factor)).otherwise(None)
    val_pos = pl.when(fin & ~neg).then(pl.col(factor)).otherwise(None)
    cnt_neg = val_neg.is_not_null().sum().over(key)
    cnt_pos = val_pos.is_not_null().sum().over(key)
    # 某一侧整列为空（比如全市场都是正数）时该侧给 null，由下面回退到普通分箱
    q_neg = (val_neg.rank("ordinal").over(key) * k_neg / cnt_neg).ceil().clip(1, k_neg)
    q_pos = (k_neg + val_pos.rank("ordinal").over(key) * k_pos / cnt_pos).ceil() \
        .clip(k_neg + 1, n_groups)
    q = (pl.when(~fin).then(None)
         .when(neg).then(pl.when(cnt_neg > 0).then(q_neg).otherwise(None))
         .otherwise(pl.when(cnt_pos > 0).then(q_pos).otherwise(None)))
    return df.with_columns(q.cast(pl.Int32).alias(out))


@dataclass(frozen=True)
class DropAccounting:
    """丢样三分账（alphalens 的 ``max_loss`` 思想）。

    因子评价里"丢了多少样本"从来不是一个数：**因子本身是空的**、**前瞻收益
    缺失**（回测期末尾必然出现）、**分组键缺失** 是三件不同的事。合成一个
    "丢了 30%" 的数字，既查不出原因，也会把「数据集本来就该这么小」误判成 bug。
    """

    total: int
    used: int
    factor_null: int
    factor_nonfinite: int
    ret_missing: int
    group_missing: int

    @property
    def dropped(self) -> int:
        return self.total - self.used

    @property
    def loss_ratio(self) -> float:
        return (self.dropped / self.total) if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total, "used": self.used, "dropped": self.dropped,
            "loss_ratio": round(self.loss_ratio, 6),
            "factor_null": self.factor_null,
            "factor_nonfinite": self.factor_nonfinite,
            "ret_missing": self.ret_missing,
            "group_missing": self.group_missing,
        }


def account_samples(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                    *, date_col: str = "trade_date",
                    by_group: str | None = None) -> DropAccounting:
    """逐项统计会被分箱丢掉的样本（不真正改数据）。

    口径与 :func:`group_returns` 的 ``drop_nulls`` 一致：只有「因子有限 **且**
    前瞻收益非空 **且** 分组键非空」的行才参与分组。
    """
    if factor not in df.columns or ret_col not in df.columns:
        return DropAccounting(df.height, 0, df.height, 0, 0, 0)
    if by_group is not None and by_group not in df.columns:
        raise KeyError(f"by_group 列不存在: {by_group}")
    fin = pl.col(factor).is_finite()
    f_null = pl.col(factor).is_null()
    ok = fin & pl.col(ret_col).is_not_null()
    if by_group is not None:
        ok = ok & pl.col(by_group).is_not_null()
    row = df.select([
        f_null.sum().alias("f_null"),
        (pl.col(factor).is_not_null() & ~fin).sum().alias("f_nonfinite"),
        (fin & pl.col(ret_col).is_null()).sum().alias("ret_missing"),
        (fin & pl.col(ret_col).is_not_null()
         & (pl.col(by_group).is_null() if by_group else pl.lit(False))).sum().alias("grp_missing"),
        ok.sum().alias("used"),
    ]).row(0, named=True)
    return DropAccounting(
        total=df.height, used=int(row["used"] or 0),
        factor_null=int(row["f_null"] or 0),
        factor_nonfinite=int(row["f_nonfinite"] or 0),
        ret_missing=int(row["ret_missing"] or 0),
        group_missing=int(row["grp_missing"] or 0),
    )


def group_returns(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                  n_groups: int = 10, *, date_col: str = "trade_date",
                  weighted: bool = False, zero_aware: bool = False,
                  by_group: str | None = None) -> pl.DataFrame:
    """每期每组的平均收益（组内等权）。

    ``zero_aware`` / ``by_group`` 见 :func:`add_quantile`。
    """
    d = add_quantile(df, factor, n_groups, date_col=date_col, zero_aware=zero_aware,
                     by_group=by_group)
    cols = [date_col, "q", factor, ret_col] + ([by_group] if by_group else [])
    d = d.select(cols).drop_nulls()
    if weighted:
        agg = (pl.col(ret_col) * pl.col(factor).abs()).sum() / pl.col(factor).abs().sum()
    else:
        agg = pl.col(ret_col).mean()
    return (
        d.group_by([date_col, "q"])
        .agg([agg.alias("ret"), pl.len().alias("n")])
        .sort([date_col, "q"])
    )


def pivot_group_returns(g: pl.DataFrame, n_groups: int = 10, *,
                        date_col: str = "trade_date") -> pl.DataFrame:
    """把 group_returns 的长表一次性转成宽表（date + 1..N 列）。

    之前每个分位组都要 filter 一次长表，10 组就是 10 次全表扫描；
    pivot 一次成宽表后，所有下游计算（净值、汇总、多空）都退化成列运算。
    缺失的 (date, q) 组合补 null —— 该日该组无人，收益视为不参与累计。
    """
    if not len(g):
        return g
    piv = g.pivot(on="q", index=date_col, values="ret",
                  aggregate_function="first").sort(date_col)
    for q in range(1, n_groups + 1):
        if str(q) not in piv.columns:
            piv = piv.with_columns(pl.lit(None, dtype=pl.Float64).alias(str(q)))
    return piv.select([date_col] + [str(q) for q in range(1, n_groups + 1)])


def _cum_nav(col: str) -> pl.Expr:
    """(1+r) 累乘成净值；空值按 0 收益处理（净值原地踏步）。"""
    return (pl.col(col).fill_null(0.0) + 1.0).cum_prod()


def quantile_nav(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                 n_groups: int = 10, *, date_col: str = "trade_date",
                 zero_aware: bool = False,
                 by_group: str | None = None) -> pl.DataFrame:
    """各组净值曲线。返回宽表：date + q1..qN + long_short。

    注意：这里的「收益」是**当期因子暴露对应的前瞻收益**，
    等权组合的换仓已经在收益定义里，不做额外的复利错位。
    """
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col,
                      zero_aware=zero_aware, by_group=by_group)
    if not len(g):
        return g
    piv = pivot_group_returns(g, n_groups, date_col=date_col)
    out = piv.with_columns(
        [_cum_nav(str(q)).alias(f"q{q}") for q in range(1, n_groups + 1)]
    ).select([date_col] + [f"q{q}" for q in range(1, n_groups + 1)])
    return out.with_columns((pl.col(f"q{n_groups}") - pl.col("q1")).alias("long_short"))


def long_short_nav(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                   n_groups: int = 10, *, date_col: str = "trade_date",
                   top: int | None = None, bottom: int | None = None,
                   zero_aware: bool = False,
                   by_group: str | None = None) -> pl.DataFrame:
    """多空组合净值（做多 top 组、做空 bottom 组，日度再平衡）。

    默认 1/N 十分位多空。常用替代：top=1, bottom=10（十分位）。
    """
    top = top or n_groups
    bottom = bottom or 1
    for name, q in (("top", top), ("bottom", bottom)):
        if not 1 <= q <= n_groups:
            raise ValueError(f"{name}={q} 超出分组范围 1..{n_groups}")
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col,
                      zero_aware=zero_aware, by_group=by_group)
    if not len(g):
        return g
    piv = pivot_group_returns(g, n_groups, date_col=date_col)
    return (
        piv.select([
            date_col,
            pl.col(str(top)).fill_null(0.0).alias("ret_long"),
            pl.col(str(bottom)).fill_null(0.0).alias("ret_short"),
        ])
        .with_columns((pl.col("ret_long") - pl.col("ret_short")).alias("ret_long_short"))
        .with_columns(_cum_nav("ret_long_short").alias("nav_long_short"))
    )


#: 多空绩效的键集合。正常路径与空数据路径**必须用同一份**——两条路径返回
#: 结构不一致，调用方（server/api/factors.py）就只能在空面板上 KeyError。
_LS_KEYS = ("total_return", "annual_return", "annual_vol", "sharpe",
            "max_drawdown", "win_rate", "calmar")


def quantile_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                     n_groups: int = 10, *, date_col: str = "trade_date",
                     periods_per_year: int = 252, zero_aware: bool = False,
                     by_group: str | None = None,
                     max_loss: float | None = None) -> dict:
    """分层汇总：每组年化 + 多空绩效 + 单调性 + **丢样三分账**。

    空数据（因子全 null / 样本不足分不出组）时返回**同样的键**，值全为 None：
    「没有结论」和「没有这个字段」对调用方是两回事，后者会直接炸。

    ``max_loss``：丢样比例阈值（0.3 = 30%）。超过就抛 ``DataQualityError``，
    而不是在报告里印一行小字就完事 —— 分组收益最大的坑就是「只剩 5% 的样本
    还在算多空」，覆盖率一掉，IC 反而好看。默认 None = 只统计不拦截。
    """
    drops = account_samples(df, factor, ret_col, date_col=date_col, by_group=by_group)
    if max_loss is not None and drops.loss_ratio > max_loss:
        raise DataQualityError(
            "SAMPLE_LOSS",
            f"分层丢样 {drops.loss_ratio:.1%} 超过阈值 {max_loss:.1%}"
            f"（总数 {drops.total}，可用 {drops.used}；"
            f"因子空 {drops.factor_null}、因子非有限 {drops.factor_nonfinite}、"
            f"前瞻收益缺失 {drops.ret_missing}、分组键缺失 {drops.group_missing}）",
        )

    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col,
                      zero_aware=zero_aware, by_group=by_group)
    if not len(g):
        return {"factor": factor, "ret_col": ret_col, "n_groups": n_groups,
                "zero_aware": zero_aware, "by_group": by_group,
                "groups": [], "long_short": dict.fromkeys(_LS_KEYS),
                "monotonicity": float("nan"), "top_bottom_spread": float("nan"),
                "dropped": drops.to_dict()}

    piv = pivot_group_returns(g, n_groups, date_col=date_col)
    groups = []
    for q in range(1, n_groups + 1):
        s = piv[str(q)].drop_nulls()
        # 股票数 < n_groups 时部分分位组可能为空 —— 该列全 null，按 NaN 处理
        m = s.mean()
        r = s.to_numpy() if len(s) else []
        p = perf_from_returns(r, periods_per_year=periods_per_year) if len(s) else {}
        groups.append({
            "q": q,
            "mean_ret": float(m) if m is not None else float("nan"),
            "annual_return": p.get("annual_return", float("nan")),
            "sharpe": p.get("sharpe", float("nan")),
            "max_drawdown": p.get("max_drawdown", float("nan")),
            "n_periods": len(s),
        })

    ls = piv.select(
        (pl.col(str(n_groups)).fill_null(0.0) - pl.col("1").fill_null(0.0))
        .alias("ret_long_short")
    )["ret_long_short"]
    ls_perf = perf_from_returns(ls.to_numpy(), periods_per_year=periods_per_year) \
        if len(ls) else {}

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
        "zero_aware": zero_aware,
        "by_group": by_group,
        "groups": groups,
        "long_short": {k: ls_perf.get(k) for k in _LS_KEYS},
        "monotonicity": mono,
        "top_bottom_spread": spread,
        # 丢样账随结果一起走：下游（报告 / 前端 / 评级）不必重新算一遍，
        # 也就不会出现「报告里写的覆盖率」和「实际用的样本」不一致
        "dropped": drops.to_dict(),
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
