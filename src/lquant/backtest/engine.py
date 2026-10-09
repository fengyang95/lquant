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
from lquant.backtest.rules.loader import default_slippage, load_ruleset
from lquant.backtest.rules.model import InstrumentRules, RuleSet
from lquant.backtest.security_meta import load_security_meta, merge_meta
from lquant.backtest.slippage import make_slippage
from lquant.backtest.strategy.base import Context, Strategy
from lquant.core.types import parse_symbol
from lquant.data.quality.tradability import no_limit_window, parse_listing_date

__all__ = ["EngineConfig", "BacktestResult", "Engine", "build_rules"]

# 上市日所在的候选列名：日线湖或调用方注入，命中即用（前者优先）
_LISTING_DATE_COLS = ("listing_date", "list_date")


def _listing_dates_from(df: pl.DataFrame, symbol_col: str) -> dict[str, date]:
    """df 里的上市日列 → {symbol: 上市日}；没有该列（或全空）时返回 {}。"""
    col = next((c for c in _LISTING_DATE_COLS if c in df.columns), None)
    if col is None:
        return {}
    out: dict[str, date] = {}
    for sym, raw in zip(df[symbol_col].to_list(), df[col].to_list(), strict=False):
        d = parse_listing_date(raw)
        if d is not None:
            out[str(sym)] = d
    return out


def _listing_dates_from_meta(meta: dict[str, dict] | None) -> dict[str, date]:
    """security 表元数据 → {symbol: 上市日}（list_date 或 listing_date）。"""
    out: dict[str, date] = {}
    for sym, m in (meta or {}).items():
        for key in _LISTING_DATE_COLS:
            d = parse_listing_date(m.get(key))
            if d is not None:
                out[str(sym)] = d
                break
    return out


def _no_price_limit_flags(symbols: list[str], dates: list[date],
                          listing_dates: dict[str, date]) -> list[bool | None]:
    """逐 (symbol, trade_date) 判定免涨跌停；None = 无法判定（退回静态值）。

    「第 N 个交易日」用**该标的自己的交易日序列**数：优先精确路径。
    但回测窗口常常晚于上市日，此时 df 内的序号并不等于真实的上市后序号，
    直接拿来用会把「上市第 100 个交易日」误当成第 1 个 → 精确路径只在
    数据窗口覆盖上市日（该标的在本 df 内的首个交易日 <= 上市日）时启用，
    否则退回日历天保守估算（可能多标，绝不漏标）。
    """
    if not listing_dates:
        return [None] * len(symbols)

    # 列可能是 Date / Datetime / str：统一成 date 再参与比较（parse 失败 → None）
    day_list = [parse_listing_date(d) for d in dates]

    seq: dict[str, list[date]] = {}
    for sym, d in zip(symbols, day_list, strict=False):
        if d is not None:
            seq.setdefault(sym, []).append(d)
    ranks: dict[str, dict[date, int]] = {}
    for sym, ds in seq.items():
        listing = listing_dates.get(sym)
        if listing is None:
            continue
        uniq = sorted(set(ds))
        if uniq and uniq[0] <= listing:
            ranks[sym] = {d: i + 1 for i, d in enumerate(uniq) if d >= listing}

    out: list[bool | None] = []
    for sym, d in zip(symbols, day_list, strict=False):
        listing = listing_dates.get(sym)
        if listing is None or d is None:
            out.append(None)                 # 无法判定 → 交给静态规则兜底
            continue
        verdict = no_limit_window(sym, listing, d, day_rank=ranks.get(sym, {}).get(d))
        # resolved=False（上市日非法）同样退化为 None：不猜，保守按有涨跌停处理
        out.append(verdict.no_limit if verdict.resolved else None)
    return out


