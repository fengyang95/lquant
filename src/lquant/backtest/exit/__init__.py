"""可插拔退出策略层。

**为什么单独一层**：同一套选股信号，配不同退出规则会得到完全不同的绩效。
把退出从策略里抽出来，才能做「选股维度 × 退出维度」的正交实验，
而不必给每个策略复制一份止盈止损。

三个内置实现：

======================  ==================================================
``simple``              固定止损 / 固定止盈 / 时间止损
``tiered``              分级移动止盈（峰值盈利越多，容忍回撤越小）
``pressure``            通道压力位分批止盈（A 档卖 1/3 → B 档再卖 1/2 → C 档清）
======================  ==================================================

用法（不改回测引擎）::

    from lquant.backtest.exit import ExitOverlay, get_exit_strategy

    strategy = ExitOverlay(MySignalStrategy(), get_exit_strategy("tiered"))
    Engine(strategy).run(data)

移植来源与修正说明见各实现模块的 docstring。
"""
from __future__ import annotations

from lquant.backtest.exit import pressure, simple, tiered  # noqa: F401  触发注册
from lquant.backtest.exit.base import (
    ExitContext,
    ExitSignal,
    ExitStrategy,
    ExitType,
    PositionView,
    is_sealed_limit_down,
)
from lquant.backtest.exit.overlay import ExitOverlay
from lquant.backtest.exit.pressure import PressureExitStrategy
from lquant.backtest.exit.registry import (
    EXIT_STRATEGIES,
    get_exit_strategy,
    register_exit_strategy,
)
from lquant.backtest.exit.simple import SimpleExitStrategy
from lquant.backtest.exit.tiered import DEFAULT_TIERS, TieredExitStrategy

__all__ = [
    "DEFAULT_TIERS",
    "EXIT_STRATEGIES",
    "ExitContext",
    "ExitOverlay",
    "ExitSignal",
    "ExitStrategy",
    "ExitType",
    "PositionView",
    "PressureExitStrategy",
    "SimpleExitStrategy",
    "TieredExitStrategy",
    "get_exit_strategy",
    "is_sealed_limit_down",
    "register_exit_strategy",
]
