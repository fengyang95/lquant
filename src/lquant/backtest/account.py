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

    def available_at(self, d: date, _rules: InstrumentRules,
                     date_index: dict[date, int] | None = None) -> float:
        """T+N：QDII / 黄金 / 债券 / 货币 ETF 是 0，当日可卖。

        N 是**交易日**，不是自然日 —— 自然日口径下 T+2 买入遇周末会提前可卖
        （周四买、周六就「到期」），T+5 之类的中长锁定更是系统性偏松。
        传入 date_index（交易日 → 序号）时按交易日算；不传则退回自然日口径
        （向后兼容，仅用于没有日历的单元测试）。
        """
        n = _rules.sellable_after_days
        if n <= 0:
            return self.qty
        if date_index is None:
            return sum(q for bd, q, _ in self.lots if bd + timedelta(days=n) <= d)
        di = date_index.get(d)
        if di is None:
            return sum(q for bd, q, _ in self.lots if bd + timedelta(days=n) <= d)
        return sum(q for bd, q, _ in self.lots
                   if di - date_index.get(bd, di) >= n)

    def apply_corporate_action(self, ratio: float) -> None:
        """除权调整：按复权因子比放大份额（分红默认再投资的份额调整法）。

        qty 放大 ratio 倍；avg_cost 反向缩放，保持「总成本 = qty × avg_cost」不变；
        lots 同步放大份额（买入价不变，总成本口径一致），买入日期保留 —— T+N 可卖约束不受影响。
        """
        self.qty *= ratio
        self.avg_cost /= ratio if ratio else 1.0
        self.lots = [(bd, q * ratio, p) for bd, q, p in self.lots]


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

    def apply_corporate_action(self, symbol: str, ratio: float) -> None:
        """除权：把 ratio 转发给对应持仓（无持仓则忽略）。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.apply_corporate_action(ratio)

    def nav(self, prices: dict[str, float],
            last_prices: dict[str, float] | None = None) -> float:
        """持仓估值：prices 优先 → last_prices（该股最近一次有 bar 的 close）→ avg_cost。

        停牌股当日无 bar 时 prices 不含该 symbol；avg_cost 回退会把浮盈浮亏
        抹平导致 NAV 失真，正确口径是最近可见收盘价。last_prices=None 时
        行为与旧版完全一致（向后兼容）。
        """
        v = self.cash
        for sym, pos in self.positions.items():
            if not pos.qty:
                continue
            px = prices.get(sym)
            if px is None and last_prices:
                px = last_prices.get(sym)
            v += pos.qty * (px if px is not None else pos.avg_cost)
        return v