def _annotate_no_price_limit(bars_by_day: dict[date, dict[str, Bar]],
                             listing_dates: dict[str, date]) -> None:
    """给已构建的 bar 逐日补 no_price_limit（只填 None，不覆盖已有判定）。

    调用方直接传 {日期: {代码: Bar}} 时走这里（prepare 的 DataFrame 路径
    在构建 Bar 时已算好）。已有非 None 值的来源是数据层的 no_price_limit 列，
    优先级高于按上市日现算，故不覆盖。
    """
    if not listing_dates:
        return
    # 一趟扫描同时收集：待判定的 (symbol, date) 与该 symbol 的交易日序列
    todo: dict[str, list[date]] = {}
    seqs: dict[str, list[date]] = {}
    for d in sorted(bars_by_day):
        for sym, bar in bars_by_day[d].items():
            if sym not in listing_dates:
                continue
            seqs.setdefault(sym, []).append(d)
            if bar.no_price_limit is None:
                todo.setdefault(sym, []).append(d)
    if not todo:
        return
    for sym, ds in todo.items():
        listing = listing_dates[sym]
        # 该标的的交易日序列用「本 dict 里它出现过的日期」近似：窗口覆盖上市日
        # 时精确，否则退回日历天保守估算（与 prepare 路径同一取舍）
        seq = sorted(set(seqs[sym]))
        ranks: dict[date, int] = {}
        if seq and seq[0] <= listing:
            ranks = {d: i + 1 for i, d in enumerate(seq) if d >= listing}
        for d in ds:
            verdict = no_limit_window(sym, listing, d, day_rank=ranks.get(d))
            if verdict.resolved:
                bars_by_day[d][sym].no_price_limit = verdict.no_limit


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
    # 退市残值率：退市股按最后可见收盘价 × 该比例一次性核销（0 = 全额损失，
    # 保守口径）。不核销会让退市持仓按最后收盘价永久冻结，长回测 NAV 虚高。
    delist_recovery: float = 0.0
    # 资金不足口径：reject（真实券商/backtrader）| truncate（聚宽 order_value）
    insufficient_cash: str = "reject"
    # 考核基准指数代码。None/"" = 不挂基准（只有绝对收益）。
    # 别名（hs300/沪深300/000300）会被 parse_benchmark 规范化；指数序列缺失时
    # 该回测**不报错**，只在 metrics 里标 benchmark_available=False。
    benchmark: str | None = "000300.SH"
    # 注入基准收盘序列 [(date, close)]，规避 DB 依赖（测试/离线回放）。
    # 给了它就不再查 index_daily；空列表 = 显式声明「没有基准」。
    benchmark_series: list[tuple[date, float]] | None = None


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
                meta: dict[str, dict] | None = None,
                *, with_db_meta: bool = True) -> dict[str, InstrumentRules]:
    """给每个标的生成撮合规则。

    meta 可覆盖 per-instrument 属性（sellable_after_days / fund_type / is_st /
    track_index_limit / no_price_limit）。缺省时从 security 表读 is_st、
    退市日、名称（名称用于推断 fund_type → ETF 的 T+0）——
    这样原生 Engine 路径与 JQ 路径拿到的是同一套 per-instrument 元数据，
    ST 股不再按 10% 涨跌停、黄金/QDII ETF 不再按 T+1。
    """
    rs = ruleset or load_ruleset()
    base_meta = load_security_meta() if with_db_meta else {}
    meta = merge_meta(base_meta, meta)
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
            track_index_limit=m.get("track_index_limit"),
            no_price_limit=m.get("no_price_limit", False),
            name=m.get("name"),
        )
    return out


