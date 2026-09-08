"""撮合：涨跌停不可成交、T+N 可用持仓、费率按规则表。"""
from __future__ import annotations

from datetime import date

from lquant.backtest.events import Bar, Fill, Order, OrderStatus, Side
from lquant.backtest.rules.model import InstrumentRules


class Broker:
    def __init__(self, rules: dict[str, InstrumentRules], slippage=None) -> None:
        self.rules = rules
        self.slippage = slippage
        # 按订单追踪：累计成交额 + 已付佣金
        self._cum_amount: dict[str, float] = {}
        self._paid_comm: dict[str, float] = {}

    def _price(self, bar: Bar, side: Side) -> float:
        p = bar.open                      # 默认次日开盘价成交
        if self.slippage is not None:
            p = self.slippage.apply(p, side)
        return p

    def match(self, order: Order, bar: Bar, d: date, max_qty: float | None = None) -> Fill | None:
        r = self.rules.get(order.symbol)
        if r is None:
            order.status = OrderStatus.REJECTED
            order.reason = "无撮合规则"
            return None

        # 涨跌停不可成交
        limit = r.price_limit.values.get("main", 0.10)
        if bar.halted:
            order.status = OrderStatus.REJECTED
            order.reason = "停牌"
            return None
        if order.side == Side.BUY and bar.open >= bar.pre_close * (1 + limit) - 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "涨停不可买"
            return None
        if order.side == Side.SELL and bar.open <= bar.pre_close * (1 - limit) + 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "跌停不可卖"
            return None

        price = self._price(bar, order.side)
        qty = min(order.qty - order.filled_qty, self._max_qty(order, price, r, max_qty))
        if qty <= 0:
            order.status = OrderStatus.REJECTED
            order.reason = "数量不足一手或资金不足"
            return None

        amount = qty * price
        transfer = amount * r.transfer_fee_rate
        tax = amount * r.tax_rate(d) if order.side == Side.SELL else 0.0

        # 最低佣金按订单累计：整个订单的佣金 = max(min, 累计成交额 * rate)。
        # 分多次成交时，后续成交只补足差额（可能为零），绝不重复收 5 元。
        cum = self._cum_amount.get(order.order_id, 0.0) + amount
        target = max(r.commission.min, cum * r.commission.rate) if r.commission.per_order \
            else max(r.commission.min, amount * r.commission.rate)
        paid = self._paid_comm.get(order.order_id, 0.0)
        comm = max(target - paid, 0.0)
        self._cum_amount[order.order_id] = cum
        self._paid_comm[order.order_id] = paid + comm
        fee = comm + transfer + tax

        order.filled_qty += qty
        order.filled_amount += amount
        order.fee += fee
        order.status = OrderStatus.FILLED if order.filled_qty >= order.qty - 1e-9 else OrderStatus.PARTIAL
        return Fill(order.order_id, order.symbol, order.side, qty, price, fee, d)

    def _max_qty(self, order: Order, price: float, r: InstrumentRules,
                 max_qty: float | None = None) -> float:
        q = order.qty - order.filled_qty
        if max_qty is not None:
            q = min(q, max_qty)          # 成交量 / 资金约束
        return (q // r.lot_size) * r.lot_size
