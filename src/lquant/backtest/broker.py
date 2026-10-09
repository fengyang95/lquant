"""撮合：涨跌停不可成交、T+N 可用持仓、费率按规则表、撮合模式决定成交价。

撮合模式（设计文档 5.3，防未来函数的核心）：
  next_open    T 日收盘生成信号 → T+1 开盘价成交（默认）
  next_vwap    T 日收盘        → T+1 VWAP（amount/volume）
  next_close   T 日收盘        → T+1 收盘价成交
  same_close   T 日收盘信号用 T 日收盘成交 —— 危险，仅研究用，显式警告

前三种都要求"推迟到下一根 bar 成交"；same_close 当日成交。

涨跌停价一律经 InstrumentRules.limit_up/limit_down 取**按 tick 取整**的挂牌价：
前收 3.63 的 10% 涨停价是 3.99（不是 3.993），拿 3.993 当阈值会把
「开盘即涨停」放行 —— 等于把根本买不进去的收益算进回测。
"""
from __future__ import annotations

import inspect
from datetime import date

from loguru import logger

from lquant.backtest.events import Bar, Fill, Order, OrderStatus, Side
from lquant.backtest.rules.model import InstrumentRules

MATCH_MODES = ("next_open", "next_vwap", "next_close", "same_close")

# 资金不足时的处理口径：
#   reject   —— 整单作废（真实券商开盘集合竞价语义 / backtrader 对账口径）
#   truncate —— 按可用资金截量成交（聚宽 order_value 语义）
INSUFFICIENT_CASH_MODES = ("reject", "truncate")