class Engine:
    """逐日回测。策略只负责输出目标权重，成交细节全交给 Broker。"""

    def __init__(self, strategy: Strategy, ruleset: RuleSet | None = None,
                 config: EngineConfig | None = None,
                 slippage=None, meta: dict[str, dict] | None = None,
                 with_db_meta: bool = True) -> None:
        self.strategy = strategy
        self.ruleset = ruleset or load_ruleset()
        self.cfg = config or EngineConfig()
        # 滑点默认值来自规则表（cn_a_share.yaml 的 default.slippage），
        # 不再把成本假设硬编码在代码里；显式 slippage= 仍最高优先。
        if slippage is not None:
            self.slippage = slippage
        else:
            mode, params = default_slippage(self.ruleset)
            kind = self.cfg.slippage or mode
            # 只有「选中的模型 == 规则表配置的模型」时才继承规则表参数 ——
            # 否则会把 pct 的 rate 塞给 NoSlippage 之类的其它模型。
            merged = dict(self.cfg.slippage_params)
            if kind == mode:
                merged = {**params, **merged}
            self.slippage = make_slippage(kind, **merged)
        self._meta = meta or {}
        self._with_db_meta = with_db_meta
        self._rules: dict[str, InstrumentRules] = {}
        self._meta_resolved: dict[str, dict] = {}
        self.broker: Broker | None = None
        self.account = Account(cash=self.cfg.initial_cash)
        self._pending: list[Order] = []
        self._seq = 0
        self._last_rebal_key: str | None = None
        self._last_factor: dict[str, float] = {}
        self._date_index: dict[date, int] = {}
        self._delisted: set[str] = set()
        self._delist_schedule: dict[str, date] = {}

    # ---------- 数据准备 ----------

    @staticmethod
    def prepare(df: pl.DataFrame, *, date_col: str = "trade_date",
                symbol_col: str = "symbol",
                extra_fields: list[str] | None = None,
                listing_dates: dict[str, date] | None = None) -> dict[date, dict[str, Bar]]:
        """把长表转成 {日期: {代码: Bar}}。

        extra_fields 里的列会塞进 Bar.fields，供策略读取因子值 ——
        策略需要什么因子就传什么，引擎不关心语义。

        listing_dates 是该标的的上市日（symbol → date，通常来自 security 表），
        用于逐日判定「上市初期无涨跌幅窗口」。df 自带的 listing_date / list_date
        列优先；两处都没有时该列留 null（= 无法判定），由 rules 退回静态值。
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
        # 逐日 ST 标记：日线湖的 is_st 列（baostock isST）**逐日**给出真实戴帽状态。
        # 缺失/为 null 时保留 null（= 未知），由 rules 退回 security 表的静态默认值。
        if "is_st" not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_st"))
        else:
            df = df.with_columns(pl.col("is_st").cast(pl.Boolean, strict=False).alias("is_st"))
        # 逐日免涨跌停标记：与 is_st 同形，null = 未知 → 退回 InstrumentRules 静态值。
        # 数据层若已算好 no_price_limit 列则直接消费（优先级最高）；否则用上市日现算
        # （df 的 listing_date/list_date 列优先于调用方传入的 listing_dates）。
        if "no_price_limit" in df.columns:
            df = df.with_columns(
                pl.col("no_price_limit").cast(pl.Boolean, strict=False).alias("no_price_limit"))
        else:
            merged = dict(listing_dates or {})
            merged.update(_listing_dates_from(df, symbol_col))
            df = df.with_columns(pl.Series(
                "no_price_limit",
                _no_price_limit_flags(df[symbol_col].to_list(),
                                      df[date_col].to_list(), merged),
                dtype=pl.Boolean))
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
            (vol.is_not_null() & (vol == 0.0)
              & ~pl.col("is_suspended").fill_null(False)
              & ~pl.col("halted").fill_null(False)).alias("no_volume"),
        )

        out: dict[date, dict[str, Bar]] = {}
        cols = ["open", "high", "low", "close", "pre_close",
                "volume", "amount", "adj_factor", "halted", "suspended", "no_volume",
                "is_st", "no_price_limit"]
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
                    no_volume=bool(cvals["no_volume"][k]),
                    is_st=None if cvals["is_st"][k] is None else bool(cvals["is_st"][k]),
                    no_price_limit=(None if cvals["no_price_limit"][k] is None
                                    else bool(cvals["no_price_limit"][k])),
                    fields={c: fvals[c][k] for c in fields},
                )
            out[d] = bars
        return out

    # ---------- 主循环 ----------

    def run(self, data: pl.DataFrame | dict[date, dict[str, Bar]], **kw) -> BacktestResult:
        # 元数据先于 prepare 解析：上市日要注入逐日免涨跌停判定（prepare 参数）
        self._meta_resolved = merge_meta(
            load_security_meta() if self._with_db_meta else {}, self._meta)
        listing_dates = _listing_dates_from_meta(self._meta_resolved)
        if isinstance(data, dict):
            bars_by_day = data
            _annotate_no_price_limit(bars_by_day, listing_dates)
        else:
            kw.setdefault("listing_dates", listing_dates)
            bars_by_day = self.prepare(data, **kw)
        dates = sorted(bars_by_day)
        if not dates:
            return BacktestResult()

        symbols = sorted({s for b in bars_by_day.values() for s in b})
        self._rules = build_rules(symbols, self.ruleset, self._meta_resolved,
                                  with_db_meta=False)
        self.broker = Broker(self._rules, self.slippage, price_mode=self.cfg.price_mode,
                             insufficient_cash=self.cfg.insufficient_cash)
        self.account = Account(cash=self.cfg.initial_cash)
        self._last_factor = {}
        self._last_close: dict[str, float] = {}   # 每只股票最近一次有 bar 的 close
        # 交易日序号：T+N 可卖约束按**交易日**算，不是自然日
        self._date_index = {d: i for i, d in enumerate(dates)}
        self._delisted = set()
        # 退市计划预筛：只保留真正有退市日的标的，避免日循环遍历全量元数据
        self._delist_schedule = {
            s: m["delist_date"] for s, m in self._meta_resolved.items()
            if m.get("delist_date") is not None}
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
                # 停牌估值口径：记录每只股票最近一次有 bar 的 close。
                # close<=0 属脏数据（源数据错误/复权异常），记进去会把持仓
                # 估成 0 并可能以 0 价撮合 —— 只认正价，坏价视同无 bar。
                if b.close > 0:
                    self._last_close[s] = b.close

            # 0b) 退市核销：退市日当天把持仓按残值率变现，不再按最后收盘价冻结
            self._apply_delistings(d, res)

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

        关键：**挂单也要按同一比例调整，且与是否已持仓无关**。除权日新建仓时
        持仓还是空的，若把挂单调整嵌在「遍历持仓」循环里就永远不会执行 ——
        当日买入量会少买 1/ratio（ratio=1.3 时只建到 77% 仓位），
        日频调仓次日自愈，周频/月频策略会一直错。
        """
        assert self.broker is not None
        ratios: dict[str, float] = {}
        for sym, bar in bars.items():
            if bar.adj_factor <= 0:
                continue
            prev = self._last_factor.get(sym, bar.adj_factor)
            if prev <= 0:
                continue
            ratio = bar.adj_factor / prev
            if abs(ratio - 1.0) > 1e-12:
                ratios[sym] = ratio
        if not ratios:
            return
        for sym, ratio in ratios.items():
            self.account.apply_corporate_action(sym, ratio)
            # 待成交挂单同步按比例调整股数（券商对存量委托的除权调整语义）：
            # 送股后按旧股数撮合会偏离目标仓位，缩股则可能超额卖出。
            for o in self._pending:
                if o.symbol == sym:
                    o.qty *= ratio
                    o.filled_qty *= ratio

    def _is_delisted(self, sym: str, d: date) -> bool:
        """该标的是否已到退市日（到了就不可再买入）。"""
        m = self._meta_resolved.get(sym)
        if not m:
            return False
        delist = m.get("delist_date")
        return delist is not None and d >= delist

    def _apply_delistings(self, d: date, res: BacktestResult) -> None:
        """退市核销：退市日起把持仓按残值变现，避免按最后收盘价永久冻结。

        不做核销时，退市股会一直以最后可见收盘价计入 NAV —— 亏损头寸永远
        不实现，长区间回测的净值系统性虚高（幸存者偏差的另一种形态）。

        只遍历**有退市日**的标的（`_delist_schedule` 预筛）。若直接遍历全量
        元数据，5000 标的 × 千日会给日循环白加数百万次无用迭代。
        """
        if not self._delist_schedule:
            return
        for sym, delist in self._delist_schedule.items():
            if d < delist or sym in self._delisted:
                continue
            self._delisted.add(sym)
            pos = self.account.positions.get(sym)
            if pos is None or pos.qty <= 0:
                # 退市日尚无持仓：挂单**不在这里清理** —— 留给 _fill_pending 按
                # 「已退市」明确拒单并记账。静默丢弃挂单等于让策略少一笔委托。
                continue
            # 平仓后未成交的挂单作废（已无持仓可卖）
            self._pending = [o for o in self._pending if o.symbol != sym]
            px = self._last_close.get(sym) or pos.avg_cost
            proceeds = pos.qty * px * float(self.cfg.delist_recovery)
            self.account.cash += proceeds
            res.rejected.append((str(d), sym,
                                 f"退市核销 qty={pos.qty:.0f} 残值率={self.cfg.delist_recovery}"))
            pos.qty = 0.0
            pos.lots = []

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
            if self._is_delisted(order.symbol, d):
                # 退市后不可交易：否则会在已核销的标的上重新建仓
                order.status = OrderStatus.REJECTED
                order.reason = "已退市"
                res.rejected.append((str(d), order.symbol, order.reason))
                continue
            if bar.suspended:
                order.status = OrderStatus.REJECTED
                order.reason = "suspended"
                res.rejected.append((str(d), order.symbol, order.reason))
                continue
            if bar.halted:
                order.status = OrderStatus.REJECTED
                # 零成交与停牌都不可成交，但归因必须分开 —— 否则「为什么没成交」
                # 永远查不清（零成交是流动性问题，不是停牌）
                order.reason = "无成交量" if bar.no_volume else "停牌或无行情"
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
                      rules=self._rules, params=self.strategy.params,
                      date_index=self._date_index)
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

        def _plan_sell(sym: str, want: float, px: float) -> None:
            nonlocal planned_proceeds
            qty, odd_ok = self._sell_qty(sym, want, d)
            if qty > 0:
                orders.append(self._order(sym, Side.SELL, qty, allow_odd_lot=odd_ok))
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
                    _plan_sell(sym, -delta_value / px, px)
        # 清仓：不在买入目标里的持仓（含显式 w<=0 的清仓目标）
        buy_set = {s for s, _ in targets}
        for sym, pos in self.account.positions.items():
            if sym in buy_set or pos.qty <= 0:
                continue
            px = prices.get(sym)
            if px is None or px <= 0:
                continue
            _plan_sell(sym, pos.qty, px)
        # 后买：现金 + 卖出释放的预期资金（A 股卖出资金当日可用，
        # 与聚宽「先卖后买」撮合语义一致）。买与卖都在 T+1 开盘成交，
        # 两边按同一开盘价缩放，预估缺口只在「现金残余 × 跳空幅度」量级。
        planned_buys = 0.0        # 已排出的买单名义金额（顺序预留，防末位买单超资）
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
                # 每个买单只能用「资金池 - 已排出的买单」：此前各单独立对着
                # 全额池计算，Σ目标权重≈1 时实际成交额（含滑点+费用）必然
                # 超出 cash_buffer 余量，最后一个买单在 T+1 整单被拒
                # （insufficient_cash=reject），边际标的一个调仓周期欠配。
                cash = max((self.account.cash + planned_proceeds)
                           * (1 - self.cfg.cash_buffer) - planned_buys, 0.0)
                qty = min(delta_value, cash) / px
                qty = self._round_lot(sym, qty, floor=True)
                if qty > 0:
                    orders.append(self._order(sym, Side.BUY, qty))
                    planned_buys += qty * px

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
                if self._is_delisted(o.symbol, d):
                    o.status = OrderStatus.REJECTED
                    o.reason = "已退市"
                    res.rejected.append((str(d), o.symbol, o.reason))
                    continue
                if bar.suspended:
                    o.status = OrderStatus.REJECTED
                    o.reason = "suspended"
                    res.rejected.append((str(d), o.symbol, o.reason))
                    continue
                if bar.halted:
                    o.status = OrderStatus.REJECTED
                    o.reason = "无成交量" if bar.no_volume else "停牌或无行情"
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

    def _sell_qty(self, sym: str, want: float, d: date) -> tuple[float, bool]:
        """T+N 约束下能卖的最大数量，以及是否属于「清仓」（允许卖零股）。

        返回 (qty, allow_odd_lot)。A 股规则是零股必须一次性全部卖出，
        所以清仓时不做整手取整，否则 10 送 9 之后剩的 11.11 股会永远卖不掉。
        """
        pos = self.account.positions.get(sym)
        if pos is None or pos.qty <= 0:
            return 0.0, False
        rules = self._rules.get(sym)
        avail = (pos.available_at(d, rules, self._date_index) if rules
                 else pos.available_qty)
        qty = min(want, pos.qty, avail)
        if qty <= 0:
            return 0.0, False
        if qty >= pos.qty - 1e-9:
            # 全量卖出：零股一次性卖出，不取整
            return qty, True
        return self._round_lot(sym, qty, floor=True), False

    def _round_lot(self, sym: str, qty: float, floor: bool = True) -> float:
        lot = self._rules.get(sym).lot_size if sym in self._rules else 100
        if lot <= 1:
            return qty
        return (qty // lot) * lot if floor else math.ceil(qty / lot) * lot

    def _order(self, sym: str, side: Side, qty: float,
               allow_odd_lot: bool = False) -> Order:
        self._seq += 1
        return Order(order_id=f"o{self._seq}", symbol=sym, side=side, qty=float(qty),
                     allow_odd_lot=allow_odd_lot)

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
        res.metrics.update(self._benchmark_metrics(dates, rets))

    def _benchmark_metrics(self, dates: list[date], rets: list[float]) -> dict:
        """挂基准后的相对指标（总超额 / 跟踪误差 / 信息比率 / α / β）。

        基准口径：``parse_benchmark`` 规范化后的指数；``benchmark_series``
        注入时优先用它（离线/测试）。任何环节失败都降级成
        ``benchmark_available=False`` + ``benchmark_note``，**绝不让回测失败** ——
        主产物是净值，基准是可选的对照物。
        """
        from lquant.backtest.attribution import risk_vs_benchmark
        from lquant.backtest.benchmark import (
            benchmark_returns_by_date,
            parse_benchmark,
        )

        symbol = parse_benchmark(self.cfg.benchmark)
        if symbol is None:
            return {"benchmark": None, "benchmark_available": False,
                    "benchmark_note": "未配置基准（benchmark=None）"}
        if not dates or not rets:
            return {"benchmark": symbol, "benchmark_available": False,
                    "benchmark_note": "无净值序列，无法计算相对指标"}
        try:
            if self.cfg.benchmark_series is not None:
                series = list(self.cfg.benchmark_series)
                note_src = "injected"
            else:
                from lquant.backtest.benchmark import load_index_series

                series = load_index_series(symbol, start=dates[0], end=dates[-1])
                note_src = "index_daily"
            bmap = benchmark_returns_by_date(series)
        except Exception as e:  # noqa: BLE001  基准读取失败不阻断回测
            return {"benchmark": symbol, "benchmark_available": False,
                    "benchmark_note": f"{symbol}: 基准读取失败（{type(e).__name__}: {e}）"}
        port: list[float] = []
        bench: list[float] = []
        for d, r in zip(dates[1:], rets, strict=False):
            b = bmap.get(d)
            if b is None or r is None or not math.isfinite(float(r)):
                continue
            port.append(float(r))
            bench.append(b)
        base = {"benchmark": symbol, "benchmark_source": note_src,
                "benchmark_days": len(port)}
        if not series:
            base |= {"benchmark_available": False,
                     "benchmark_note": f"{symbol}: index_daily 无数据（跑 `lq data index`）"}
            return base
        if len(port) < 20:
            base |= {"benchmark_available": False,
                     "benchmark_note": f"{symbol}: 对齐后仅 {len(port)} 期（<20 不下结论）"}
            return base
        try:
            rel = risk_vs_benchmark(port, bench)
        except ValueError as e:
            base |= {"benchmark_available": False, "benchmark_note": str(e)}
            return base
        base |= {"benchmark_available": True, **rel}
        if len(port) < len(rets):
            base["benchmark_note"] = (
                f"{symbol}: 对齐 {len(port)}/{len(rets)} 期（缺口双边丢弃，不补造）")
        return base
