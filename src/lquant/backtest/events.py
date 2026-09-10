"""事件与订单状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
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
    fields: dict = field(default_factory=dict)