class Broker:
    def __init__(self, rules: dict[str, InstrumentRules], slippage=None,
                 price_mode: str = "next_open",
                 insufficient_cash: str = "reject") -> None:
        if price_mode not in MATCH_MODES:
            raise ValueError(f"未知撮合模式 {price_mode!r}，可选: {MATCH_MODES}")
        if insufficient_cash not in INSUFFICIENT_CASH_MODES:
            raise ValueError(f"未知资金不足口径 {insufficient_cash!r}，"
                             f"可选: {INSUFFICIENT_CASH_MODES}")
        self.rules = rules
        self.slippage = slippage
        self.price_mode = price_mode
        self.insufficient_cash = insufficient_cash
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

    def _base_price(self, bar: Bar) -> float:
        """不含滑点的成交基准价（撮合模式决定）。"""
        if self.price_mode in ("next_close", "same_close"):
            return bar.close                 # T+1 收盘 / T 日收盘（危险，已警告）
        if self.price_mode == "next_vwap":
            return (bar.amount / bar.volume) if bar.volume > 0 else bar.close
        return bar.open                      # next_open（默认）

    def _price(self, bar: Bar, side: Side, qty: float = 0.0) -> float:
        p = self._base_price(bar)
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

        # 涨跌停不可成交（PriceLimit 规则：ST 主板 5%，创业板/科创板 ST 20%，
        # 板块值 gem/star 20%、bse 30%，ETF 按跟踪指数）。取整到挂牌价。
        # is_st 用**当日** bar 上的真实戴帽状态（bar.is_st），None 才退回规则静态值。
        day_st = bar.is_st
        up = r.limit_up(bar.pre_close, is_st=day_st)
        down = r.limit_down(bar.pre_close, is_st=day_st)
        if bar.halted:
            order.status = OrderStatus.REJECTED
            order.reason = "停牌"
            return None
        # 开盘价处的第一道筛查只对 next_open 有意义 —— next_close/next_vwap
        # 的成交价不是开盘价，误用 open 判断会在"平开收板"时放行（由下方
        # 实际成交价的最终校验兜底）。
        if self.price_mode == "next_open":
            if up is not None and order.side == Side.BUY and bar.open >= up - 1e-9:
                order.status = OrderStatus.REJECTED
                order.reason = "涨停不可买"
                return None
            if down is not None and order.side == Side.SELL and bar.open <= down + 1e-9:
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
        if up is not None and order.side == Side.BUY and price >= up - 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "涨停不可买"
            return None
        if down is not None and order.side == Side.SELL and price <= down + 1e-9:
            order.status = OrderStatus.REJECTED
            order.reason = "跌停不可卖"
            return None

        # 限价单语义（Order.limit_price）：此前该字段从未被读取 —— 策略设了
        # 限价仍按市价成交，属静默错误。判定用**不含滑点**的基准价（是否可成交由
        # 市场决定），成交价则封顶/保底到限价（限价单绝不会成交在比限价更差的价位）。
        # 委托当日有效（A 股默认），不成交即作废，不挂到下一日。
        if order.limit_price is not None:
            lp = float(order.limit_price)
            base = self._base_price(bar)
            if order.side == Side.BUY:
                if base > lp + 1e-9:
                    order.status = OrderStatus.REJECTED
                    order.reason = f"限价未触及（基准价 {base:.4f} > 限价 {lp:.4f}）"
                    return None
                price = min(price, lp)
            else:
                if base < lp - 1e-9:
                    order.status = OrderStatus.REJECTED
                    order.reason = f"限价未触及（基准价 {base:.4f} < 限价 {lp:.4f}）"
                    return None
                price = max(price, lp)

        # 资金充足性：买单成交额+费用不得超过可用现金。
        # 费用含印花税（按当日税档）：cn_a_share.yaml 里 2008-09-19 前
        # side="both" 双边征收，买入同样有税 —— 预检漏掉这项时，早期回测
        # truncate 模式下实际扣款会超出预算（成交额约 0.1%~0.4%），
        # 现金变负 = 隐性杠杆。
        # reject（默认）：整单作废 —— 与 backtrader / 真实券商（开盘集合竞价
        #   资金不足废单）语义一致；绝不能让现金悄悄变负（隐性杠杆）。
        # truncate：按可用资金截量成交 —— 与聚宽 order_value 语义一致，
        #   由 JQRunner 显式选择，两条路径的口径差异因此是**显式配置**而非偶然。
        if order.side == Side.BUY and cash is not None:
            buy_cost = self._buy_cost(r, qty, price, d)
            if buy_cost > cash:
                if self.insufficient_cash == "reject":
                    order.status = OrderStatus.REJECTED
                    order.reason = "资金不足"
                    return None
                qty = self._affordable_qty(r, qty, price, cash, d)
                if qty <= 0:
                    order.status = OrderStatus.REJECTED
                    order.reason = "数量不足一手或资金不足"
                    return None
                # 截量后滑点/费用按新数量重算
                price = self._price(bar, order.side, qty=qty)
                if up is not None and price >= up - 1e-9:
                    order.status = OrderStatus.REJECTED
                    order.reason = "涨停不可买"
                    return None
                # 滑点随成交量变化的模型下，截量时的单价与重算单价可能不同，
                # 截量结果可能仍超预算 —— 按新价格再截一轮；两轮后仍超就整单
                # 作废：宁可拒单，绝不让现金透支。
                if self._buy_cost(r, qty, price, d) > cash:
                    qty = self._affordable_qty(r, qty, price, cash, d)
                    if qty <= 0:
                        order.status = OrderStatus.REJECTED
                        order.reason = "数量不足一手或资金不足"
                        return None
                    price = self._price(bar, order.side, qty=qty)
                    if self._buy_cost(r, qty, price, d) > cash:
                        order.status = OrderStatus.REJECTED
                        order.reason = "资金不足"
                        return None

        amount = qty * price
        transfer = amount * r.transfer_fee_rate_on(d)
        # 印花税按日期区间 + 方向取：2008-09-19 前双边征收，之后仅卖方
        tax = amount * r.tax_rate(d, order.side)

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

    def _buy_cost(self, r: InstrumentRules, qty: float, price: float,
                  d: date) -> float:
        """买单含费总额：成交额 + 佣金(含最低) + 印花税(当日税档) + 过户费(当日档)。

        预检与截量复查共用，保证「按多少现金封顶」和「实际扣多少」是同一套
        公式 —— 两处口径一旦漂移，truncate 模式就会静默透支。
        """
        a = qty * price
        c = max(r.commission.min, a * r.commission.rate)
        return a + c + a * (r.tax_rate(d, Side.BUY) + r.transfer_fee_rate_on(d))

    def _affordable_qty(self, r: InstrumentRules, qty: float, price: float,
                        cash: float, d: date) -> float:
        """资金不足时能买的最大数量（按含费口径反解，再按整手向下取整）。

        含费含税：印花税按当日税档取买入方向（2008-09-19 前双边征收），
        过户费同样按当日档取，与 account.apply_fill 的实际扣款公式一致。
        """
        if price <= 0:
            return 0.0
        # 反解：q*price*(1 + comm_rate + tax_rate + transfer) + min_comm <= cash
        tax = r.tax_rate(d, Side.BUY)
        unit = price * (1.0 + r.commission.rate + tax + r.transfer_fee_rate_on(d))
        budget = cash - r.commission.min
        q = min(qty, max(budget, 0.0) / unit)
        if q >= qty - 1e-9:              # 反解已够，不需要截量
            return qty
        return (q // r.lot_size) * r.lot_size if r.lot_size > 1 else q

    def _max_qty(self, order: Order, r: InstrumentRules,
                 max_qty: float | None = None) -> float:
        q = order.qty - order.filled_qty
        capped = False
        if max_qty is not None and q > max_qty:
            q = max_qty                   # 成交量约束
            capped = True
        if order.allow_odd_lot and not capped:
            # 清仓零股：允许一次性卖出非整手余量（A 股规则）
            return q
        if r.lot_size <= 1:
            return q
        return (q // r.lot_size) * r.lot_size
