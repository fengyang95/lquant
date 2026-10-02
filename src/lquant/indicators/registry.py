"""指标注册表。

扩展点之一，与 ``factors/ops`` / ``backtest/strategy`` 同构：

    from lquant.indicators import register_indicator

    @register_indicator("my_ind", label="我的指标", category="trend",
                        min_window=20, outputs=("my_ind",))
    def add_my_ind(df: pl.DataFrame, n: int = 20) -> pl.DataFrame:
        return df.with_columns(pl.col("close").rolling_mean(n).alias("my_ind"))

注册后 ``GET /api/data/indicators`` 与前端可自动枚举。

**为什么指标不放进因子算子（``factors/ops``）**：因子 DSL 是逐节点表达式树，
每个算子都必须是可静态推导 ``min_window`` 的窗口化纯函数。指标里有相当一部分
是**有状态/分段**结构（中枢、浪型、通道），用 ``Ts_*`` 算子表达不出来。
两者关系是：指标可作为「预计算信号列」反哺因子层，而不是让指标去实现 DSL 算子。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import polars as pl

from lquant.core.registry import Registry

__all__ = ["INDICATORS", "register_indicator", "compute", "compute_many",
           "min_window", "outputs", "required_history"]

IndicatorFn = Callable[..., pl.DataFrame]

INDICATORS: Registry[IndicatorFn] = Registry("indicators")

# 类别取值：趋势 / 摆动 / 量能 / 形态 / 通道
CATEGORIES = ("trend", "oscillator", "volume", "pattern", "channel")

# 挂载面板：**这是给前端用的量纲信息，不能靠类别猜**。
# MACD 属 trend，但它的量纲与价格差两个数量级，画到价格轴上会把 K 线压成一条直线；
# 而 KDJ 属 oscillator 同理。所以「画在哪」必须显式声明，而不是由 category 推断。
PANES = ("price", "sub", "volume")


def register_indicator(
    name: str,
    *,
    label: str = "",
    category: str = "trend",
    pane: str = "sub",
    min_window: int = 0,
    inputs: Iterable[str] = ("close",),
    outputs: Iterable[str] | None = None,
) -> Callable[[IndicatorFn], IndicatorFn]:
    """装饰器：注册指标。

    Args:
        name: 唯一键，前端与 API 用。
        label: 中文展示名。
        category: ``trend`` / ``oscillator`` / ``volume`` / ``pattern`` / ``channel``。
        pane: 前端挂载面板 —— ``price``（与价格同轴，可叠加 K 线）、
            ``sub``（独立子图）、``volume``（量能轴）。
            **默认 ``sub``**：不显式声明就不会被误画到价格轴上。
        min_window: 预热根数。**取数时必须多取这么多根再截尾**，否则头部指标为 null。
        inputs: 依赖的输入列。
        outputs: 新增的列名；缺省为 ``(name,)``。未来函数检测按此逐列比对。
    """
    if category not in CATEGORIES:
        raise ValueError(f"未知指标类别 {category!r}，可选: {CATEGORIES}")
    if pane not in PANES:
        raise ValueError(f"未知挂载面板 {pane!r}，可选: {PANES}")
    if min_window < 0:
        raise ValueError("min_window 不能为负")
    meta: dict[str, Any] = {
        "label": label or name,
        "category": category,
        "pane": pane,
        "min_window": int(min_window),
        "inputs": list(inputs),
        "outputs": list(outputs) if outputs is not None else [name],
    }
    return INDICATORS.register(name, meta)


def outputs(name: str) -> list[str]:
    """指标新增的列名。"""
    INDICATORS.get(name)  # 未注册即抛 KeyError（含可选清单）
    return list(INDICATORS.meta(name).get("outputs", []))


def min_window(name: str) -> int:
    """指标预热根数。"""
    INDICATORS.get(name)
    return int(INDICATORS.meta(name).get("min_window", 0))


def required_history(name: str) -> int:
    """取数建议根数：预热期 + 一个默认观察窗口（250 个交易日 ≈ 一年）。"""
    return min_window(name) + 250


def compute(name: str, df: pl.DataFrame, **params: Any) -> pl.DataFrame:
    """按名字计算单个指标，返回原表 + 指标列（不改行数、不改顺序）。"""
    return INDICATORS.get(name)(df, **params)


def compute_many(df: pl.DataFrame, names: Iterable[str],
                 params: dict[str, dict] | None = None) -> pl.DataFrame:
    """串联多个指标。``params`` 为 ``{指标名: 关键字参数}``。"""
    params = params or {}
    out = df
    for n in names:
        out = compute(n, out, **params.get(n, {}))
    return out
