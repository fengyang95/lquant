"""策略：原生 + 方言（JoinQuant 兼容）。

策略注册表 —— 扩展点之一：
    from lquant.backtest.strategy import register_strategy

    @register_strategy("my_strategy", {"label": "我的策略"})
    class MyStrategy(Strategy):
        def on_bar(self, ctx, bars): ...

注册后 GET /api/strategies 自动枚举，回测 API 可按名字实例化。
"""
from __future__ import annotations

from lquant.backtest.strategy.base import Context, Strategy  # noqa: F401 (re-export)
from lquant.core.registry import Registry

STRATEGIES: Registry[type[Strategy]] = Registry("strategies")


def register_strategy(name: str, meta: dict | None = None):
    """装饰器：注册策略类。meta.label 用于前端展示。"""
    return STRATEGIES.register(name, meta)


def get_strategy(name: str, **params) -> Strategy:
    """按名字实例化策略。"""
    return STRATEGIES.get(name)(**params)


def _import_native() -> None:
    """导入内置策略模块，触发注册（幂等）。"""
    import importlib

    for mod in ("factor_topn",):
        importlib.import_module(f"lquant.backtest.strategy.{mod}")


_import_native()
