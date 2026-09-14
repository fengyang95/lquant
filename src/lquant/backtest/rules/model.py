"""规则模型。

三处设计修正（来自 akquant / rqalpha 源码）：
1. T+N 是 per-instrument 属性（sellable_after_days），不是按 sec_type
2. 印花税有生效区间（2023-08-28 从千一降到万五）
3. 最低佣金按订单累计，不是按成交
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lquant.core.errors import RuleNotFound
from lquant.core.types import Board, SecType, Symbol


@dataclass
class TaxSchedule:
    """按日期区间取税率。历史回测若统一用当前税率，2023 年前成本被系统性低估一半。"""

    schedule: list[tuple[date, date, float]]

    def rate_at(self, d: date) -> float:
        for s, e, r in self.schedule:
            if s <= d <= e:
                return r
        raise RuleNotFound(f"无匹配的税率区间: {d}")


@dataclass
class Commission:
    rate: float
    min: float
    per_order: bool = True      # 订单分多次成交，最低佣金只收一次


@dataclass
class PriceLimit:
    mode: str                    # by_board | by_track_index
    values: dict[str, float] = field(default_factory=dict)

    def for_symbol(self, sym: Symbol, board: Board, is_st: bool, track_index_limit: float | None = None) -> float:
        if self.mode == "by_track_index" and track_index_limit is not None:
            return track_index_limit
        if is_st:
            return self.values.get("st", 0.05)
        return self.values.get(board.value, self.values.get("main", 0.10))


@dataclass
class InstrumentRules:
    """per-instrument 覆盖，优先于类型默认值。"""

    symbol: Symbol
    sec_type: SecType
    commission: Commission
    tax: TaxSchedule
    transfer_fee_rate: float
    price_limit: PriceLimit
    lot_size: int
    sellable_after_days: int      # T+0 for QDII/黄金/债券/货币 ETF
    is_st: bool = False           # 涨跌停 5% 判定依据（PriceLimit.for_symbol）

    def tax_rate(self, d: date) -> float:
        return self.tax.rate_at(d)


@dataclass
class RuleSet:
    market: str
    currency: str
    default: dict
    etf: dict
    exceptions: dict

    def for_symbol(
        self,
        symbol: str,
        sec_type: SecType,
        board: Board,
        is_st: bool = False,
        fund_type: str | None = None,
        sellable_after_days: int | None = None,
        track_index_limit: float | None = None,
    ) -> InstrumentRules:
        base = self.etf if sec_type in (SecType.ETF, SecType.LOF) else self.default
        comm = base.get("commission", {})
        tax_sched = [
            (date.fromisoformat(x["from"]), date.fromisoformat(x["to"]), float(x["rate"]))
            for x in base.get("tax", {}).get("schedule", [])
        ] or [(date(2000, 1, 1), date(9999, 12, 31), float(base.get("tax", {}).get("rate", 0.0)))]

        t_plus = base.get("t_plus", 1)
        if isinstance(t_plus, dict):
            t_plus = t_plus.get(fund_type or "", t_plus.get("default", 1))
        if sellable_after_days is not None:
            t_plus = sellable_after_days      # per-instrument 优先

        return InstrumentRules(
            symbol=Symbol(*symbol.split(".")),
            sec_type=sec_type,
            commission=Commission(rate=comm.get("rate", 0.0), min=comm.get("min", 0.0),
                                  per_order=comm.get("per_order", True)),
            tax=TaxSchedule(tax_sched),
            transfer_fee_rate=base.get("transfer_fee", {}).get("rate", 0.0),
            price_limit=PriceLimit(base.get("price_limit", {}).get("mode", "by_board"),
                                   base.get("price_limit", {}).get("values", {})
                                   or {k: v for k, v in base.get("price_limit", {}).items()
                                       if isinstance(v, (int, float))}),
            lot_size=base.get("lot_size", 100),
            sellable_after_days=int(t_plus),
            is_st=is_st,
        )
