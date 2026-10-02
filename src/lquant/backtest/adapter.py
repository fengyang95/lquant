"""回测引擎适配器：外部引擎接入协议 + 内置无摩擦对照引擎。

设计意图（docs/BACKTEST_ENGINES.md）：
自研 Engine 是唯一「执行真源」；外部引擎（RQAlpha / vectorbt 等）如需接入做
对照或参数扫描，实现本协议后结果与原生引擎同构，落同一批表、可直接 /compare。
引擎可换，研究资产不换。

**内置 `frictionless_buy_hold`**：一个零依赖、纯向量化的对照实现，用来支撑
L5「无摩擦设定下两引擎净值必须一致」这一层验证（原本 adapter 是**空注册表**，
零注册零调用方 → L5 无任何测试）。它不依赖第三方库，因此 CI 可跑；真正的
第三方引擎（vectorbt/RQAlpha）仍按同一协议接入。

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


class FrictionlessBuyHoldAdapter:
    """无摩擦买入持有对照引擎（L5 用的独立实现）。

    刻意**不复用** Engine/Broker/Account 的任何代码：直接按收盘价向量化算净值。
    因此它与原生引擎是两份独立实现 —— 无摩擦设定下两者净值必须逐日一致，任何
    一端的系统性偏差（费用漏算、份额换算、估值时点）都会立刻暴露。

    口径（与 Engine 在以下设定下对齐）：
      price_mode=same_close（信号日收盘价成交）、零费率/零滑点、无涨跌停、
      lot_size=1、单标的、目标权重只下过一次（此后持有不动）。
    """

    name = "frictionless_buy_hold"

    def run(self, df: pl.DataFrame, *, symbol: str, weight: float = 1.0,
            initial_cash: float = 1_000_000.0, **_ignored) -> AdapterOutput:
        sub = (df.filter(pl.col("symbol") == symbol)
                 .sort("trade_date")
                 .select(["trade_date", "close"]))
        if sub.is_empty():
            return AdapterOutput(meta={"error": f"{symbol} 无数据"})
        dates = sub["trade_date"].to_list()
        closes = [float(c) for c in sub["close"].to_list()]

        # 首日收盘建仓（lot_size=1 → 不取整），此后持有不动
        px0 = closes[0]
        qty = (initial_cash * float(weight)) / px0
        cash = initial_cash - qty * px0
        nav = [cash + qty * c for c in closes]

        from lquant.backtest.metrics import perf_from_nav
        metrics = perf_from_nav(nav, dates=dates)
        metrics.pop("nav", None)
        metrics.update({"initial_cash": initial_cash, "final_nav": nav[-1],
                        "n_trades": 1})
        trades = pl.DataFrame([{
            "trade_date": dates[0], "symbol": symbol, "side": "buy",
            "qty": qty, "price": px0, "fee": 0.0, "amount": qty * px0,
        }])
        return AdapterOutput(nav=list(zip(dates, nav, strict=True)), trades=trades,
                             metrics=metrics,
                             meta={"engine": self.name, "qty": qty, "weight": weight})


# 非空注册表：L5 的对照层至少有一个可跑、零依赖的实现（第三方引擎按同一协议接入）
register_adapter(FrictionlessBuyHoldAdapter)
