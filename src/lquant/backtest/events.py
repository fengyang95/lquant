"""事件与订单状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    qty: float
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_amount: float = 0.0
    fee: float = 0.0
    reason: str = ""
    # 清仓单允许卖出零股：A 股规则是「不足一手的零股必须一次性全部卖出」，
    # 若一律向下取整到整手，10 送 9 之后剩下的 11.11 股会永远卖不掉。
    allow_odd_lot: bool = False


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    trade_date: date


@dataclass
class Bar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: float
    amount: float
    adj_factor: float = 1.0
    halted: bool = False
    suspended: bool = False   # 停牌日：有承接报价但不可交易 → 拒单 reason="suspended"
    # 当日零成交（volume==0）但并未停牌：同样不可成交，但**不能**记成「停牌」——
    # 归因错误会让「为什么这单没成交」永远查不清。
    no_volume: bool = False
    # 当日是否 ST（逐日，来自日线湖的 is_st / baostock isST）。
    # None = 未知（数据缺列或为 null）→ 退回 InstrumentRules.is_st（security 表静态值）。
    # 做成逐日是因为 ST 会随戴帽/摘帽变化：静态值会让整段回测用同一个涨跌幅，
    # 摘帽后仍按 5% 处理、戴帽前就按 5% 处理，两个方向都错。
    is_st: bool | None = None
    fields: dict = field(default_factory=dict)
