"""回测引擎：逐日事件循环。

防未来函数的核心在**成交时点**：
T 日收盘后策略看到 T 日的 bar（含 close），生成目标权重；
订单进入 pending，在 T+1 日**开盘价**撮合。
如果图省事用 T 日收盘价成交，等于假设你能预知收盘价 ——
回测收益会漂亮得离谱，实盘必然亏钱。

另一个容易漏的是调仓顺序：先卖后买。
A 股卖出资金当日可用于买入，反过来会误判资金不足而少买。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from lquant.backtest.account import Account
from lquant.backtest.broker import Broker
from lquant.backtest.events import Bar, Fill, Order, OrderStatus, Side
from lquant.backtest.metrics import perf_from_returns, turnover_from_trades
from lquant.backtest.rules.loader import load_ruleset
from lquant.backtest.rules.model import InstrumentRules, RuleSet
from lquant.backtest.slippage import make_slippage
from lquant.backtest.strategy.base import Context, Strategy
from lquant.core.types import parse_symbol

__all__ = ["EngineConfig", "BacktestResult", "Engine", "build_rules"]


@dataclass
class EngineConfig:
    initial_cash: float = 1_000_000.0
    rebalance: str = "daily"                # daily | weekly | monthly | none
    price_mode: str = "next_open"           # next_open|next_vwap|next_close|same_close（防未来函数）
    slippage: str = "pct"
    slippage_params: dict = field(default_factory=dict)
    participation: float = 0.1              # 单只最多吃掉当日成交量的比例
    max_position_weight: float = 1.0        # 单标的最大权重
    cash_buffer: float = 0.001              # 留一点现金，避免全额买满后无法扣费
    min_order_value: float = 1000.0         # 小于此金额的委托不下单


@dataclass
class BacktestResult:
    nav: list[tuple[date, float]] = field(default_factory=list)
    trades: list[Fill] = field(default_factory=list)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)   # (date, symbol, reason)
    metrics: dict = field(default_factory=dict)
    positions: dict[date, dict[str, float]] = field(default_factory=dict)

    @property
    def returns(self) -> list[float]:
        nav = [n for _, n in self.nav]
        return [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]

    def to_frame(self) -> pl.DataFrame:
        # 显式 Float64：未交易日的 nav 是 int，混入浮点后 Polars 严格模式会炸
        return pl.DataFrame({"trade_date": [d for d, _ in self.nav],
                             "nav": [float(n) for _, n in self.nav]},
                            schema_overrides={"nav": pl.Float64})

    def trades_frame(self) -> pl.DataFrame:
        return pl.DataFrame([{
            "trade_date": f.trade_date, "symbol": f.symbol, "side": f.side.value,
            "qty": f.qty, "price": f.price, "fee": f.fee,
            "amount": f.qty * f.price,
        } for f in self.trades], schema_overrides={"qty": pl.Float64, "amount": pl.Float64})


def build_rules(symbols: list[str], ruleset: RuleSet | None = None,
                meta: dict[str, dict] | None = None) -> dict[str, InstrumentRules]:
    """给每个标的生成撮合规则。

    meta 可覆盖 per-instrument 属性（sellable_after_days / fund_type / is_st）。
    """
    rs = ruleset or load_ruleset()
    meta = meta or {}
    out = {}
    for s in symbols:
        try:
            sym = parse_symbol(s)
        except ValueError:
            continue
        m = meta.get(s, {})
        out[s] = rs.for_symbol(
            str(sym), sym.sec_type, sym.board,
            is_st=m.get("is_st", False),
            fund_type=m.get("fund_type"),
            sellable_after_days=m.get("sellable_after_days"),
        )
    return out


class Engine:
    """逐日回测。策略只负责输出目标权重，成交细节全交给 Broker。"""

    def __init__(self, strategy: Strategy, ruleset: RuleSet | None = None,
                 config: EngineConfig | None = None,
                 slippage=None, meta: dict[str, dict] | None = None) -> None:
        self.strategy = strategy
        self.ruleset = ruleset or load_ruleset()
        self.cfg = config or EngineConfig()
        self.slippage = slippage or make_slippage(self.cfg.slippage,
                                                  **self.cfg.slippage_params)
        self._meta = meta or {}
        self._rules: dict[str, InstrumentRules] = {}
        self.broker: Broker | None = None
        self.account = Account(cash=self.cfg.initial_cash)
        self._pending: list[Order] = []
        self._seq = 0
        self._last_rebal_key: str | None = None
        self._last_factor: dict[str, float] = {}

    # ---------- 数据准备 ----------

    @staticmethod
    def prepare(df: pl.DataFrame, *, date_col: str = "trade_date",
                symbol_col: str = "symbol",
                extra_fields: list[str] | None = None) -> dict[date, dict[str, Bar]]:
        """把长表转成 {日期: {代码: Bar}}。

        extra_fields 里的列会塞进 Bar.fields，供策略读取因子值 ——
        策略需要什么因子就传什么，引擎不关心语义。
        """
        need = {date_col, symbol_col, "open", "high", "low", "close", "pre_close"}
        miss = need - set(df.columns)
        if miss:
            raise KeyError(f"回测数据缺少列: {sorted(miss)}")
        fields = [c for c in (extra_fields or []) if c in df.columns]

        # 缺失的可选列先补常量，后面统一走列式兜底（与旧逐行 `x or default` 语义一致）
        lits = {"volume": 0.0, "amount": 0.0, "adj_factor": 1.0, "halted": False}
        df = df.with_columns([pl.lit(v).alias(c) for c, v in lits.items() if c not in df.columns])
        if "is_suspended" not in df.columns:
            df = df.with_columns(pl.lit(False).alias("is_suspended"))
        vol = pl.col("volume")
        df = df.with_columns(
            pl.when(pl.col("pre_close").fill_null(0.0) == 0.0).then(pl.col("close"))
              .otherwise(pl.col("pre_close")).cast(pl.Float64).alias("pre_close"),
            vol.fill_null(0.0).cast(pl.Float64).alias("volume"),
            pl.col("amount").fill_null(0.0).cast(pl.Float64).alias("amount"),
            pl.when(pl.col("adj_factor").fill_null(0.0) == 0.0).then(1.0)
              .otherwise(pl.col("adj_factor")).cast(pl.Float64).alias("adj_factor"),
            (pl.col("is_suspended").fill_null(False)
             | (pl.col("halted").fill_null(False)
                | (vol.is_not_null() & (vol == 0.0)))).alias("halted"),
            pl.col("is_suspended").fill_null(False).alias("suspended"),
        )

        out: dict[date, dict[str, Bar]] = {}
        cols = ["open", "high", "low", "close", "pre_close",
                "volume", "amount", "adj_factor", "halted", "suspended"]
        for sub in df.sort([date_col, symbol_col]).partition_by(date_col, as_dict=False):
            d = sub[date_col][0]
            syms = sub[symbol_col].to_list()
            cvals = {c: sub[c].to_list() for c in cols}
            fvals = {c: sub[c].to_list() for c in fields}
            bars: dict[str, Bar] = {}
            for k, s in enumerate(syms):
                bars[s] = Bar(
                    symbol=s, trade_date=d,
                    open=float(cvals["open"][k]), high=float(cvals["high"][k]),
                    low=float(cvals["low"][k]), close=float(cvals["close"][k]),
                    pre_close=float(cvals["pre_close"][k]),
                    volume=float(cvals["volume"][k]), amount=float(cvals["amount"][k]),
                    adj_factor=float(cvals["adj_factor"][k]),
                    halted=bool(cvals["halted"][k]),
                    suspended=bool(cvals["suspended"][k]),
                    fields={c: fvals[c][k] for c in fields},
                )
            out[d] = bars
        return out

    # ---------- 主循环 ----------

    def run(self, data: pl.DataFrame | dict[date, dict[str, Bar]], **kw) -> BacktestResult:
        bars_by_day = data if isinstance(data, dict) else self.prepare(data, **kw)
        dates = sorted(bars_by_day)
        if not dates:
            return BacktestResult()

        symbols = sorted({s for b in bars_by_day.values() for s in b})
        self._rules = build_rules(symbols, self.ruleset, self._meta)
        self.broker = Broker(self._rules, self.slippage, price_mode=self.cfg.price_mode)
        self.account = Account(cash=self.cfg.initial_cash)
        self._last_factor = {}
        self._last_close: dict[str, float] = {}   # 每只股票最近一次有 bar 的 close
        # 多次 run() 必须从干净状态开始：调仓周期标记、订单序号与残留挂单都重置，
        # 否则第二次 run 会因周期 key 相同而跳过首个调仓日，或把上一次 run 的
        # PARTIAL 残单在首日撮合（结果静默错位）。
        self._seq = 0
        self._last_rebal_key = None
        self._pending = []

        res = BacktestResult()

        for i, d in enumerate(dates):
            bars = bars_by_day[d]

            # 0) 公司行为：除权日按复权因子比放大持仓份额（分红默认再投资的份额调整法）
            self._apply_corporate_actions(bars)
            for s, b in bars.items():
                if b.adj_factor > 0:
                    self._last_factor[s] = b.adj_factor
                # 停牌估值口径：记录每只股票最近一次有 bar 的 close
                self._last_close[s] = b.close

            # 1) 撮合上一日挂单（用今日开盘价，防未来函数）
            if self._pending:
                self._fill_pending(bars, d, res)

            # 2) 调仓：策略看今日收盘数据，下单到明日开盘
            if self._should_rebalance(d, i) and self.cfg.rebalance != "none":
                self._schedule_rebalance(bars, d, res)

            # 3) 按收盘价估值
            prices = {s: b.close for s, b in bars.items()}
            nav = self.account.nav(prices, self._last_close)
            if nav <= 0:
                # NAV 非正说明账目已出问题（现金不足扣费/杠杆漏洞），继续算收益率只会出 NaN
                raise ValueError(
                    f"{d} NAV={nav:.2f} ≤ 0，账户账目异常，请检查费率/资金约束"
                )
            res.nav.append((d, nav))
            res.positions[d] = {s: p.qty for s, p in self.account.positions.items() if p.qty}

        self._finalize(res)
        return res

    # ---------- 公司行为 ----------

    def _apply_corporate_actions(self, bars: dict[str, Bar]) -> None:
        """除权日调整持仓份额：ratio = 今日复权因子 / 昨日复权因子。

        数据层只提供后复权因子（无分红现金金额），因此采用份额调整法 ——
        等价于假设分红全部再投资。停牌日无 bar 不调整，复牌后按累计因子比一次性补齐。
        """
        assert self.broker is not None
        for sym, pos in self.account.positions.items():
            if pos.qty <= 0:
                continue
            bar = bars.get(sym)
            if bar is None:
                continue
            prev = self._last_factor.get(sym, bar.adj_factor)
            if prev <= 0 or bar.adj_factor <= 0:
                continue
            ratio = bar.adj_factor / prev
            if abs(ratio - 1.0) > 1e-12:
                self.account.apply_corporate_action(sym, ratio)
                # 待成交挂单同步按比例调整股数（券商对存量委托的除权调整语义）：
                # 送股后按旧股数撮合会偏离目标仓位，缩股则可能超额卖出。
                for o in self._pending:
                    if o.symbol == sym:
                        o.qty *= ratio
                        o.filled_qty *= ratio

    # ---------- 撮合 ----------

    def _fill_pending(self, bars: dict[str, Bar], d: date, res: BacktestResult) -> None:
        assert self.broker is not None
        remaining: list[Order] = []
        for order in self._pending:
            bar = bars.get(order.symbol)
            if bar is None:
                order.status = OrderStatus.REJECTED
                order.reason = "无行情"
                res.rejected.append((str(d), order.symbol, order.reason))
                continue
            if bar.suspended:
                order.status = OrderStatus.REJECTED
                order.reason = "suspended"
                res.rejected.append((str(d), order.symbol, order.reason))
                continue
            if bar.halted:
                order.status = OrderStatus.REJECTED
                order.reason = "停牌或无行情"
                res.rejected.append((str(d), order.symbol, order.reason))
                continue
            # 成交量约束：单只最多吃掉 participation 比例的当日成交量
            max_qty = bar.volume * self.cfg.participation if bar.volume > 0 else None
            # 买单带现金约束：跳高开时按可用资金截断，防现金变负（隐性杠杆）
            cash = self.account.cash if order.side == Side.BUY else None
            fill = self.broker.match(order, bar, d, max_qty=max_qty, cash=cash)
            if fill is None:
                res.rejected.append((str(d), order.symbol, order.reason or "未成交"))
                continue
            self.account.apply_fill(fill)
            res.trades.append(fill)
            if order.status is OrderStatus.PARTIAL:
                remaining.append(order)
        # PARTIAL 订单保留到下一交易日继续撮合剩余数量（真实订单簿语义）；
        # FILLED / REJECTED 直接出队。剩余量挂到回测结束为止自然作废。
        self._pending = remaining

    def _schedule_rebalance(self, bars: dict[str, Bar], d: date, res: BacktestResult) -> None:
        ctx = Context(account=self.account, trade_date=d,
                      rules=self._rules, params=self.strategy.params)
        raw = self.strategy.on_bar(ctx, bars) or []
        if not raw:
            # 空列表 = 无操作（保留持仓）。这是因子策略「当日无信号」的常态。
            return
        # 权重 <= 0 的显式目标 = 清仓该标的。择时策略（双均线/动量轮动）表达
        # 「空仓」的唯一途径 —— 若不支持，这类策略在信号转负时只能干瞪眼持仓。
        zero_out = {s for s, w in raw if s in self._rules and w <= 0}
        targets = [(s, w) for s, w in raw if s in self._rules and w > 0]
        if not targets and not zero_out:
            return

        prices = {s: b.close for s, b in bars.items()}
        nav = self.account.nav(prices, self._last_close)
        if nav <= 0:
            return

        # 权重语义：目标市值 / NAV 的**绝对占比**（残差留在现金），单标的上限封顶。
        # 不做归一化 —— 归一化会把「0.5 半仓」放大成「1.0 满仓」，直接破坏
        # 网格 / ATR 定仓这类绝对仓位策略（验证时由 backtrader 对账暴露）。
        # 因子 TopN 等权策略本身权重和恰为 1，两种语义下行为一致。
        if targets:
            targets = [(s, min(w, self.cfg.max_position_weight)) for s, w in targets]

        orders: list[Order] = []
        planned_proceeds = 0.0     # 本轮卖出在 T+1 释放的资金（按 T 收盘价预估）

        def _plan_sell(sym: str, qty: float, px: float) -> None:
            nonlocal planned_proceeds
            if qty > 0:
                orders.append(self._order(sym, Side.SELL, qty))
                planned_proceeds += qty * px

        # 先卖（减仓 + 清仓都必须在买单之前 —— 换仓时新买单依赖旧持仓的卖出资金）
        if targets:
            for sym, w in targets:
                pos = self.account.positions.get(sym)
                held = pos.qty if pos else 0.0
                px = prices.get(sym)
                if px is None or px <= 0:
                    continue
                want_value = nav * w
                have_value = held * px
                delta_value = want_value - have_value
                if abs(delta_value) < self.cfg.min_order_value:
                    continue
                if delta_value < 0:
                    _plan_sell(sym, self._sell_qty(sym, -delta_value / px, d), px)
        # 清仓：不在买入目标里的持仓（含显式 w<=0 的清仓目标）
        buy_set = {s for s, _ in targets}
        for sym, pos in self.account.positions.items():
            if sym in buy_set or pos.qty <= 0:
                continue
            px = prices.get(sym)
            if px is None or px <= 0:
                continue
            _plan_sell(sym, self._sell_qty(sym, pos.qty, d), px)
        # 后买：现金 + 卖出释放的预期资金（A 股卖出资金当日可用，
        # 与聚宽「先卖后买」撮合语义一致）。买与卖都在 T+1 开盘成交，
        # 两边按同一开盘价缩放，预估缺口只在「现金残余 × 跳空幅度」量级。
        if targets:
            for sym, w in targets:
                pos = self.account.positions.get(sym)
                held = pos.qty if pos else 0.0
                px = prices.get(sym)
                if px is None or px <= 0:
                    continue
                want_value = nav * w
                have_value = held * px
                delta_value = want_value - have_value
                if delta_value <= self.cfg.min_order_value:
                    continue
                cash = (self.account.cash + planned_proceeds) * (1 - self.cfg.cash_buffer)
                qty = min(delta_value, cash) / px
                qty = self._round_lot(sym, qty, floor=True)
                if qty > 0:
                    orders.append(self._order(sym, Side.BUY, qty))

        if self.cfg.price_mode in ("next_open", "next_vwap", "next_close"):
            # T 日收盘生成信号，推迟到 T+1 按对应成交价撮合 —— 防未来函数
            self._pending = orders
        else:
            # same_close：T 日收盘成交（危险，仅研究对照）。
            # 行为与 next_* 分支保持一致：同样的量约束与 rejected 记录。
            for o in orders:
                bar = bars.get(o.symbol)
                if bar is None:
                    o.status = OrderStatus.REJECTED
                    o.reason = "无行情"
                    res.rejected.append((str(d), o.symbol, o.reason))
                    continue
                if bar.suspended:
                    o.status = OrderStatus.REJECTED
                    o.reason = "suspended"
                    res.rejected.append((str(d), o.symbol, o.reason))
                    continue
                if bar.halted:
                    o.status = OrderStatus.REJECTED
                    o.reason = "停牌或无行情"
                    res.rejected.append((str(d), o.symbol, o.reason))
                    continue
                max_qty = bar.volume * self.cfg.participation if bar.volume > 0 else None
                f = self.broker.match(o, bar, d, max_qty=max_qty,
                                      cash=self.account.cash if o.side == Side.BUY else None)
                if f is None:
                    res.rejected.append((str(d), o.symbol, o.reason or "未成交"))
                else:
                    self.account.apply_fill(f)
                    res.trades.append(f)

    def _sell_qty(self, sym: str, want: float, d: date) -> float:
        """T+N 约束下能卖的最大数量。"""
        pos = self.account.positions.get(sym)
        if pos is None or pos.qty <= 0:
            return 0.0
        rules = self._rules.get(sym)
        avail = pos.available_at(d, rules) if rules else pos.available_qty
        qty = min(want, pos.qty, avail)
        return self._round_lot(sym, qty, floor=True)

    def _round_lot(self, sym: str, qty: float, floor: bool = True) -> float:
        lot = self._rules.get(sym).lot_size if sym in self._rules else 100
        if lot <= 1:
            return qty
        return (qty // lot) * lot if floor else math.ceil(qty / lot) * lot

    def _order(self, sym: str, side: Side, qty: float) -> Order:
        self._seq += 1
        return Order(order_id=f"o{self._seq}", symbol=sym, side=side, qty=float(qty))

    # ---------- 调仓频率 ----------

    def _should_rebalance(self, d: date, i: int) -> bool:
        mode = self.cfg.rebalance
        if mode == "daily":
            return True
        if mode == "weekly":
            key = f"{d.isocalendar().year}-{d.isocalendar().week}"
        elif mode == "monthly":
            key = f"{d.year}-{d.month}"
        else:
            return False
        if self._last_rebal_key == key:
            return False
        self._last_rebal_key = key
        return True

    # ---------- 收尾 ----------

    def _finalize(self, res: BacktestResult) -> None:
        rets = res.returns
        dates = [d for d, _ in res.nav]
        perf = perf_from_returns(rets, dates=dates[1:] if rets else None)
        perf.pop("nav", None)
        to = turnover_from_trades(
            [(f.trade_date, f.qty * f.price) for f in res.trades], nav=res.nav)
        res.metrics = {
            **perf,
            "initial_cash": self.cfg.initial_cash,
            "final_nav": res.nav[-1][1] if res.nav else self.cfg.initial_cash,
            "n_trades": len(res.trades),
            "n_rejected": len(res.rejected),
            "total_fee": sum(f.fee for f in res.trades),
            "turnover": to,
        }
