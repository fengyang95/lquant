"""字符串字段的「信号条件通道」。

扩展表里的字符串字段（所属概念、行业、标签）不该被注册成因子 —— 因子评价是
IC / 排序口径，对字符串排序没有金融含义。它们的价值在**条件筛选**：把
「属于 AI 概念」「行业 == 白酒」变成可组合的信号条件。

数值字段走因子通道（``factors/ext_bridge``），字符串字段走这里，两条通道
刻意分开：混在一起就会出现「给字符串算了 IC」这种无意义结果。
"""

from __future__ import annotations

import polars as pl

from lquant.data.ext.models import ExtConfig, ExtConfigError
from lquant.data.ext.schema import ext_column_name

#: 支持的字符串运算符。contains=包含子串，eq/ne=相等/不等。
STRING_OPS = ("contains", "eq", "ne")


def signal_fields(config: ExtConfig) -> list[str]:
    """该表可用于信号条件的字段名（配置里声明的字符串字段）。"""
    return [f.name for f in config.string_fields]


def evaluate_signal(frame: pl.DataFrame, config: ExtConfig, condition: dict) -> pl.Series:
    """把单个条件编译成布尔列。

    条件形如 ``{"field": "concepts", "op": "contains", "value": "AI"}``。
    字段必须是该表声明的**字符串**字段：拿数值字段做 contains 是口径错误，
    静默返回全 False 比报错更难排查。
    """
    field = str(condition.get("field", ""))
    op = str(condition.get("op", "contains"))
    value = condition.get("value")
    if field not in signal_fields(config):
        raise ExtConfigError(
            f"信号条件字段 {field!r} 不是扩展表 {config.id} 的字符串字段"
            f"（可用: {signal_fields(config)}）"
        )
    if op not in STRING_OPS:
        raise ExtConfigError(f"信号运算符非法: {op!r}（可选 {STRING_OPS}）")
    if value is None:
        raise ExtConfigError(f"信号条件缺 value: {condition!r}")
    # 同一条件要在两种帧上都能用：扩展表原始帧（列名=字段名）与注入后的
    # 面板（列名=ext_{表}_{字段}）。让调用方自己记住用哪套列名，迟早会分叉。
    column = field if field in frame.columns else ext_column_name(config.id, field)
    if column not in frame.columns:
        raise ExtConfigError(
            f"信号字段 {field!r} 在帧里找不到（既没有 {field!r} 也没有 {column!r}）"
        )
    text = str(value)
    col = pl.col(column).cast(pl.Utf8, strict=False).fill_null("")
    if op == "contains":
        return frame.select(col.str.contains(text, literal=True).alias("__sig")).to_series()
    if op == "eq":
        return frame.select((col == text).alias("__sig")).to_series()
    return frame.select((col != text).alias("__sig")).to_series()


def apply_signals(frame: pl.DataFrame, config: ExtConfig, conditions: list[dict]) -> pl.DataFrame:
    """多个信号条件取交集（AND）后返回命中行。

    空条件列表返回原帧 —— 「没有条件」是「不筛」，不是「全 False」。
    """
    if not conditions:
        return frame
    mask: pl.Series | None = None
    for cond in conditions:
        sig = evaluate_signal(frame, config, cond)
        mask = sig if mask is None else (mask & sig)
    assert mask is not None
    return frame.filter(mask)


def signal_field_catalog(config: ExtConfig) -> list[dict]:
    """信号条件字段清单（供 UI / AI 提示词枚举）。"""
    return [
        {"name": f.name, "label": f.label or f.name,
         "column": ext_column_name(config.id, f.name), "ops": list(STRING_OPS)}
        for f in config.string_fields
    ]
