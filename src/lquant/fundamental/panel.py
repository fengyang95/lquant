"""PIT 面板解析：把 ``financial_pit`` 长表解析成某个观察日「可见」的宽表。

**这是与 FinancialTool 基本面模块最本质的差异。**

FinancialTool 的同花顺路径不带公告日，只按报告期排序取最新一期 ——
最新报告期可能在公告之前就被当成「已披露」，回测出现前视偏差
（详见对比报告的移植坑清单第 2 条）。

本模块的所有解析都强制 ``pub_date <= asof``：**没有公告日的行永远不会返回**，
宁可当天拿不到数据，也不给未来函数开门。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

__all__ = ["WIDE_EMPTY", "resolve_industry", "resolve_pit"]

#: 解析结果的空表骨架（下游无需再判列是否存在）
WIDE_EMPTY = pl.DataFrame(
    schema={"symbol": pl.String, "stat_date": pl.Date, "pub_date": pl.Date}
)


def resolve_pit(panel: pl.DataFrame, asof: date,
                items: Sequence[str] | None = None) -> pl.DataFrame:
    """长表 → 宽表：每个 ``(symbol, item)`` 取 ``pub_date <= asof`` 中报告期最新的一条。

    Args:
        panel: ``financial_pit`` 长表，至少含
            ``symbol / stat_date / pub_date / item / value``。
        asof: 观察日（含）。只用该日及以前**已公告**的数据。
        items: 只保留这些 item；``None`` 表示全部。

    Returns:
        ``symbol`` + ``stat_date`` + ``pub_date`` + 每个 item 一列。
        同一报告期存在修订时取 ``pub_date`` 较晚的那条。
    """
    if panel.is_empty():
        return WIDE_EMPTY.clone()
    need = {"symbol", "stat_date", "pub_date", "item", "value"}
    missing = need - set(panel.columns)
    if missing:
        raise KeyError(f"financial_pit 面板缺列: {sorted(missing)}")

    sub = panel.filter(pl.col("pub_date") <= asof)
    if items is not None:
        sub = sub.filter(pl.col("item").is_in(list(items)))
    if sub.is_empty():
        return WIDE_EMPTY.clone()

    # 排序后取最后一条 = stat_date 最新；同报告期取公告更晚的修订版
    latest = (sub.sort(["symbol", "item", "stat_date", "pub_date"])
                 .group_by(["symbol", "item"])
                 .agg(pl.col("stat_date").last(), pl.col("pub_date").last(),
                      pl.col("value").last()))
    wide = latest.pivot(on="item", index=["symbol", "stat_date", "pub_date"],
                        values="value", aggregate_function="last")
    return wide.sort("symbol")


def resolve_industry(ic: pl.DataFrame, asof: date,
                     std: str | None = None) -> pl.DataFrame:
    """行业分类的 PIT 解析：取 ``std_date <= asof`` 中生效日最新的一条。

    ``industry_classify`` 表带 ``std_date``（生效日）就是为了防止
    「用今天的分类去回测十年前」—— 本函数是该列的强制消费点。

    Returns:
        ``(symbol, industry)``；行业取 ``name``（缺失时回退 ``code``）。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "industry": pl.String})
    if ic.is_empty():
        return empty
    need = {"symbol", "std_date"}
    missing = need - set(ic.columns)
    if missing:
        raise KeyError(f"industry_classify 缺列: {sorted(missing)}")

    sub = ic.filter(pl.col("std_date") <= asof)
    if std is not None and "std" in sub.columns:
        sub = sub.filter(pl.col("std") == std)
    if sub.is_empty():
        return empty

    label = (pl.col("name").fill_null(pl.col("code"))
             if {"name", "code"} <= set(sub.columns)
             else (pl.col("name") if "name" in sub.columns else pl.col("code")))
    out = (sub.sort(["symbol", "std_date"])
              .group_by("symbol")
              .agg(label.last().alias("industry")))
    return out.select(["symbol", "industry"]).sort("symbol")
