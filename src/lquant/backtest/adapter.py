"""外部回测引擎适配器协议 —— 预留接口。

设计意图（docs/BACKTEST_ENGINES.md）：
自研 Engine 是唯一「执行真源」；外部引擎（RQAlpha / vectorbt 等）如需接入做
对照或参数扫描，实现本协议后结果与原生引擎同构，落同一批表、可直接 /compare。
引擎可换，研究资产不换。

用法：
    from lquant.backtest.adapter import AdapterOutput, register_adapter

    class MyAdapter:
        name = "my_engine"
        def run(self, df: pl.DataFrame, **params) -> AdapterOutput: ...

    register_adapter(MyAdapter)          # 注册
    from lquant.backtest.adapter import get_adapter
    out = get_adapter("my_engine").run(df, top_n=10)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

import polars as pl

from lquant.core.registry import Registry


@dataclass
class AdapterOutput:
    """与 Engine.run() 输出同形 —— 保证落库与对比逻辑零差异复用。

    metrics 约定使用 backtest.metrics.perf_from_nav 的键名
    （total_return / annual_return / sharpe / max_drawdown / ...）。
    """

    nav: list[tuple[date, float]] = field(default_factory=list)
    trades: pl.DataFrame = field(default_factory=pl.DataFrame)
    metrics: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@runtime_checkable
class BacktestAdapter(Protocol):
    """外部引擎适配器协议。实现方需保证：

    1. 输入 df 至少含 trade_date/symbol/open/high/low/close/pre_close；
    2. 输出 AdapterOutput（净值逐交易日，trades 含 trade_date/symbol/side/qty/price/fee）。
    """

    name: str

    def run(self, df: pl.DataFrame, **params) -> AdapterOutput: ...


ADAPTERS: Registry[type] = Registry("backtest_adapters")


def register_adapter(cls: type) -> type:
    """类或工厂注册。类需带 name 属性与 run() 方法。"""
    return ADAPTERS.register(getattr(cls, "name", cls.__name__), {})(cls)


def get_adapter(name: str):
    return ADAPTERS.get(name)()


def list_adapters() -> list[str]:
    return ADAPTERS.keys()
