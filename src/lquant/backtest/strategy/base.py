"""策略基类。原生策略直接继承。"""
from __future__ import annotations

from dataclasses import dataclass

from lquant.backtest.account import Account
from lquant.backtest.events import Bar


@dataclass
class Context:
    account: Account
    trade_date: object
    rules: dict
    params: dict


class Strategy:
    """子类实现 on_bar。"""

    def __init__(self, **params) -> None:
        self.params = params

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        """返回目标权重 [(symbol, weight), ...]，由引擎换算成订单。"""
        raise NotImplementedError
