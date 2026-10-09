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

from dataclasses import dataclass, field
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
    # T+N 冻结台账：[(剩余交易日数, 数量), ...]。按**交易日**递减（on_day_close
    # 每次调用 = 过一个交易日），而不是「n>=1 一律次日解冻」—— 后者会把
    # T+2/T+5 之类的中长锁定一律当成 T+1。
    frozen: list = field(default_factory=list)
    # 最近一次外部传入的逐日 ST 标记（None = 未知，由名称/规则表兜底）
    is_st: bool | None = None
    # 证券名称（来自行情快照）：用于推断 fund_type → T+0（黄金/QDII/债券/货币 ETF）。
    # 不存下来的话，规则表拿不到名称，这些 ETF 会被当成 T+1。
    name: str = ""

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
    # True=限价单（成交价不得越过 o.price）；False=市价单（o.price 只是下单
    # 时的参考报价，成交价 = 最新报价 ± 滑点）。此前不区分：service.tick 用
    # 「当前报价」下的默认单被 min/max 限价帽完整吞掉滑点，模拟盘与回测的
    # 成本模型系统性差 slippage_pct。
    limit: bool = True


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
        # 最近一次 day_close 的交易日（ISO 字符串）：日终幂等标记，
        # 重复 day_close 会把 T+N 冻结台账多减一天（T+1 当日即解冻）
        self.last_day_close: str | None = None

    def submit(self, symbol: str, side: str, qty: int, price: float,
               ts: datetime | None = None, *, is_st: bool | None = None,
               name: str | None = None, limit: bool = True) -> PaperOrder:
        """提交委托。数量/整手/资金/可卖合法性在这里检查。

        name / is_st 来自行情快照：名称用于推断 fund_type（决定 T+0/T+1），
        is_st 决定当日涨跌幅。二者都要落到持仓上，否则每次重算规则都会丢。
        limit=False 表示市价单（price 仅为参考报价，不构成限价帽）。
        """
        self._seq += 1
        o = PaperOrder(order_id=f"P{self._seq:06d}", ts=ts or now_cn(),
                       symbol=symbol, side=side, qty=qty, price=price,
                       limit=bool(limit))
        if is_st is not None or name:
            pos0 = self.positions.setdefault(symbol, PaperPosition(symbol=symbol))
            if is_st is not None:
                pos0.is_st = bool(is_st)
            if name:
                pos0.name = str(name)
        rules = self._rules(symbol)
        if qty <= 0:
            o.status, o.reason = "rejected", "数量非法"
        else:
            # A 股整手约束：与回测 Broker 同口径。买入必须整手；卖出只有
            # **一次性清仓**才允许零股（零股必须一次卖完）。此前模拟盘完全不查
            # 整手，能挂 1 股买单 —— 回测/模拟盘数量口径直接不可比。
            lot = rules.lot_size
            if lot > 1 and side == "buy":
                qty = (qty // lot) * lot
                o.qty = qty
            elif lot > 1 and side == "sell":
                held = self.positions.get(symbol)
                if held is None or qty < held.qty - 1e-9:
                    qty = (qty // lot) * lot
                    o.qty = qty
            if qty <= 0:
                o.status, o.reason = "rejected", "数量不足一手"
        if o.status == "rejected":
            self.orders.append(o)
            return o
        if side == "buy":
            need = self._buy_need(symbol, qty, price)
            # 已挂未成交买单也占资金（与卖侧 pending_sell 同理）：
            # 两笔 90 万挂单在 100 万现金下各自合法、合计成交即穿仓
            pending_buy = sum(
                self._buy_need(b.symbol, b.qty, b.price) for b in self.orders
                if b.symbol == symbol and b.side == "buy"
                and b.status == "pending"
            )
            if need + pending_buy > self.cash:
                o.status, o.reason = "rejected", (
                    f"资金不足 need={need:.0f} 已挂买单占={pending_buy:.0f} "
                    f"cash={self.cash:.0f}")
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

    def _rules(self, symbol: str):
        """当日撮合规则：ST 以持仓/最近行情带进来的逐日标记为准，名称用于
        推断 fund_type（黄金/QDII/债券/货币 ETF → T+0）。"""
        sym = parse_symbol(symbol)
        pos = self.positions.get(symbol)
        return self._ruleset.for_symbol(
            symbol, sym.sec_type, sym.board,
            is_st=bool(pos.is_st) if pos and pos.is_st else False,
            name=pos.name if pos else None)

    def apply_corporate_action(self, symbol: str, ratio: float) -> None:
        """除权调整：按复权因子比放大份额（与回测 Engine 同一套份额调整法）。

        模拟盘此前**完全没有**公司行为处理 —— 持仓跨除权日时份额不调整，
        股价除权后净值凭空少一截，与回测对账必然对不上。
        """
        if ratio <= 0 or abs(ratio - 1.0) <= 1e-12:
            return
        pos = self.positions.get(symbol)
        if pos is None or pos.qty <= 0:
            return
        pos.qty = int(round(pos.qty * ratio))
        pos.available = int(round(pos.available * ratio))
        pos.avg_cost = pos.avg_cost / ratio if ratio else pos.avg_cost
        # 冻结台账同步按比例放大（送股后剩余冻结份额也要按新股数计）
        pos.frozen = [[n, int(round(q * ratio))] for n, q in pos.frozen]

    def _buy_need(self, symbol: str, qty: int, price: float) -> float:
        """买入所需资金 = 金额 + 真实费率估算（含佣金最低额）+ 滑点余量。

        原来 0.1% 粗估在小单上低于佣金最低额（如 ¥5），成交后现金穿仓。
        """
        amount = qty * price
        rules = self._ruleset.for_symbol(symbol, parse_symbol(symbol).sec_type,
                                         parse_symbol(symbol).board)
        # 过户费这里用**现行常数**（transfer_fee_rate）而不是当日区间值：
        # 本函数只做建仓资金预估，没有交易日上下文；真实扣费在 _fill 里按
        # transfer_fee_rate_on(trade_date) 逐日取，历史区间不会在这里失真。
        fee = max(amount * rules.commission.rate, rules.commission.min) + \
            amount * rules.transfer_fee_rate
        slip = amount * self.cfg.slippage_pct
        return amount + fee + slip

    def on_quote(self, symbol: str, price: float, limit_up: float | None = None,
                 limit_down: float | None = None, ts: datetime | None = None) -> list[PaperOrder]:
        """行情驱动：用最新价撮合该标的的挂单。返回本轮有变化的委托。

        限价约束：买单只在 price <= limit、卖单只在 price >= limit 时成交，
        否则保持 pending —— 限价单才有「限」的语义（以前只记录不检查）。
        """
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
            # 限价未触达 → 继续挂单等待（不是拒单）。
            # 市价单不受此限：o.price 只是下单时的参考报价，报价上行不应
            # 让市价买单永远挂着（真实市价单按对手价立即成交）。
            if o.limit:
                if o.side == "buy" and price > o.price:
                    continue
                if o.side == "sell" and price < o.price:
                    continue
            self._fill(o, price, ts)
            touched.append(o)
        return touched

    def _fill(self, o: PaperOrder, px: float, ts: datetime | None) -> None:
        slip = px * self.cfg.slippage_pct
        if not o.limit:
            # 市价单：成交价 = 最新报价 ± 滑点（此前被 min/max 限价帽完整
            # 吞掉 —— 报价下默认单的 o.price 就是当前报价，滑点恒为 0，
            # 模拟盘与回测对账出现系统性 5bp/边偏差）
            price = px + slip if o.side == "buy" else px - slip
        else:
            # 限价单：成交价不得越过限价（买价压到限价内、卖价抬到限价上），
            # 否则 quote==limit 时滑点会击穿刚检查过的限价约束
            price = min(px + slip, o.price) if o.side == "buy" \
                else max(px - slip, o.price)
        o.filled_price = round(price, 4)
        o.filled_qty = o.qty

        rules = self._rules(o.symbol)
        trade_date = ts.date() if ts else now_cn().date()
        if o.side == "buy":
            amount = o.qty * price
            # 印花税按方向取（2008-09-19 前双边都收）—— 与回测 Broker 同口径
            fee = max(amount * rules.commission.rate, rules.commission.min) + \
                  amount * rules.transfer_fee_rate_on(trade_date) + \
                  amount * rules.tax_rate(trade_date, "buy")
            self.cash -= amount + fee
            pos = self.positions.setdefault(o.symbol, PaperPosition(symbol=o.symbol))
            total = pos.qty + o.qty
            pos.avg_cost = (pos.avg_cost * pos.qty + amount + fee) / total
            pos.qty = total
            # T+N：n<=0（QDII/黄金/债券 ETF）当日可卖；n>=1 按**交易日**冻结 n 天，
            # 由 on_day_close 逐日递减（不是「一律次日解冻」）
            n = rules.sellable_after_days
            if n <= 0:
                pos.available += o.qty
            else:
                pos.frozen.append([n, o.qty])
            pos.last_price = price
        else:
            amount = o.qty * price
            fee = max(amount * rules.commission.rate, rules.commission.min) + \
                  amount * rules.transfer_fee_rate_on(trade_date) + \
                  amount * rules.tax_rate(trade_date, "sell")
            self.cash += amount - fee
            pos = self.positions[o.symbol]
            pos.qty -= o.qty
            pos.available -= o.qty
            if pos.qty == 0:
                pos.avg_cost = 0.0
            pos.last_price = price
        o.status = "filled"

    def on_day_close(self, d: date) -> None:
        """日终：按交易日释放 T+N 冻结份额。

        冻结台账是 `[[剩余交易日, 数量]]`，每个交易日递减 1，归零才转入 available。
        （旧实现是「sellable_after_days>=1 就 available=qty-pending_sell」——
        等于把 T+2/T+5 一律当成 T+1，且会把已有可卖份额重算掉。）

        挂卖单占用无需在这里扣：submit 的可卖校验是
        `avail < qty + pending_sell` 的加性判断，挂单从不减少 available。
        """
        for pos in self.positions.values():
            released = 0
            rest: list = []
            for n, q in pos.frozen:
                if n - 1 <= 0:
                    released += q
                else:
                    rest.append([n - 1, q])
            pos.frozen = rest
            if released:
                pos.available += released
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
        """单条行情推进：策略出委托 → 撮合 → 盯市。

        quote 里的 name/is_st 必须透传给 submit —— 名称决定 ETF 的 T+0/T+1，
        is_st 决定当日涨跌幅；丢了就退化成「一律 T+1 + 非 ST」。

        盯市（last_price 刷新）不能省：`nav()` 用 last_price 估值，不刷新的话
        持仓只有成交那天有价，**净值曲线会一直停在建仓当天的水平**。
        service.tick 一直在做这件事，但离线回放（replay）此前漏了 ——
        于是「回测 vs 模拟盘对账」在 replay 路径上根本对不起来。
        """
        orders = self.strategy.signals(self.broker, quote)
        for od in orders:
            has_px = od.get("price") is not None
            self.broker.submit(
                od["symbol"], od["side"], od["qty"],
                od["price"] if has_px else quote["price"],
                name=quote.get("name") or od.get("name"),
                is_st=quote.get("is_st"),
                limit=has_px)
        self.broker.on_quote(quote["symbol"], quote["price"],
                             quote.get("limit_up"), quote.get("limit_down"))
        pos = self.broker.positions.get(quote["symbol"])
        if pos is not None and pos.qty > 0 and quote.get("price"):
            pos.last_price = float(quote["price"])

    def replay(self, daily: pl.DataFrame) -> dict:
        """离线回放：逐日推进，验证模拟盘全链路。"""
        import math

        for d in sorted(daily["trade_date"].unique().to_list()):
            day_rows = daily.filter(pl.col("trade_date") == d)
            for row in day_rows.iter_rows(named=True):
                self.push({"symbol": row["symbol"], "price": row["close"],
                           "limit_up": row.get("limit_up"),
                           "limit_down": row.get("limit_down"),
                           "name": row.get("name"),
                           "is_st": row.get("is_st")})
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
            # ddof=1（样本口径）：与 backtest.metrics 的波动率估计一致，
            # 此前总体口径在样本少时低估波动
            out["daily_vol"] = float(np.std(rets, ddof=1))
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
