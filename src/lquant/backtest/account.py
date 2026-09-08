"""账户与持仓。available 按 sellable_after_days 计算，不是写死 T+1。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from lquant.backtest.events import Fill, Side
from lquant.backtest.rules.model import InstrumentRules


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_cost: float = 0.0
    lots: list[tuple[date, float, float]] = field(default_factory=list)   # (buy_date, qty, price)

    @property
    def available_qty(self) -> float:
        return self.qty

    def available_at(self, d: date, rules: InstrumentRules) -> float:
        """T+N：QDII / 黄金 / 债券 / 货币 ETF 是 0，当日可卖。"""
        n = rules.sellable_after_days
        return sum(q for bd, q, _ in self.lots if bd + timedelta(days=n) <= d)


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    nav_series: list[tuple[date, float]] = field(default_factory=list)

    def apply_fill(self, f: Fill) -> None:
        pos = self.positions.setdefault(f.symbol, Position(f.symbol))
        if f.side == Side.BUY:
            total = pos.qty * pos.avg_cost + f.qty * f.price
            pos.qty += f.qty
            pos.avg_cost = total / pos.qty if pos.qty else 0.0
            pos.lots.append((f.trade_date, f.qty, f.price))
            self.cash -= f.qty * f.price + f.fee
        else:
            remain = f.qty
            new_lots = []
            for bd, q, p in pos.lots:
                if remain <= 0:
                    new_lots.append((bd, q, p))
                    continue
                take = min(q, remain)
                remain -= take
                if q - take > 1e-9:
                    new_lots.append((bd, q - take, p))
            pos.lots = new_lots
            pos.qty = max(pos.qty - f.qty, 0.0)
            self.cash += f.qty * f.price - f.fee

    def nav(self, prices: dict[str, float]) -> float:
        v = self.cash
        for sym, pos in self.positions.items():
            if pos.qty:
                v += pos.qty * prices.get(sym, pos.avg_cost)
        return v
