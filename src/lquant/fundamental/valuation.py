"""估值指标：来自**日线湖**的估值列，不在 ``financial_pit`` 里。

为什么单独一层：``pe_ttm`` / ``pb_mrq`` / ``dv_ttm`` 是**日频**截面量，
天然属于日线湖（Tushare ``daily_basic`` 回填，见
:mod:`lquant.data.ingest.daily_basic`），把它们塞进 ``financial_pit``
会和季频报表混在同一张长表里、口径纠缠。评分目录用 ``valuation.*``
逻辑键引用本模块，取值时按 PIT 规则取 ``trade_date <= asof`` 的最近一根 bar
—— 与 :mod:`lquant.research.dialect.fundamentals` 的 ``_valuation_frame``
同一约定。**注意**：这意味着用「观察日当天收盘」的估值给当天打分；
日频策略若在开盘成交，应把 ``asof`` 取到前一日。

**符号污染（必须挡住）**：PE/PB 为负不是「很便宜」，而是「亏损 / 净资产为负」，
在经济上没有意义。若原样进入打分，反向指标（越低越好）会把亏损股排在全市场
最前面 —— 这是估值类因子最经典的一类假信号。因此本模块对 PE/PB 强制
``> 0``，非正值一律置空并计入覆盖率损失，而不是当成 0 或极低价。
股息率 ``dv_ttm`` 的 0（不分红）是**有效信息**，保留。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

__all__ = [
    "VALUATION_ITEMS",
    "valuation_frame",
    "valuation_items",
    "valuation_long",
]

#: 回退窗口：观察日往前找多少天里「最近一个有值」的 bar。
#: 只为兜住日线湖尾部偶发的估值列缺口（见 :func:`_last_bar`），不是长期回填。
#: 同时它把扫描范围收窄到一两个年文件，整块估值取数从 14s 降到 2s 量级。
_LOOKBACK_DAYS = 45

#: 逻辑键 → (湖分区, 列名)。日线湖与 daily_basic 湖分别承载不同列。
VALUATION_ITEMS: dict[str, tuple[str, str]] = {
    "valuation.pe_ttm": ("daily", "pe_ttm"),
    "valuation.pb": ("daily", "pb_mrq"),
    "valuation.dividend_yield": ("daily_basic", "dv_ttm"),
}

#: 必须严格为正才有意义的列（负值 = 亏损 / 净资产为负，不是「便宜」）
_POSITIVE_ONLY = {"pe_ttm", "pb_mrq"}

_PANEL_COLS = ("symbol", "stat_date", "pub_date", "item", "value")


def valuation_items() -> tuple[str, ...]:
    """全部估值逻辑键。"""
    return tuple(VALUATION_ITEMS)


def _scan_lake(kind: str) -> pl.LazyFrame | None:
    """懒扫某类湖；湖为空返回 ``None``（调用方据此区分「没同步」与「没结果」）。"""
    from lquant.data.store.parquet import lake_glob, lake_is_empty

    if lake_is_empty(kind):
        return None
    return pl.scan_parquet(lake_glob(kind), missing_columns="insert",
                           extra_columns="ignore")


def _last_bar(kind: str, column: str, asof: date,
              symbols: list[str] | None) -> pl.DataFrame:
    """每个 symbol 在 ``trade_date <= asof`` 上**最近一个有该列值的** bar。

    这里有两类「不可用」，处理方式**必须不同**：

    1. **值为空**（该日没回填到这一列）：日线湖的尾部几天经常缺估值列，
       这时回退到最近一次有值的那天是合理的 —— 否则 PE/PB 会在最近几天
       整体消失，看起来像「数据坏了」。
    2. **值为非正**（PE<0 亏损 / PB<0 净资产为负）：这是**有值的不可用**，
       必须就地置空，**不能**回退到更早的正值 —— 否则会把「今天亏损」
       伪装成「上周很便宜」，而反向打分（越低越好）正好把这种票排到最前。
    """
    lf = _scan_lake(kind)
    if lf is None or column not in lf.collect_schema().names():
        return pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date,
                                    column: pl.Float64})
    lf = (lf.filter(pl.col("trade_date") <= asof)
            .filter(pl.col("trade_date") >= asof - timedelta(days=_LOOKBACK_DAYS))
            .filter(pl.col(column).is_not_null())      # 只跳过「没值」，见上
            .select("symbol", "trade_date", column))
    if symbols:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    last = (lf.sort(["symbol", "trade_date"])
              .group_by("symbol")
              .agg(pl.col("trade_date").last(), pl.col(column).last())
              .collect())
    if column in _POSITIVE_ONLY:
        last = last.with_columns(
            pl.when(pl.col(column) > 0).then(pl.col(column))
            .otherwise(None).alias(column))
    return last


def valuation_frame(asof: date, symbols: list[str] | None = None) -> pl.DataFrame:
    """宽表 ``(symbol, pe_ttm, pb, dividend_yield)``，给前端直接展示。"""
    out: pl.DataFrame | None = None
    for item, (kind, column) in VALUATION_ITEMS.items():
        alias = item.split(".", 1)[1]
        bar = _last_bar(kind, column, asof, symbols)
        if not bar.height:
            continue
        part = bar.select("symbol", pl.col(column).alias(alias))
        out = part if out is None else out.join(part, on="symbol", how="full", coalesce=True)
    if out is None:
        return pl.DataFrame(schema={"symbol": pl.String})
    return out.sort("symbol")


def valuation_long(asof: date, symbols: list[str] | None = None) -> pl.DataFrame:
    """PIT 长表：``(symbol, stat_date=trade_date, pub_date=trade_date, item, value)``。

    形状与 ``financial_pit`` 一致，可以直接和报表面板 ``pl.concat`` 后
    交给 :func:`lquant.fundamental.panel.resolve_pit` 做统一的 PIT 解析。
    """
    frames: list[pl.DataFrame] = []
    for item, (kind, column) in VALUATION_ITEMS.items():
        bar = _last_bar(kind, column, asof, symbols)
        if not bar.height:
            continue
        # 置空的值不落成长表行：行存在但 value 为 null 会让「有没有这只票的
        # 估值」这件事在下游变得难判断（resolve_pit 不会替调用方判空）。
        bar = bar.filter(pl.col(column).is_not_null())
        if not bar.height:
            continue
        frames.append(
            bar.select(
                pl.col("symbol"),
                pl.col("trade_date").alias("stat_date"),
                pl.col("trade_date").alias("pub_date"),
                pl.lit(item).alias("item"),
                pl.col(column).cast(pl.Float64).alias("value"),
            )
        )
    if not frames:
        return pl.DataFrame(schema={
            "symbol": pl.String, "stat_date": pl.Date, "pub_date": pl.Date,
            "item": pl.String, "value": pl.Float64,
        })
    return pl.concat(frames, how="vertical_relaxed").select(_PANEL_COLS)
