"""退出策略注册表。

    from lquant.backtest.exit import register_exit_strategy

    @register_exit_strategy("my_exit", label="我的退出规则")
    class MyExit(ExitStrategy):
        def on_bar(self, ctx): ...

注册后 ``/api/backtests/exit-strategies`` 自动枚举，回测请求按名字实例化。
"""
from __future__ import annotations

from collections.abc import Callable

from lquant.backtest.exit.base import ExitStrategy
from lquant.core.registry import Registry

__all__ = ["EXIT_STRATEGIES", "get_exit_strategy", "register_exit_strategy"]

EXIT_STRATEGIES: Registry[type[ExitStrategy]] = Registry("exit_strategies")


def register_exit_strategy(name: str,
                           meta: dict | None = None) -> Callable[[type], type]:
    """装饰器：注册退出策略。``meta.label`` 用于前端展示。"""
    return EXIT_STRATEGIES.register(name, meta)


def get_exit_strategy(name: str, **params: object) -> ExitStrategy:
    """按名字实例化退出策略。"""
    cls = EXIT_STRATEGIES.get(name)
    return cls(**params)
