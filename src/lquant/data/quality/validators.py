"""五层验证（细化方案 §3.8）：记录级之上的一切。

- L2 序列级：缺口、跳变、僵尸、复权错
- L3 截面级：覆盖度突降、整日未更新、日历对齐
- L4 语义级：涨跌停约束（一行断言抓 80% 的价格错误）、前后复权收益率恒等

每个 check 返回 list[Issue]，不直接抛异常 —— 由调用方按 severity 决定
阻断（fatal）还是落库告警（warn）。这是「标记而非删除」原则的上层入口。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import polars as pl

from lquant.data.quality.issues import Issue

__all__ = [
    "check_limit_breach", "check_ret_identity", "check_calendar_alignment",
    "check_coverage", "check_zombie", "check_adj_factor",
]

# 涨跌停约束容差：涨停价四舍五入到分带来的浮动
_LIMIT_TOL = 0.005


def check_limit_breach(df: pl.DataFrame, security: pl.DataFrame,
                       *, dataset: str = "daily_bar") -> list[Issue]:
    """涨跌停约束（L4）：|close/pre_close - 1| 不应超过板性限制 + 容差。

    除权日、新股首日会有合法越界 —— 按标的占比判定，而非逐行 fatal：
    占比 > 0.5% 判 error（复权/单位/串码类错误），少量命中打标告警。

    已知局限（PIT）：security 表只有当前 board/is_st 快照，历史区间
    会用今天的口径回看 —— 调用方应只在近期窗口上跑本检查（见 pipeline）。
    """
    need = {"symbol", "close", "pre_close"}
    if not need <= set(df.columns) or not len(df) or not len(security):
        return []
    meta = security.select(
        pl.col("symbol"),
        pl.col("board").fill_null("main").alias("board"),
        pl.col("is_st").fill_null(False).alias("is_st"),
    ).unique(subset=["symbol"])
    joined = (
        df.join(meta, on="symbol", how="left")
        .with_columns(
            # 板性限制向量化：ST 5% / 创业板·科创板 20% / 北交所 30% / 主板 10%
            pl.when(pl.col("is_st")).then(0.05)
            .when(pl.col("board").is_in(["gem", "star"])).then(0.20)
            .when(pl.col("board") == "bj").then(0.30)
            .otherwise(0.10)
            .alias("_limit")
        )
        .with_columns(
            ((pl.col("close") / pl.col("pre_close") - 1).abs()
             > (pl.col("_limit") + _LIMIT_TOL)).alias("_breach")
        )
    )
    bad = joined.filter(pl.col("_breach").fill_null(False))
    if not len(bad):
        return []
    n = len(df)
    issues = [
        Issue(rule="LIMIT_BREACH",
              severity="error" if len(bad) / n > 0.005 else "warn",
              dataset=dataset,
              detail=f"{len(bad)}/{n} 行日收益超涨跌停限制（board/ST 口径），"
                     f"疑似复权、单位或串码错误",
              count=len(bad),
              extra={"symbols": bad["symbol"].unique().to_list()[:20]}),
    ]
    return issues


def check_ret_identity(qfq_close: pl.Series, hfq_close: pl.Series,
                       *, symbol: str = "", dataset: str = "daily_bar",
                       atol: float = 1e-9) -> list[Issue]:
    """前后复权收益率恒等（L4，杀手锏）：前复权与后复权只差常数因子，
    日收益率序列在数学上必须完全相同。不通过必然是复权因子错。"""
    q = qfq_close.cast(pl.Float64, strict=False)
    h = hfq_close.cast(pl.Float64, strict=False)
    if len(q) != len(h) or len(q) < 2:
        return []
    # 两列拼成 frame 后整行丢弃空值 —— 各自独立 drop_nulls 会让序列错位，
    # 把合法数据错判成复权错误（停牌缺口不在同一行时）
    both = pl.DataFrame({"q": q, "h": h}).drop_nulls()
    if len(both) < 2:
        return []
    ret_q = both["q"] / both["q"].shift(1) - 1
    ret_h = both["h"] / both["h"].shift(1) - 1
    pair = pl.DataFrame({"rq": ret_q, "rh": ret_h}).drop_nulls()
    if not len(pair):
        return []
    diff = (pair["rq"] - pair["rh"]).abs().max()
    if diff is not None and diff > atol:
        return [Issue(rule="ADJ_RET_IDENTITY", severity="error", dataset=dataset,
                      symbol=symbol or None,
                      detail=f"前后复权收益率不一致 max|Δret|={diff:.3e} > {atol}，"
                             f"复权因子必有错误",
                      count=1)]
    return []


def check_calendar_alignment(dates: list[date], calendar: list[date],
                             *, dataset: str = "daily_bar") -> list[Issue]:
    """交易日历对齐（L3）：数据日期必须是官方交易日子集；完整年度交易日 240–245。

    年度天数只检查「内部年」—— 窗口首尾年份天然不满一年
    （跨年切片 / 进行中的当年），对它们断言必然产生永久性假 fatal。
    """
    issues: list[Issue] = []
    cal = set(calendar)
    if cal:
        stray = sorted(set(dates) - cal)
        if stray:
            issues.append(Issue(
                rule="CALENDAR_STRAY", severity="fatal", dataset=dataset,
                detail=f"{len(stray)} 个数据日期不在官方交易日历内"
                       f"（首例 {stray[0]}）—— 日历错或日期错位",
                count=len(stray), extra={"dates": [str(d) for d in stray[:10]]}))
    years = sorted({d.year for d in calendar})
    interior = set(years[1:-1]) if len(years) > 2 else set()
    by_year: dict[int, int] = defaultdict(int)
    for d in cal:
        by_year[d.year] += 1
    bad_years = {y: n for y, n in by_year.items()
                 if y in interior and not 240 <= n <= 245}
    if bad_years:
        issues.append(Issue(
            rule="CALENDAR_YEAR_LEN", severity="fatal", dataset=dataset,
            detail=f"交易日历年度天数异常（完整年应为 240–245）: {bad_years}",
            count=len(bad_years), extra={"years": bad_years}))
    return issues


def check_coverage(df: pl.DataFrame, *, min_ratio: float = 0.90,
                   dataset: str = "daily_bar") -> list[Issue]:
    """覆盖度（L3，fatal）：单日标的数相对全窗口中位数的占比。

    分母用「本湖自身的每日标的数中位数」而不是当前 active_symbols ——
    历史日期的合理标的数本来就少（未上市 / 已退市），拿今天的活跃
    清单当分母会把每一段历史都判成批量缺数（假 fatal 淹没真信号）。
    中位数口径只抓「突降」：某天比常态少 10% 以上 = 疑似批量缺数。
    """
    if not len(df):
        return []
    per_day = (
        df.group_by("trade_date")
        .agg(pl.col("symbol").n_unique().alias("n"))
        .sort("trade_date")
    )
    counts = per_day["n"]
    median = counts.median()
    if median is None or median <= 0:
        return []
    issues: list[Issue] = []
    for d, n in zip(per_day["trade_date"], counts):
        ratio = n / median
        if ratio < min_ratio:
            issues.append(Issue(
                rule="COVERAGE", severity="fatal", dataset=dataset, trade_date=d,
                detail=f"{d} 覆盖率 {ratio:.1%} < {min_ratio:.0%}（{n} 只，"
                       f"窗口中位 {median:.0f} 只），疑似批量缺数",
                count=int(median - n)))
    return issues


def check_zombie(df: pl.DataFrame, *, warn_ratio: float = 0.08,
                 error_ratio: float = 0.30, dataset: str = "daily_bar") -> list[Issue]:
    """僵尸检测（L3）：全市场某日 |ret| == 0 占比。正常 < 8%，
    > 30% 说明源站没更新（整日复制的旧数据）。"""
    if not {"symbol", "close"} <= set(df.columns) or not len(df):
        return []
    zero_ret = (
        df.sort(["symbol", "trade_date"])
        .with_columns((pl.col("close") == pl.col("close").shift(1).over("symbol"))
                      .alias("_zero"))
        .filter(pl.col("_zero").fill_null(False))
        .group_by("trade_date").agg(pl.len().alias("n_zero"))
    )
    per_day = df.group_by("trade_date").agg(pl.len().alias("n")).join(
        zero_ret, on="trade_date", how="inner").with_columns(
        (pl.col("n_zero") / pl.col("n")).alias("ratio"))
    issues: list[Issue] = []
    for d, ratio in zip(per_day["trade_date"], per_day["ratio"]):
        if ratio >= error_ratio:
            issues.append(Issue(
                rule="ZOMBIE_DAY", severity="error", dataset=dataset, trade_date=d,
                detail=f"{d} 零收益占比 {ratio:.0%} ≥ {error_ratio:.0%}，"
                       f"源站疑似未更新（僵尸日）", count=1))
        elif ratio >= warn_ratio:
            issues.append(Issue(
                rule="ZOMBIE_DAY", severity="warn", dataset=dataset, trade_date=d,
                detail=f"{d} 零收益占比 {ratio:.0%} ≥ {warn_ratio:.0%}", count=1))
    return issues


def check_adj_factor(df: pl.DataFrame, *, dataset: str = "daily_bar") -> list[Issue]:
    """复权因子（L2）：后复权因子应随时间单调不减；跳变 >50% 需与除权事件比对。"""
    if not {"symbol", "trade_date", "adj_factor"} <= set(df.columns) or not len(df):
        return []
    s = df.sort(["symbol", "trade_date"]).with_columns(
        pl.col("adj_factor").shift(1).over("symbol").alias("_prev"))
    decreasing = s.filter(
        pl.col("_prev").is_not_null() & (pl.col("adj_factor") < pl.col("_prev") - 1e-12))
    jump = s.filter(
        pl.col("_prev").is_not_null()
        & (((pl.col("adj_factor") / pl.col("_prev")) - 1).abs() > 0.5))
    issues: list[Issue] = []
    if len(decreasing):
        issues.append(Issue(
            rule="ADJ_DECREASE", severity="error", dataset=dataset,
            detail=f"{len(decreasing)} 行后复权因子不单调（分红送转只会让它变大）",
            count=len(decreasing),
            extra={"symbols": decreasing["symbol"].unique().to_list()[:20]}))
    if len(jump):
        issues.append(Issue(
            rule="ADJ_JUMP", severity="warn", dataset=dataset,
            detail=f"{len(jump)} 行复权因子跳变 >50%，疑似除权，需与除权事件比对",
            count=len(jump),
            extra={"symbols": jump["symbol"].unique().to_list()[:20]}))
    return issues
