"""记录级八项断言（§3.7）。每次同步后必跑，fatal 失败阻断下游。

只证明「每条数据都合法」。整体性错误（复权系统性偏移、日期错位）
由 validators.py 的序列/截面级检查负责，两者是互补不是重复。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import DataQualityError
from lquant.data.quality.flags import (
    ADJ_ANOMALY,
    NEG_QTY,
    OHLC_CONFLICT,
    PRE_CLOSE_MISSING,
    PRICE_OUT_OF_RANGE,
    SUSPENSION_FILL,
    UNIT_MISMATCH,
    ZOMBIE,
    hit,
    or_flags,
)
from lquant.data.quality.issues import Issue

__all__ = ["run_record_checks", "assert_no_dup"]

# 单位归一的合理性带宽：amount / (close * volume) 应接近 1
# （VWAP 与 close 同量级）。万元当元会差 1e4，亿元差 1e8 —— 一查一个准。
_UNIT_LO, _UNIT_HI = 0.2, 5.0

# fatal 断言 → 位掩码 / 规则名一一对应：打标、报 issue 都能定位到具体断言
_FATAL_SPECS = [
    ("PRICE_RANGE", PRICE_OUT_OF_RANGE),
    ("OHLC_CONFLICT", OHLC_CONFLICT),
    ("NEG_QTY", NEG_QTY),
    ("UNIT_MISMATCH", UNIT_MISMATCH),
]


def _warn_issues(out: pl.DataFrame, issues: list[Issue], dataset: str) -> None:
    """warn 级 issue 汇总（flags 已含全部命中，这里只做计数 → Issue）。"""
    n_susp = len(out.filter((pl.col("quality_flags") & SUSPENSION_FILL) != 0))
    n_zombie = len(out.filter((pl.col("quality_flags") & ZOMBIE) != 0))
    n_preclose = len(out.filter((pl.col("quality_flags") & PRE_CLOSE_MISSING) != 0))
    if n_preclose:
        issues.append(Issue(rule="PRE_CLOSE_MISSING", severity="warn", dataset=dataset,
                            detail=f"{n_preclose} 行 pre_close 缺失"
                                   f"（上市首日属正常）", count=n_preclose))
    if n_susp:
        issues.append(Issue(rule="SUSPENSION_FILL", severity="warn", dataset=dataset,
                            detail=f"{n_susp} 行疑似停牌填充", count=n_susp))
    if n_zombie:
        issues.append(Issue(rule="ZOMBIE", severity="warn", dataset=dataset,
                            detail=f"{n_zombie} 行疑似僵尸报价", count=n_zombie))


def run_record_checks(df: pl.DataFrame, *, raise_on_fatal: bool = True,
                      dataset: str = "daily_bar") -> tuple[pl.DataFrame, list[Issue]]:
    """八项断言的记录级部分：打标 + 出 issue；fatal 聚合后一次性抛出。

    返回 (带 quality_flags 的 df, issues)。fatal 抛错发生在打标之后，
    便于调用方在捕获后仍能保留证据数据。不改变行序（H 下游按行号对账）。

    注意：复权因子跳变检查假设输入按 (symbol, trade_date) 有序 ——
    入湖前 write_daily 会排序，同步链路里的批次天然满足。

    停牌豁免：is_suspended=True 的行不参与记录级断言（序列级 adj 跳变检查除外）（停牌日零量零额、
    价格静止都是常态，按正常行检查只会误报）。只影响断言，不影响入湖数据。
    """
    if not len(df):
        return df, []

    issues: list[Issue] = []
    masks: list[pl.Expr] = []
    # 停牌行的所有命中条件强制为 False（行保留、flags 不打）
    not_susp = (
        ~pl.col("is_suspended").fill_null(False)
        if "is_suspended" in df.columns else pl.lit(True)
    )

    # 1) 价格区间（fatal）—— 0.1 ~ 10000 元；只看 o/h/l/c。
    #    pre_close 为空常见于上市首日 —— warn 打标，不阻断（H6）
    price_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    cond = pl.lit(False)
    for c in price_cols:
        cond = cond | pl.col(c).is_null() | (pl.col(c) <= 0.1) | (pl.col(c) > 10000)
    masks.append(hit(PRICE_OUT_OF_RANGE, cond & not_susp))

    # 2) OHLC 一致性（fatal）
    if {"open", "high", "low", "close"} <= set(df.columns):
        ohlc_bad = (
            (pl.col("high") < pl.max_horizontal("open", "close"))
            | (pl.col("low") > pl.min_horizontal("open", "close"))
            | (pl.col("high") < pl.col("low"))
        )
        masks.append(hit(OHLC_CONFLICT, (ohlc_bad & not_susp).fill_null(False)))

    # 3) 非负量额（fatal）
    qty_bad = pl.lit(False)
    if "volume" in df.columns:
        qty_bad = qty_bad | (pl.col("volume") < 0)
    if "amount" in df.columns:
        qty_bad = qty_bad | (pl.col("amount") < 0)
    masks.append(hit(NEG_QTY, (qty_bad & not_susp).fill_null(False)))

    # 4) 单位归一（fatal）—— amount 应与 close*volume 同量级
    if {"amount", "close", "volume"} <= set(df.columns):
        ratio = pl.when((pl.col("volume") > 0) & (pl.col("close") > 0) & (pl.col("amount") > 0))\
            .then(pl.col("amount") / (pl.col("close") * pl.col("volume"))).otherwise(1.0)
        masks.append(hit(UNIT_MISMATCH,
                         ((ratio < _UNIT_LO) | (ratio > _UNIT_HI)) & not_susp))

    # 5) pre_close 缺失（warn）—— 上市首日合法，其余情况提示。
    #    独立位掩码：不能混进 PRICE_OUT_OF_RANGE，否则会被 fatal 聚合误捕
    if "pre_close" in df.columns:
        masks.append(hit(PRE_CLOSE_MISSING,
                         pl.col("pre_close").is_null() & not_susp))

    # 6) 停牌填充（warn）—— 零成交但价格在动，应显式标记而非 0 值
    if {"volume", "open", "close"} <= set(df.columns):
        susp = (pl.col("volume") == 0) & (pl.col("open") != pl.col("close"))
        masks.append(hit(SUSPENSION_FILL, (susp & not_susp).fill_null(False)))

    # 7) 僵尸报价（warn，行级：零成交零收益；全市场占比由 validators 管）
    if {"volume", "open", "close"} <= set(df.columns):
        zombie = (pl.col("volume") == 0) & (pl.col("open") == pl.col("close"))
        masks.append(hit(ZOMBIE, (zombie & not_susp).fill_null(False)))

    out = or_flags(df, *masks)

    # 8) 复权因子跳变（warn）—— |factor[t]/factor[t-1] - 1| > 0.5 疑似除权
    #    或复权异常；序列级，单独处理
    if "adj_factor" in out.columns and {"symbol", "trade_date"} <= set(out.columns):
        out = out.with_columns(
            ((pl.col("adj_factor") / pl.col("adj_factor").shift(1).over("symbol") - 1)
             .abs()).alias("_adj_jump"))
        jump = out.filter(
            pl.col("_adj_jump").is_not_null() & (pl.col("_adj_jump") > 0.5))
        out = or_flags(
            out, hit(ADJ_ANOMALY, pl.col("_adj_jump").fill_null(0.0) > 0.5)
        ).drop("_adj_jump")
        if len(jump):
            issues.append(Issue(
                rule="ADJ_JUMP", severity="warn", dataset=dataset,
                detail=f"{len(jump)} 行复权因子跳变 >50%，疑似除权或因子错误",
                count=len(jump),
                extra={"symbols": jump["symbol"].unique().to_list()[:20]}))

    # fatal 聚合：每项断言独立成 issue，定位到具体规则
    for rule, mask in _FATAL_SPECS:
        bad = out.filter((pl.col("quality_flags") & mask) != 0)
        if len(bad):
            issues.append(Issue(rule=rule, severity="fatal", dataset=dataset,
                                detail=f"{len(bad)} 行未通过 {rule} 断言",
                                count=len(bad)))

    if raise_on_fatal and any(i.severity == "fatal" for i in issues):
        first = next(i for i in issues if i.severity == "fatal")
        raise DataQualityError(first.rule.lower(), first.detail)

    _warn_issues(out, issues, dataset)
    return out, issues


def assert_no_dup(df: pl.DataFrame, keys: list[str]) -> None:
    n = len(df) - len(df.unique(subset=keys))
    if n:
        raise DataQualityError("duplicate_key", f"{keys} 有 {n} 行重复", "fatal")
