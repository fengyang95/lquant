"""撮合费用 Python 参考实现 —— 与 Rust（crates/lq-backtest 的 match_order 单笔）对拍。

Rust `lq_backtest.match_order_py` 暴露的是**单笔一次性撮合**：给定成交量/价格与费率，
返回 (成交数量, 费用)。这里提供逐字段镜像的参考实现，供对拍测试锁两边的收费语义：
- 一手取整（lot_size 向下取整）；
- 卖出才收印花税（tax_rate），买入不收；
- 佣金 min 与 rate 取大，`commission_per_order` 固定为 true（Rust py 接口语义）；
- 过户费 transfer_fee_rate 按全额成交额计。

注：Python Broker.match 是面向「多元素分多次成交」的完整撮合（更广），
此参考只对齐 Rust 暴露的单笔接口 —— 两者恰好同费率的场景可在对拍里覆盖。
"""
from __future__ import annotations

import math


def match_order(  # noqa: PLR0917 镜像 Rust 9 参接口
    symbol: str,
    is_buy: bool,
    qty: float,
    price: float,
    commission_rate: float,
    commission_min: float,
    transfer_fee_rate: float,
    tax_rate: float,
    lot_size: float,
) -> tuple[float, float]:
    """单笔撮合：返回 (成交数量, 费用)。与 lq_backtest.match_order_py 对拍。"""
    del symbol  # 参考实现不含符号语义（与 Rust 相同，成交不限手数外的品种差异）
    lots = math.floor(qty / lot_size)
    filled = lots * lot_size
    if filled <= 0:
        return (0.0, 0.0)
    amount = filled * price
    transfer = amount * transfer_fee_rate
    tax = amount * tax_rate if not is_buy else 0.0
    commission = max(commission_min, amount * commission_rate)
    fee = commission + transfer + tax
    return (filled, fee)