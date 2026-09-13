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
    """子类实现 on_bar。

    on_bar 返回目标权重列表，约定：
    - ``[]``（或 None）→ 无操作，保留当前持仓（因子策略「当日无信号」的常态）；
    - ``[(symbol, w)]``，w > 0 → 调整到目标权重 w（引擎内部归一化）；
    - ``[(symbol, 0.0)]`` → 显式清仓该标的 —— 择时策略（双均线/动量轮动）
      表达「空仓」的唯一途径。引擎同时会把不在新目标里的持仓一并清掉。
    """

    def __init__(self, **params) -> None:
        self.params = params

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        """返回目标权重 [(symbol, weight), ...]，由引擎换算成订单。"""
        raise NotImplementedError
