"""模拟盘引擎：信号生成 + 虚拟撮合 + 净值跟踪。

与回测引擎的关键差异（这是模拟盘存在的全部意义）：
- 回测用**历史全量日线**，撮合是确定性的、收盘价一锤定音
- 模拟盘用**实时/最新可得价格**，信号在盘中产生、价格滑移不可预知

所以模拟盘撮合走「事件驱动」：
1. `on_quote`：每次行情推送进来，检查挂单能否成交（含涨跌停拒单）
2. `on_day_close`：日终结算，更新净值、检查 T+N 可卖
3. 委托/成交全部可导出（orders_frame），可事后与回测对拍

费率规则复用 backtest.rules 的 RuleSet —— 同一套费率，
模拟盘与回测的成本模型才可比。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from lquant.backtest.rules.loader import load_ruleset
from lquant.core.types import now_cn, parse_symbol


@dataclass
class PaperConfig:
    initial_cash: float = 1_000_000.0
    slippage_pct: float = 0.001          # 模拟盘固定滑点（真实盘用盘口）
    max_positions: int = 10


@dataclass
class PaperPosition:
    symbol: str
    qty: int = 0
    avg_cost: float = 0.0
    available: int = 0                   # T+N：当日可卖数量
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price


@dataclass
class PaperOrder:
    order_id: str
    ts: datetime
    symbol: str
    side: str                            # buy / sell
    qty: int
    price: float
    status: str = "pending"              # pending / filled / rejected
    reason: str = ""
    filled_qty: int = 0
    filled_price: float = 0.0


class PaperBroker:
    """虚拟撮合器：挂单 → 行情触发 → 成交/拒单。

    拒单要给出明确 reason —— 模拟盘的价值就在于暴露
    「回测里想象不到的执行问题」：资金不足、T+N 不可卖、涨跌停堵单。
    """

    def __init__(self, config: PaperConfig | None = None) -> None:
        self.cfg = config or PaperConfig()
        self.cash = self.cfg.initial_cash
        self.positions: dict[str, PaperPosition] = {}
        self.orders: list[PaperOrder] = []
        self._seq = 0
        self._ruleset = load_ruleset()

    def submit(self, symbol: str, side: str, qty: int, price: float,
               ts: datetime | None = None) -> PaperOrder:
        """提交委托。数量/资金/可卖合法性在这里检查。"""
        self._seq += 1
        o = PaperOrder(order_id=f"P{self._seq:06d}", ts=ts or now_cn(),
                       symbol=symbol, side=side, qty=qty, price=price)
        if qty <= 0:
            o.status, o.reason = "rejected", "数量非法"
        elif side == "buy":
            need = qty * price * 1.001  # 粗估含费
            if need > self.cash:
                o.status, o.reason = "rejected", f"资金不足 need={need:.0f} cash={self.cash:.0f}"
        else:
            pos = self.positions.get(symbol)
            avail = pos.available if pos else 0
            # 已挂未成交卖单也占额度：两笔卖单合计不能超可卖，
            # 否则逐笔都合法、合计却卖穿成负持仓
            pending_sell = sum(
                o.qty for o in self.orders
                if o.symbol == symbol and o.side == "sell"
                and o.status == "pending"
            )
            if avail < qty + pending_sell:
                o.status, o.reason = "rejected", (
                    f"可卖不足 avail={avail} 已挂卖单={pending_sell} (T+N)")
        self.orders.append(o)
        return o

    def on_quote(self, symbol: str, price: float, limit_up: float | None = None,
                 limit_down: float | None = None, ts: datetime | None = None) -> list[PaperOrder]:
        """行情驱动：用最新价撮合该标的的挂单。返回本轮有变化的委托。"""
        touched: list[PaperOrder] = []
        for o in self.orders:
            if o.symbol != symbol or o.status != "pending":
                continue
            if o.side == "buy" and limit_up is not None and price >= limit_up:
                o.status, o.reason = "rejected", "涨停无法买入"
                touched.append(o)
                continue
            if o.side == "sell" and limit_down is not None and price <= limit_down:
                o.status, o.reason = "rejected", "跌停无法卖出"
                touched.append(o)
                continue
            self._fill(o, price, ts)
            touched.append(o)
        return touched

    def _fill(self, o: PaperOrder, px: float, ts: datetime | None) -> None:
        slip = px * self.cfg.slippage_pct
        price = px + slip if o.side == "buy" else px - slip
        o.filled_price = round(price, 4)
        o.filled_qty = o.qty

        rules = self._ruleset.for_symbol(o.symbol, parse_symbol(o.symbol).sec_type,
                                         parse_symbol(o.symbol).board)
        trade_date = ts.date() if ts else now_cn().date()
        if o.side == "buy":
            amount = o.qty * price
            fee = max(amount * rules.commission.rate, rules.commission.min) + \
                  amount * rules.transfer_fee_rate
            self.cash -= amount + fee
            pos = self.positions.setdefault(o.symbol, PaperPosition(symbol=o.symbol))
            total = pos.qty + o.qty
            pos.avg_cost = (pos.avg_cost * pos.qty + amount + fee) / total
            pos.qty = total
            # T+N：仅 T+0（n<=0，如 QDII/黄金/债券 ETF）买入当日即可卖；
            # n>=1 冻结，日终 on_day_close 解冻 → 次日起可卖（A 股 T+1）
            n = rules.sellable_after_days
            if n <= 0:
                pos.available += o.qty
            pos.last_price = price
        else:
            amount = o.qty * price
            fee = max(amount * rules.commission.rate, rules.commission.min) + \
                  amount * rules.transfer_fee_rate + \
                  amount * rules.tax_rate(trade_date)
            self.cash += amount - fee
            pos = self.positions[o.symbol]
            pos.qty -= o.qty
            pos.available -= o.qty
            if pos.qty == 0:
                pos.avg_cost = 0.0
            pos.last_price = price
        o.status = "filled"

    def on_day_close(self, d: date) -> None:
        """日终：解冻跨日买入的持仓（T+1 及以上）。"""
        for sym, pos in self.positions.items():
            rules = self._ruleset.for_symbol(sym, parse_symbol(sym).sec_type,
                                             parse_symbol(sym).board)
            if rules.sellable_after_days >= 1:
                pos.available = pos.qty
            if pos.last_price == 0:
                pos.last_price = pos.avg_cost

    def nav(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def positions_frame(self) -> pl.DataFrame:
        rows = [{"symbol": p.symbol, "qty": p.qty, "available": p.available,
                 "avg_cost": round(p.avg_cost, 4), "last_price": p.last_price,
                 "market_value": round(p.market_value, 2),
                 "pnl_pct": round(p.last_price / p.avg_cost - 1, 4) if p.avg_cost else 0.0}
                for p in self.positions.values() if p.qty > 0]
        return pl.DataFrame(rows)


class PaperEngine:
    """模拟盘主循环：策略信号 → 委托 → 撮合 → 净值序列。

    盘中模式：
        eng.push(quote)   # 每条行情推送调用一次

    离线回放模式（用日线验证链路，收盘价近似盘中价）：
        eng.replay(daily_df)
    """

    def __init__(self, strategy, config: PaperConfig | None = None) -> None:
        self.broker = PaperBroker(config)
        self.cfg = config or PaperConfig()
        self.strategy = strategy            # 须实现 signals(broker, quote) -> list[order dict]
        self.nav_series: list[dict] = []
        self.alerts: list[dict] = []

    def push(self, quote: dict) -> None:
        """单条行情推进：先让策略看行情产生委托，再用该行情撮合。"""
        orders = self.strategy.signals(self.broker, quote)
        for od in orders:
            self.broker.submit(od["symbol"], od["side"], od["qty"],
                               od.get("price", quote["price"]))
        self.broker.on_quote(quote["symbol"], quote["price"],
                             quote.get("limit_up"), quote.get("limit_down"))

    def replay(self, daily: pl.DataFrame) -> dict:
        """离线回放：逐日推进，验证模拟盘全链路。"""
        import math

        for d in sorted(daily["trade_date"].unique().to_list()):
            day_rows = daily.filter(pl.col("trade_date") == d)
            for row in day_rows.iter_rows(named=True):
                self.push({"symbol": row["symbol"], "price": row["close"],
                           "limit_up": row.get("limit_up"),
                           "limit_down": row.get("limit_down")})
            self.broker.on_day_close(d)
            nav = self.broker.nav()
            if not math.isfinite(nav):
                self.alerts.append({"date": str(d), "type": "nav_error"})
                nav = self.broker.cash
            self.nav_series.append({
                "trade_date": d, "nav": round(nav, 2),
                "cash": round(self.broker.cash, 2),
                "n_positions": sum(1 for p in self.broker.positions.values() if p.qty > 0),
            })
        return self.summary()

    def summary(self) -> dict:
        navs = [r["nav"] for r in self.nav_series]
        filled = [o for o in self.broker.orders if o.status == "filled"]
        rejected = [o for o in self.broker.orders if o.status == "rejected"]
        out = {
            "final_nav": navs[-1] if navs else self.cfg.initial_cash,
            "total_return": (navs[-1] / self.cfg.initial_cash - 1) if navs else 0.0,
            "n_orders": len(self.broker.orders),
            "n_filled": len(filled),
            "n_rejected": len(rejected),
            "rejections": [{"order_id": o.order_id, "symbol": o.symbol, "reason": o.reason}
                           for o in rejected[:10]],
        }
        if len(navs) > 1:
            import numpy as np
            rets = np.diff(navs) / np.asarray(navs[:-1])
            out["daily_vol"] = float(np.std(rets))
            peak, mdd = -np.inf, 0.0
            for v in navs:
                peak = max(peak, v)
                mdd = max(mdd, 1 - v / peak)
            out["max_drawdown"] = float(mdd)
        return out

    def orders_frame(self) -> pl.DataFrame:
        rows = [{"order_id": o.order_id, "ts": str(o.ts), "symbol": o.symbol,
                 "side": o.side, "qty": o.qty, "price": o.price,
                 "status": o.status, "reason": o.reason,
                 "filled_price": o.filled_price} for o in self.broker.orders]
        return pl.DataFrame(rows)
