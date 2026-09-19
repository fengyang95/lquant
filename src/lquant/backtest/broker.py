"""撮合：涨跌停不可成交、T+N 可用持仓、费率按规则表、撮合模式决定成交价。

撮合模式（设计文档 5.3，防未来函数的核心）：
  next_open    T 日收盘生成信号 → T+1 开盘价成交（默认）
  next_vwap    T 日收盘        → T+1 VWAP（amount/volume）
  next_close   T 日收盘        → T+1 收盘价成交
  same_close   T 日收盘信号用 T 日收盘成交 —— 危险，仅研究用，显式警告

前三种都要求"推迟到下一根 bar 成交"；same_close 当日成交。
"""
from __future__ import annotations

import inspect
from datetime import date

from loguru import logger

from lquant.backtest.events import Bar, Fill, Order, OrderStatus, Side
from lquant.backtest.rules.model import InstrumentRules

MATCH_MODES = ("next_open", "next_vwap", "next_close", "same_close")


class Broker:
    def __init__(self, rules: dict[str, InstrumentRules], slippage=None,
                 price_mode: str = "next_open") -> None:
        if price_mode not in MATCH_MODES:
            raise ValueError(f"未知撮合模式 {price_mode!r}，可选: {MATCH_MODES}")
        self.rules = rules
        self.slippage = slippage
        self.price_mode = price_mode
        if price_mode == "same_close":
            logger.warning("撮合模式 same_close：用 T 日收盘价成交属未来函数，"
                           "仅研究对照用，勿用于实盘口径")
        # 滑点模型是否接受量参数（qty/volume）：内置模型统一四参签名，
        # 聚宽风格的 duck-type 对象（apply(price, side)）也能接入
        self._slip_takes_volume = False
        if slippage is not None and callable(getattr(slippage, "apply", None)):
            try:
                n_params = len([
                    p for p in inspect.signature(slippage.apply).parameters.values()
                    if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                ])
                self._slip_takes_volume = n_params >= 4
            except (TypeError, ValueError):
                self._slip_takes_volume = False
        # 按订单追踪：累计成交额 + 已付佣金
        self._cum_amount: dict[str, float] = {}
        self._paid_comm: dict[str, float] = {}

    def _price(self, bar: Bar, side: Side, qty: float = 0.0) -> float:
        if self.price_mode == "next_close":
            p = bar.close                 # T+1 收盘
        elif self.price_mode == "same_close":
            p = bar.close                 # T 日收盘（危险，已警告）
        elif self.price_mode == "next_vwap":
            p = (bar.amount / bar.volume) if bar.volume > 0 else bar.close  # T+1 VWAP
        else:                             # next_open（默认）
            p = bar.open                  # T+1 开盘
        if self.slippage is not None:
            if self._slip_takes_volume:
                p = self.slippage.apply(p, side, qty=qty, volume=bar.volume)
            else:
                p = self.slippage.apply(p, side)
        return p

    def match(self, order: Order, bar: Bar, d: date, max_qty: float | None = None,
              cash: float | None = None) -> Fill | None:
        r = self.rules.get(order.symbol)
        if r is None:
            order.status = OrderStatus.REJECTED
            order.reason = "无撮合规则"
            return None

        # 涨跌停不可成交（走 PriceLimit 规则：ST 5%，板块值 gem/star/bse 生效）
        limit = r.price_limit.for_symbol(r.symbol, r.symbol.board, is_st=r.is_st)
        if bar.halted:
            order.status = OrderStatus.REJECTED
            order.reason = "停牌"
            return None
        # 开盘价处的第一道筛查只对 next_open 有意义 —— next_close/next_vwap
        # 的成交价不是开盘价，误用 open 判断会在"平开收板"时放行（由下方
        # 实际成交价的最终校验兜底）。
        if self.price_mode == "next_open":
            if order.side == Side.BUY and bar.open >= bar.pre_close * (1 + limit) - 1e-9:
                order.status = OrderStatus.REJECTED
                order.reason = "涨停不可买"
                return None
            if order.side == Side.SELL and bar.open <= bar.pre_close * (1 - limit) + 1e-9:
                order.status = OrderStatus.REJECTED
                order.reason = "跌停不可卖"
                return None

        # 数量先按成交量/资金/一手约束截断，再算滑点 —— 冲击成本必须
        # 按实际成交数量计，不能给被截掉的部分付费（H1 教训）
        qty = self._max_qty(order, r, max_qty)
        if qty <= 0:
            order.status = OrderStatus.REJECTED
            order.reason = "数量不足一手或资金不足"
            return None

        price = self._price(bar, order.side, qty=qty)

        # 涨跌停不可成交的最终校验点：检查的是**实际成交价**，而非仅开盘价。
        # next_close/next_vwap 的成交价是收盘/VWAP —— 一只平开但收盘封板的票，
        # 在 next_close 里就会以涨停价成交，现实中买不进去。上一处 open 检查
        # 只拦「开盘即封板」，这里补齐其余撮合模式的成交价边界。
        # 市场约束（涨跌停）优先于资金约束判定。
        if order.side == Side.BUY and price >= bar.pre_close * (1 + limit) - 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "涨停不可买"
            return None
        if order.side == Side.SELL and price <= bar.pre_close * (1 - limit) + 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "跌停不可卖"
            return None

        # 资金充足性：买单成交额+费用不得超过可用现金，不足整单作废。
        # 换仓按 T 收盘价预估资金，T+1 跳空可能让实际所需超出可用资金 ——
        # 与 backtrader / 真实券商（开盘集合竞价资金不足废单）语义一致；
        # 绝不能让现金悄悄变负（隐性杠杆）。
        if order.side == Side.BUY and cash is not None:
            a = qty * price
            c = max(r.commission.min, a * r.commission.rate)
            if a + c + a * r.transfer_fee_rate > cash:
                order.status = OrderStatus.REJECTED
                order.reason = "资金不足"
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

    def _max_qty(self, order: Order, r: InstrumentRules,
                 max_qty: float | None = None) -> float:
        q = order.qty - order.filled_qty
        if max_qty is not None:
            q = min(q, max_qty)          # 成交量 / 资金约束
        return (q // r.lot_size) * r.lot_size
