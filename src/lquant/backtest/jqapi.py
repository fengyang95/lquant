"""聚宽（JoinQuant）兼容策略 API：用户直接写聚宽风格代码回测。

用法（POST /api/backtests/run-code 的 code 字段）：

    def initialize(context):
        set_benchmark('000300.SH')
        set_order_cost(type='stock', open_tax=0, close_tax=0.0005,
                       open_commission=0.0003, close_commission=0.0003,
                       min_commission=5)
        run_monthly(rebalance, monthday=1, time='open')

    def rebalance(context):
        for s in ['600519.SH', '000001.SZ']:
            order_target_value(s, context.portfolio.total_value / 2)

兼容面（日频子集）：
    生命周期   initialize / handle_data / before_trading_start / after_trading_end
               run_daily / run_weekly / run_monthly
    采集       record(**kv)（每日每键一条 (trade_date, value) 自定义曲线）
    设置       set_benchmark / set_option / set_order_cost / set_slippage / set_universe
    下单       order / order_value / order_target / order_target_value / cancel_order
    行情       get_price / history / attribute_history / get_current_data
    上下文     context.portfolio(total_value/available_cash/positions/returns...)
               context.current_dt / context.previous_date
    其他       log.info / FixedSlippage / PriceRelatedSlippage

**与聚宽的两处刻意差异**（在我们引擎口径下更保守）：
1. 成交价：'open' 时点的委托按当日开盘价撮合（与聚宽一致），
   但涨跌停拒单、T+1 可卖、按订单累计最低佣金全部走本引擎规则表；
2. history/attribute_history 严格截到**上一交易日**，杜绝未来函数。

安全边界：exec 执行用户代码是本功能的设计前提（同聚宽/本地研究环境）。
防护分三层，逐层收口：
1. 静态校验 ``validate_source``：import 白名单 + 危险调用/属性黑名单；
2. 执行侧受限内建 ``sandbox.safe_builtins()``：显式覆盖 CPython 自动注入的完整
   内建，命名空间里没有 ``eval/exec/open/getattr``；``__import__`` 换成
   只放行白名单模块的守卫版；
3. 服务默认只绑 ``127.0.0.1``（``lquant.sh`` 的 ``LQ_API_HOST``）。

**这三层都不是容器级隔离**——exec 下通过对象图逃逸在理论上始终可达。
不要把本服务暴露到不可信网络。见 ``docs/SECURITY.md``。
"""
from __future__ import annotations

import math
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime

import polars as pl

from lquant.backtest.account import Account
from lquant.backtest.broker import Broker
from lquant.backtest.engine import Engine, build_rules
from lquant.backtest.events import Bar, Fill, Order, Side
from lquant.backtest.jq_fundamentals import JQFundamentalsState
from lquant.backtest.metrics import perf_from_returns, turnover_from_trades
from lquant.backtest.rules.loader import default_slippage
from lquant.backtest.sandbox import run_with_deadline, safe_builtins
from lquant.backtest.security_meta import load_security_meta, merge_meta
from lquant.backtest.slippage import make_slippage
from lquant.core.types import parse_symbol, today_cn
from lquant.factors.panel import compute_factor_columns

__all__ = ["JQRunner", "JQResult"]

# 复权只作用于价格类字段；成交量/成交额/换手率不随复权因子缩放
_FQ_PRICE_FIELDS = frozenset({"open", "high", "low", "close", "pre_close", "avg"})
_FQ_MODES = (None, "none", "pre", "post")

# run_daily 允许的语义化时点（其余必须能解析成合法 'HH:MM'）
_RUN_DAILY_KEYWORDS = frozenset({"every_bar", "open", "close", "after_close",
                                 "before_open"})


def _parse_clock(t: str) -> int:
    """把 'HH:MM' 解析成当日分钟数；非法时刻抛错（不静默兜底）。"""
    hh, sep, mm = t.partition(":")
    if not sep:
        raise ValueError(
            f"run_daily time 非法: {t!r}（可选 every_bar/open/close/after_close "
            "或 'HH:MM' 时刻）")
    try:
        h, m = int(hh), int(mm)
    except ValueError as e:
        raise ValueError(f"run_daily time 非法: {t!r}（时刻需为 'HH:MM'）") from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"run_daily time 非法: {t!r}（时刻需在 00:00~23:59）")
    return h * 60 + m


# ---------- 结果 ----------

@dataclass
class JQResult:
    nav: list[tuple[date, float]] = field(default_factory=list)
    trades: list[Fill] = field(default_factory=list)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)
    positions: dict[date, dict[str, float]] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    records: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    error: str | None = None

    def to_frame(self) -> pl.DataFrame:
        return pl.DataFrame({"trade_date": [d for d, _ in self.nav],
                             "nav": [float(n) for _, n in self.nav]},
                            schema_overrides={"nav": pl.Float64})

    def trades_frame(self) -> pl.DataFrame:
        return pl.DataFrame([{
            "trade_date": f.trade_date, "symbol": f.symbol, "side": f.side.value,
            "qty": f.qty, "price": f.price, "fee": f.fee, "amount": f.qty * f.price,
        } for f in self.trades], schema_overrides={"qty": pl.Float64, "amount": pl.Float64})


# ---------- pandas 兼容层 ----------

def _build_jq_pandas():
    """聚宽沙箱用老版 pandas：``s[-1]`` / ``s[-5:]`` 等负数下标按**位置**解释。

    新版 pandas 对 DatetimeIndex 的 int 键（标量或切片端点）一律按标签查，
    社区策略的主流写法 ``attribute_history(...)['close'][-1]``、``history(5)[-5:]``
    会直接 KeyError/TypeError。这里用 Series/DataFrame 子类恢复老语义：
    负 int 标量与含 int 端点的切片 → iloc，其余行为不变。
    """
    import pandas as pd

    def _is_int_slice(key):
        return (isinstance(key, slice)
                and any(isinstance(b, int) for b in (key.start, key.stop)))

    class _JQSeries(pd.Series):
        @property
        def _constructor(self):
            return _JQSeries

        def __getitem__(self, key):
            # 老 pandas：非整数索引下，不在索引里的 int 键一律回退按位置取
            if isinstance(key, int) and key not in self.index:
                return self.iloc[key]
            if _is_int_slice(key):
                return self.iloc[key]
            return super().__getitem__(key)

    class _JQFrame(pd.DataFrame):
        @property
        def _constructor(self):
            return _JQFrame

        @property
        def _constructor_sliced(self):
            return _JQSeries

        def __getitem__(self, key):
            if _is_int_slice(key):
                return self.iloc[key]
            return super().__getitem__(key)

    return _JQFrame


try:
    _JQFrame = _build_jq_pandas()
except ImportError:                            # 无 pandas 环境：历史接口回退 dict/list
    _JQFrame = None


# ---------- 用户可见的辅助类型 ----------

class FixedSlippage:
    """固定价差滑点（聚宽同名类）。传入的是「总价差」一半上下各滑。"""

    def __init__(self, delta: float = 0.0) -> None:
        self.delta = float(delta) / 2.0

    def apply(self, price: float, side: Side) -> float:
        return price + self.delta if side == Side.BUY else max(price - self.delta, 0.0001)


class PriceRelatedSlippage:
    """比例滑点（聚宽同名类），rate 默认 0.0024。"""

    def __init__(self, rate: float = 0.0024) -> None:
        self.rate = float(rate) / 2.0

    def apply(self, price: float, side: Side) -> float:
        return price * (1 + self.rate) if side == Side.BUY else price * (1 - self.rate)


@dataclass(frozen=True)
class MarketOrder:
    """聚宽 order(..., style=MarketOrder())：市价单（默认行为）。"""

    price: float | None = None


@dataclass(frozen=True)
class LimitOrder:
    """聚宽 order(..., style=LimitOrder(price))：限价单。

    此前沙箱里没有这两个 style 对象（`order(s, n, style=LimitOrder(p))` 直接
    NameError），且 `Order.limit_price` 从未被撮合读取 —— 设了限价仍按市价成交。
    """

    price: float

    @property
    def limit_price(self) -> float:
        return float(self.price)


def _style_limit(style) -> float | None:
    """从聚宽 style 对象取限价；市价/None → None。"""
    if style is None:
        return None
    lp = getattr(style, "limit_price", None)
    if lp is None:
        return None
    try:
        return float(lp)
    except (TypeError, ValueError):
        return None


class _SecData:
    """get_current_data()[sym] 的返回：当日参考行情。"""
    __slots__ = ("paused", "is_st", "name", "day_open", "last_price",
                 "high_limit", "low_limit")

    def __init__(self, bar: Bar | None, ref: float, is_st: bool = False, name: str = "",
                 rules=None):
        if bar is None:
            self.last_price = float("nan")
            self.day_open = float("nan")
            self.paused = True
            self.high_limit = self.low_limit = float("nan")
        else:
            self.day_open = bar.open
            self.last_price = ref
            self.paused = bar.halted
            base = bar.pre_close or bar.close
            # 涨跌停价必须与撮合口径同源（含 tick 取整与 ST 分板规则）：
            # 此前硬编码 ±10%，ST 股显示 11.0/9.0 而撮合实际按 10.5/9.5 拒单，
            # 策略看到的上限与真实可成交边界不一致。
            up = rules.limit_up(base) if rules is not None else None
            down = rules.limit_down(base) if rules is not None else None
            self.high_limit = up if up is not None else round(base * 1.1, 2)
            self.low_limit = down if down is not None else round(base * 0.9, 2)
        self.is_st = is_st
        self.name = name


class _CurrentData:
    def __init__(self, runner: JQRunner) -> None:
        self._r = runner

    def __getitem__(self, security: str) -> _SecData:
        return self._r._sec_data(security)


class _BarProxy:
    """data[sym]：策略运行时刻的当前 bar 视图。"""
    __slots__ = ("open", "close", "high", "low", "volume", "money", "_avg")

    def __init__(self, bar: Bar, ref: float) -> None:
        self.open = bar.open
        self.close = ref
        self.high = bar.high
        self.low = bar.low
        self.volume = bar.volume
        self.money = bar.amount
        self._avg = ((bar.open + bar.high + bar.low + bar.close) / 4.0
                     if bar.close else float("nan"))

    @property
    def avg(self) -> float:
        return self._avg


class _DataProxy:
    """handle_data(context, data) 的 data。"""

    def __init__(self, runner: JQRunner) -> None:
        self._r = runner

    def __getitem__(self, security: str) -> _BarProxy:
        bar = self._r._bars_today.get(security)
        if bar is None:
            raise KeyError(f"{security} 今日无行情（可能停牌/未在数据集）")
        return _BarProxy(bar, self._r._ref_price(security))

    def __contains__(self, security: str) -> bool:
        return security in self._r._bars_today


class _JQPosition:
    """context.portfolio.positions[sym]。"""

    def __init__(self, runner: JQRunner, symbol: str) -> None:
        self._r = runner
        self._s = symbol

    @property
    def total_amount(self) -> float:
        p = self._r.account.positions.get(self._s)
        return p.qty if p else 0.0

    @property
    def closeable_amount(self) -> float:
        p = self._r.account.positions.get(self._s)
        rules = self._r._rules.get(self._s)
        if p is None or rules is None:
            return 0.0
        return self._r._round_lot(self._s, p.available_at(self._r._today, rules))

    @property
    def price(self) -> float:
        return self._r._ref_price(self._s)

    @property
    def avg_cost(self) -> float:
        p = self._r.account.positions.get(self._s)
        return p.avg_cost if p else 0.0

    @property
    def value(self) -> float:
        return self.total_amount * self.price


class _PositionsMap(dict):
    """聚宽语义：positions[未持有股票] 返回空持仓对象，不抛 KeyError。"""

    def __init__(self, runner: JQRunner) -> None:
        super().__init__()
        self._r = runner

    def __missing__(self, key: str) -> _JQPosition:
        v = _JQPosition(self._r, key)
        self[key] = v
        return v


class _JQPortfolio:
    def __init__(self, runner: JQRunner) -> None:
        self._r = runner
        self._pmap = _PositionsMap(runner)

    @property
    def starting_cash(self) -> float:
        return self._r.initial_cash

    @property
    def available_cash(self) -> float:
        return self._r.account.cash

    inout_cash = 0.0

    @property
    def positions_value(self) -> float:
        return self.total_value - self._r.account.cash

    @property
    def total_value(self) -> float:
        return self._r._nav_now()

    @property
    def returns(self) -> float:
        cash = self.starting_cash
        tv = self.total_value
        return tv / cash - 1.0 if cash > 0 and tv > 0 else 0.0

    @property
    def positions(self) -> _PositionsMap:
        held = {s for s, p in self._r.account.positions.items() if p.qty}
        for s in list(self._r._touched):
            if s in self._r._bars_today:
                held.add(s)
        for s in held:
            if s not in self._pmap:
                self._pmap[s] = _JQPosition(self._r, s)
        return self._pmap


class _JQContext:
    def __init__(self, runner: JQRunner) -> None:
        self.portfolio = _JQPortfolio(runner)
        self._r = runner
        self.current_dt: datetime = datetime.combine(today_cn(), dtime(9, 30))
        self.previous_date: date | None = None
        self.benchmark = "000300.SH"
        self.subports = []
        self.run_params = {"frequency": "daily"}

    @property
    def current_date(self) -> date:                  # 老版聚宽 API
        return self.current_dt.date()


class _Log:
    def __init__(self, sink: list[str]) -> None:
        self._s = sink

    def _fmt(self, level: str, msg, *args) -> None:
        try:
            text = msg % args if args else str(msg)
        except Exception:  # noqa: BLE001
            text = str(msg)
        self._s.append(f"[{level}] {text}")

    def info(self, msg, *args) -> None:
        self._fmt("INFO", msg, *args)

    warn = warning = info
    error = debug = info


# ---------- 运行器 ----------

def _load_security_meta() -> dict[str, dict]:
    """从 security 表读 per-instrument 元数据（is_st / 退市日 / 名称）。

    任何异常（表不存在 / 无库连接）与空表都返回 {} —— JQRunner 必须能
    在无 security 表的沙箱数据上照常回测，绝不因元数据缺失而抛错。
    与原生 Engine 路径共用 security_meta 模块，两条路径拿到同一套元数据。
    """
    return load_security_meta()


class JQRunner:
    """加载用户代码并提供聚宽日频 API。执行前先跑一遍语法/运行时冒烟。"""

    def __init__(self, code: str, *, initial_cash: float = 1_000_000.0,
                 benchmark: str = "000300.SH", rebalance: str = "none",
                 participation: float = 0.1, ruleset=None,
                 factor_formulas: list[str] | None = None,
                 security_meta: dict[str, dict] | None = None,
                 timeout_s: float | None = None,
                 delist_recovery: float = 0.0) -> None:
        self.code = code
        self.initial_cash = initial_cash
        # 用户代码墙钟预算（秒）。None = 不限；服务端入口必须设置（防死循环 DoS）。
        self._timeout_s = timeout_s
        self._deadline: float | None = None
        self.default_benchmark = benchmark
        self._ruleset = ruleset
        self._participation = participation
        self._factor_formulas = [str(f) for f in (factor_formulas or [])]
        # security 表元数据：参数注入逐键覆盖 DB（is_st / 退市日 / 名称）
        self._security_meta = security_meta
        self._delist_recovery = float(delist_recovery)
        self._jf_state: JQFundamentalsState = JQFundamentalsState()
        self._security_meta_resolved: dict[str, dict] = {}

        # 运行期状态
        self.account = Account(cash=initial_cash)
        self._rules: dict = {}
        self._bars_today: dict[str, Bar] = {}
        # 占位值（每次 step 会被真实交易日覆盖）；用业务日保证与
        # server / 采集链路的日期口径一致
        self._today: date = today_cn()
        self._bucket = "open"                # open | close
        self._touched: set[str] = set()
        self.res = JQResult()
        self._day_index = 0
        self._dates: list[date] = []
        self._bars_by_day: dict[date, dict[str, Bar]] = {}
        self._factor_panels: dict[str, dict[tuple[date, str], float]] = {}
        self._last_factor: dict[str, float] = {}
        self._date_index: dict[date, int] = {}
        self._delisted: set[str] = set()

        # 用户代码命名空间（exec 共享 globals，模块级变量等价聚宽的 g.*）
        # __builtins__ 显式收口：不设的话 CPython 会注入完整内建，用户代码可直接
        # __import__("os")，绕过 validate_source 的 import 白名单。
        self.ns: dict = {"__name__": "__jq__", "g": type("G", (), {})()}
        self.ns["__builtins__"] = safe_builtins()
        self._install_api()
        self._compile()

    # ---- 编译与 initialize ----

    def _compile(self) -> None:
        try:
            exec(self.code, self.ns)          # noqa: S102 - 设计前提，见模块 docstring
        except Exception as e:                # noqa: BLE001
            raise ValueError(f"策略代码执行失败: {e}\n{traceback.format_exc(limit=4)}") from e
        self._initialize_fn = self.ns.get("initialize")
        self._handle_data_fn = self.ns.get("handle_data")
        self._before_trading_fn = self.ns.get("before_trading_start")
        self._after_trading_fn = self.ns.get("after_trading_end")
        self._sched: list[tuple] = []          # run() 里 initialize 之后构建

    def _build_sched(self) -> None:
        """调度表必须在 initialize 之后构建（run_daily 在 initialize 里注册）。"""
        self._sched = list(self._sched_reg)
        if self._handle_data_fn:
            self._sched.append((self._handle_data_fn, "d:every_bar"))
        self.benchmark = self._set_benchmark_arg or self.default_benchmark

    # ---- API 注册 ----

    def _install_api(self) -> None:
        r = self
        ns = self.ns
        self._sched_reg = []
        self._set_benchmark_arg = None
        self._fee_override: dict | None = None
        # 默认滑点来自规则表（cn_a_share.yaml default.slippage）—— 与原生 Engine
        # 路径同一个真源；策略调用 set_slippage 时仍完全覆盖。
        _slip_mode, _slip_params = default_slippage(self._ruleset)
        self._slippage = make_slippage(_slip_mode, **_slip_params)

        def set_benchmark(security: str) -> None:
            self._set_benchmark_arg = str(security)

        def set_option(key: str, value) -> None:
            # use_real_price / avoid_future_data 等在本引擎下恒成立，接受但忽略
            pass

        def set_universe(assets) -> None:
            pass                              # 老版 API，本引擎按数据集全量提供

        def set_order_cost(type: str = "stock", open_tax: float = 0.0,   # noqa: A002
                           close_tax: float = 0.001, open_commission: float = 0.0003,
                           close_commission: float = 0.0003, min_commission: float = 5.0,
                           **kw) -> None:
            self._fee_override = {
                "tax_rate": max(open_tax, close_tax if open_tax else close_tax),
                "comm_rate": max(open_commission, close_commission),
                "min": min_commission,
            }

        def set_slippage(s) -> None:
            if isinstance(s, (int, float)):
                self._slippage = FixedSlippage(s)
            elif isinstance(s, FixedSlippage | PriceRelatedSlippage):
                self._slippage = s
            else:                             # 聚宽对象 duck-type
                self._slippage = s

        def run_daily(fn, time: str = "every_bar") -> None:    # noqa: A002
            # 调度键必须与 run_weekly/run_monthly 的 'wN:'/'mN:' 前缀区分开。
            # 历史 bug：'14:50' 直接进调度表后，_due_funcs 按 ':' 切分得到
            # kind='14' → int('4') → 被当成「每月第 4 个交易日」，
            # 日频任务被静默改写成月频。
            t = str(time)
            if t not in _RUN_DAILY_KEYWORDS:
                _parse_clock(t)      # 非法时刻立即报错，绝不静默跑偏
            self._sched_reg.append((fn, f"d:{t}"))

        def run_weekly(fn, weekday: int = 1, time: str = "open") -> None:  # noqa: A002
            self._sched_reg.append((fn, f"w{int(weekday)}:{time}"))

        def run_monthly(fn, monthday: int = 1, time: str = "open") -> None:  # noqa: A002
            self._sched_reg.append((fn, f"m{int(monthday)}:{time}"))

        def order(security: str, amount: float, style=None, **kw):
            return r._submit(str(security), float(amount),
                             limit_price=_style_limit(style))

        def order_value(security: str, value: float, style=None, **kw):
            px = r._ref_price(str(security))
            if not px or px <= 0 or not math.isfinite(px):
                return None
            return r._submit(str(security), float(value) / px,
                             limit_price=_style_limit(style))

        def order_target(security: str, amount: float, style=None, **kw):
            cur = r.account.positions.get(str(security))
            held = cur.qty if cur else 0.0
            return r._submit(str(security), float(amount) - held,
                             limit_price=_style_limit(style))

        def order_target_value(security: str, value: float, style=None, **kw):
            px = r._ref_price(str(security))
            if not px or px <= 0 or not math.isfinite(px):
                return None
            cur = r.account.positions.get(str(security))
            held_val = cur.qty * px if cur else 0.0
            return r._submit(str(security), (float(value) - held_val) / px,
                             limit_price=_style_limit(style))

        def order_target_percent(security: str, percent: float, style=None, **kw):
            """目标市值 = 组合总市值 × percent（聚宽最常用的下单 API）。

            此前未注入沙箱 → 策略里一调用就 NameError；这是聚宽示例代码
            出现频率最高的下单函数之一。
            """
            sym = str(security)
            px = r._ref_price(sym)
            if not px or px <= 0 or not math.isfinite(px):
                return None
            total = r.account.nav(r._prices_map(), r._last_close)
            cur = r.account.positions.get(sym)
            held_val = cur.qty * px if cur else 0.0
            return r._submit(sym, (total * float(percent) - held_val) / px,
                             limit_price=_style_limit(style))

        def cancel_order(order_obj) -> None:
            pass                              # 委托即时撮合，无挂单可撤

        def get_current_data() -> _CurrentData:
            return _CurrentData(r)

        def history(count: int, unit: str = "1d", field="close",                    # noqa: A002
                    security_list=None, df: bool = True, skip_paused: bool = True,
                    fq="pre"):
            return r._history(int(count), field, security_list, df,
                              skip_paused=skip_paused, fq=fq)

        def attribute_history(security: str, count: int, unit: str = "1d",         # noqa: A002
                              fields=("close",), skip_paused: bool = True, fq="pre"):
            return r._attribute_history(str(security), int(count), fields,
                                        skip_paused=skip_paused, fq=fq)

        def get_price(security, start_date=None, end_date=None,
                      frequency: str = "daily", fields=None, count: int | None = None,
                      panel: bool = True, fq="pre"):
            return r._get_price(security, start_date, end_date, fields, count,
                                panel=panel, fq=fq)

        def get_trade_days(start_date=None, end_date=None, count=None):
            """交易日列表：优先用回测自身的日历（无需库、与模拟盘一致）。"""
            return r._get_trade_days(start_date, end_date, count)

        def get_index_stocks(index_symbol, date=None):
            """指数成分股（PIT：按 date 已生效成分）。缺省用当前交易日。"""
            return r._get_index_stocks(index_symbol, date)

        def get_all_securities(types=None, date=None):
            """证券列表。优先 security 表；无库时退回回测数据集内的标的。"""
            return r._get_all_securities(types, date)

        def get_current_user_query_result(*a, **kw):   # 未支持项给清晰报错
            raise NotImplementedError("该聚宽 API 未支持（当前兼容日频核心子集）")

        # 基本面：get_fundamentals + query DSL 表对象（import 放函数内，
        # 避免模块级循环依赖；PIT 语义见 jq_fundamentals.JQFundamentalsState）
        from lquant.research.dialect import fundamentals as _fd

        ns["get_fundamentals"] = self._jf_state.make_get_fundamentals()
        for _t in ("fundamentals", "valuation", "income", "balance", "cashflow",
                   "indicator", "growth", "operation"):
            ns[_t] = getattr(_fd, _t)
        ns["query"] = _fd.query

        ns.update(
            set_benchmark=set_benchmark, set_option=set_option, set_universe=set_universe,
            set_order_cost=set_order_cost, set_slippage=set_slippage,
            run_daily=run_daily, run_weekly=run_weekly, run_monthly=run_monthly,
            order=order, order_value=order_value, order_target=order_target,
            order_target_value=order_target_value, cancel_order=cancel_order,
            record=lambda **kv: r._record(kv),
            get_current_data=get_current_data, history=history,
            attribute_history=attribute_history, get_price=get_price,
            get_factor_values=r._get_factor_values,
            order_target_percent=order_target_percent,
            get_trade_days=get_trade_days,
            get_index_stocks=get_index_stocks,
            get_all_securities=get_all_securities,
            get_Ashares=get_current_user_query_result,
            FixedSlippage=FixedSlippage, PriceRelatedSlippage=PriceRelatedSlippage,
            MarketOrder=MarketOrder, LimitOrder=LimitOrder,
            log=_Log(self.res.logs),
        )
        self.context = _JQContext(self)
        ns["context"] = self.context

    # ---- 内部工具 ----

    def _record(self, kv: dict) -> None:
        """record(**kv)：自定义曲线采集，每日每键一条 (trade_date, value)。

        非有限值（NaN/Inf）静默跳过，避免污染曲线序列。
        """
        for k, v in kv.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            self.res.records.setdefault(str(k), []).append((self._today, fv))

    def _run_hook(self, fn, d: date, when: str) -> str | None:
        """跑生命周期钩子；异常时返回错误文本（调用方置 res.error 并终止）。"""
        self._bucket = "open" if when == "open" else "close"
        self.context.current_dt = datetime.combine(
            d, dtime(9, 30) if when == "open" else dtime(15, 0))
        try:
            self._call_user(fn, self.context) if fn.__code__.co_argcount else self._call_user(fn)
        except Exception as e:                    # noqa: BLE001
            name = getattr(fn, "__name__", "?")
            return (f"{d} {when} 钩子 {name} 异常: {e}\n{traceback.format_exc(limit=4)}")
        return None

    def _round_lot(self, sym: str, qty: float) -> float:
        rules = self._rules.get(sym)
        lot = rules.lot_size if rules else 100
        if lot <= 1:
            return max(qty, 0.0)
        return (int(qty) // lot) * lot

    def _ref_price(self, sym: str) -> float:
        bar = self._bars_today.get(sym)
        if bar is None:
            return float("nan")
        return bar.open if self._bucket == "open" else bar.close

    def _prices_map(self) -> dict[str, float]:
        return {s: self._ref_price(s) for s in self._bars_today}

    def _nav_now(self) -> float:
        return self.account.nav(self._prices_map(), self._last_close)

    def _sec_data(self, sym: str) -> _SecData:
        m = self._security_meta_resolved.get(sym, {})
        return _SecData(self._bars_today.get(sym), self._ref_price(sym),
                        is_st=m.get("is_st", False), name=m.get("name", ""),
                        rules=self._rules.get(sym))

    # ---- 聚宽数据接口补充实现 ----

    def _get_trade_days(self, start_date=None, end_date=None, count=None) -> list:
        """交易日列表。

        优先用回测自身的日历（与撮合/模拟盘完全一致，且不需要库连接）——
        策略据此判断「下一个交易日」不会与引擎实际推进的日期错位。
        回测日历为空时退回库里的官方交易日历。
        """
        if self._dates:
            days = list(self._dates)
            sd = self._as_date(start_date)
            ed = self._as_date(end_date)
            if sd is not None:
                days = [d for d in days if d >= sd]
            if ed is not None:
                days = [d for d in days if d <= ed]
            if count is not None:
                n = int(count)
                days = days[-n:] if n >= 0 else days[:-n] if n else []
            return days
        from lquant.research.dialect import jq_shim
        return list(jq_shim.get_trade_days(start_date, end_date))

    def _get_index_stocks(self, index_symbol, date=None) -> list:
        """指数成分股，PIT：按 date 已生效成分。缺省 = 当前交易日（防前视）。

        成分表未同步时给可操作的报错 —— 绝不静默返回空（"回测跑通了但
        选股池是空的" 是最糟的结果：策略静默空仓，指标却正常）。
        """
        from lquant.core.errors import DataError
        from lquant.research.dialect import jq_shim

        day = self._as_date(date) or self._today
        try:
            return list(jq_shim.get_index_stocks(index_symbol, day))
        except Exception as e:                 # noqa: BLE001 - 换成可操作的报错
            raise DataError(
                f"get_index_stocks({index_symbol!r}, {day}) 取不到成分股：{e}。"
                "请先同步指数成分（index_cons 表）") from e

    def _get_all_securities(self, types=None, date=None) -> object:
        """证券列表。无库（合成数据沙箱）时退回本次回测数据集内的标的。"""
        try:
            from lquant.research.dialect import jq_shim
            out = jq_shim.get_all_securities(types, date)
            if out is not None and len(out):
                return out
        except Exception:                 # noqa: BLE001 - 元数据缺失不致命
            pass
        return pl.DataFrame({"symbol": sorted(self._bars_by_day_date_symbols())})

    def _bars_by_day_date_symbols(self) -> set[str]:
        return {s for bars in self._bars_by_day.values() for s in bars}

    @staticmethod
    def _as_date(v) -> date | None:
        """把 str / date / None 归一成 date（get_fundamentals 曾因未归一而崩）。"""
        if v is None:
            return None
        if isinstance(v, date):
            return v
        return date.fromisoformat(str(v)[:10])

    # ---- 公司行为 / 退市（与 engine.Engine 同一套语义） ----

    def _apply_corporate_actions(self, bars: dict[str, Bar]) -> None:
        """除权日按复权因子比放大持仓份额（分红默认再投资的份额调整法）。

        与 Engine._apply_corporate_actions 同口径：ratio = 今日因子 / 昨日因子。
        JQ 路径委托是即时撮合的、没有挂单，所以只需要调整持仓。
        """
        for sym, bar in bars.items():
            if bar.adj_factor <= 0:
                continue
            prev = self._last_factor.get(sym, bar.adj_factor)
            if prev <= 0:
                continue
            ratio = bar.adj_factor / prev
            if abs(ratio - 1.0) > 1e-12:
                self.account.apply_corporate_action(sym, ratio)

    def _apply_delistings(self, d: date) -> None:
        """退市核销：退市日起把持仓按残值变现，避免按最后收盘价永久冻结。"""
        for sym, m in self._security_meta_resolved.items():
            delist = m.get("delist_date")
            if delist is None or d < delist or sym in self._delisted:
                continue
            self._delisted.add(sym)
            pos = self.account.positions.get(sym)
            if pos is None or pos.qty <= 0:
                continue
            px = self._last_close.get(sym) or pos.avg_cost
            self.account.cash += pos.qty * px * float(self._delist_recovery)
            self.res.rejected.append(
                (str(d), sym, f"退市核销 qty={pos.qty:.0f} 残值率={self._delist_recovery}"))
            pos.qty = 0.0
            pos.lots = []

    def _is_delisted_now(self, sym: str) -> bool:
        """该标的是否已到退市日（到了就不可再交易）。"""
        m = self._security_meta_resolved.get(sym)
        if not m:
            return False
        delist = m.get("delist_date")
        return delist is not None and self._today >= delist

    def _history_df(self, secs: list[str], fields: list[str], rows: list[dict]) -> object:
        """rows 为时间升序窗口 [{sec: {field: v}, 'day': d}]；优先 pandas。

        布局对齐聚宽：单字段 → 列=证券；单证券 → 列=字段；多证券多字段 → MultiIndex。
        """
        # 统一用字符串日期做索引（与 _attribute_history 一致），
        # 否则 s['2026-01-05'] 在两个接口行为相反
        index = [str(row["day"]) for row in rows]
        try:
            import pandas as pd
        except ImportError:
            out: dict = {}
            for sec in secs:
                out[sec] = {f: [row.get(sec, {}).get(f) for row in rows]
                            for f in fields}
            if len(fields) == 1 and secs:
                return {s: out[s][fields[0]] for s in secs}
            return out

        f0 = fields[0]
        if len(fields) == 1:
            data = {s: [row.get(s, {}).get(f0) for row in rows] for s in secs}
            return _JQFrame(data, index=index)
        if len(secs) == 1:
            data = {f: [row.get(secs[0], {}).get(f) for row in rows] for f in fields}
            return _JQFrame(data, index=index)
        cols = pd.MultiIndex.from_product([secs, fields])
        df = _JQFrame(index=index, columns=cols)
        for s in secs:
            for f in fields:
                df[(s, f)] = [row.get(s, {}).get(f) for row in rows]
        return df

    _hist_fields_map = {"avg": "avg"}

    def _bar_field(self, bar: Bar, f: str) -> float:
        if f == "avg":
            return (bar.open + bar.high + bar.low + bar.close) / 4.0
        if f == "money":
            return bar.amount
        if f == "volume":
            return bar.volume
        return float(getattr(bar, f, float("nan")))

    def _fq_ref(self, sym: str) -> float:
        """pre 复权的归一基准 = 今日复权因子；今日无 bar 时退回最近可见因子。"""
        bar = self._bars_today.get(sym)
        if bar is not None and bar.adj_factor > 0:
            return bar.adj_factor
        f = self._last_factor.get(sym, 1.0)
        return f if f and f > 0 else 1.0

    def _bar_field_fq(self, sym: str, bar: Bar, f: str, fq, f_now: float):
        """取 bar 字段并按 fq 复权。"""
        raw = self._bar_field(bar, f)
        if fq in (None, "none") or f not in _FQ_PRICE_FIELDS:
            return raw
        fday = bar.adj_factor if bar.adj_factor and bar.adj_factor > 0 else f_now
        if not math.isfinite(raw):
            return raw
        return raw * fday / f_now if fq == "pre" else raw * fday

    def _history(self, count: int, field, security_list, df: bool = True,
                 skip_paused: bool = True, fq="pre"):
        # 字符串 fields 必须当单字段处理 —— list('close') 会拆成
        # ['c','l','o','s','e'] 五个列，随后报迷惑的 KeyError('close')
        fields = [field] if isinstance(field, str) else list(field or ["close"])
        secs = list(security_list) if security_list else sorted(self._bars_today)
        i = self._day_index
        if skip_paused:
            # G5:交易日窗口 — 从 i 前推收集 count 个"标的当日有 bar"的交易日
            # (单标的看该标的,多标的看当日任一标的有 bar,即合并交易日)。
            # 严格不含今天 → 无未来函数。
            def _traded(day) -> bool:
                bars = self._bars_by_day.get(day, {})
                if len(secs) == 1:
                    return secs[0] in bars
                return bool(bars)

            days = [d for d in self._dates[:i] if _traded(d)][-count:]
        else:
            days = self._dates[max(0, i - count):i]
        f_now = {s: self._fq_ref(s) for s in secs}
        rows = []
        for day in days:
            bars = self._bars_by_day.get(day, {})
            rows.append({s: {f: self._bar_field_fq(s, b, f, fq, f_now[s])
                             for f in fields}
                         for s, b in ((s, bars.get(s)) for s in secs) if b})
            rows[-1]["day"] = day
        return self._history_df(secs, fields, rows)

    def _attribute_history(self, sec: str, count: int, fields,
                           skip_paused: bool = True, fq="pre"):
        # 与 _history 同理：字符串 fields 是「一个字段」，不是字符序列
        fields = [fields] if isinstance(fields, str) else list(fields or ["close"])
        try:
            import pandas as pd
        except ImportError:
            pd = None
        i = self._day_index
        if skip_paused:
            # G5:按"有 bar 的行"从当前 i 向前取 count 根(不含今天)
            j = i - 1
            taken = 0
            js = []
            while j >= 0 and taken < count:
                if self._bars_by_day.get(self._dates[j], {}).get(sec):
                    js.append(j)
                    taken += 1
                j -= 1
            js.reverse()
        else:
            js = range(max(0, i - count), i)
        f_now = self._fq_ref(sec)
        rows = []
        for j in js:
            b = self._bars_by_day.get(self._dates[j], {}).get(sec)
            if b:
                rows.append({"day": self._dates[j],
                             **{f: self._bar_field_fq(sec, b, f, fq, f_now)
                                for f in fields}})
        if pd is None:
            return {f: [r[f] for r in rows] for f in fields}
        return _JQFrame([{**r, "day": str(r["day"])} for r in rows]
                        ).set_index("day") if rows else _JQFrame(columns=fields)

    def _get_price(self, security, start_date=None, end_date=None,
                   fields=None, count=None, panel: bool = True, fq="pre"):
        secs = [security] if isinstance(security, str) else list(security)
        fields = list(fields or ["open", "close", "high", "low", "volume"])
        dates = self._dates
        if count is not None:
            i = self._day_index
            lo, hi = max(0, i - int(count)), i          # 不含今天
        else:
            sd = date.fromisoformat(str(start_date)) if start_date else dates[0]
            ed = date.fromisoformat(str(end_date)) if end_date else None
            # 无未来函数：end_date 一律钳制到上一交易日（与 history/attribute_history 一致）。
            # 当日 bar 在开盘决策时点含收盘价，放行即是未来函数。
            if self._day_index > 0:
                prev = dates[self._day_index - 1]
                ed = min(ed, prev) if ed is not None else prev
            else:
                # 首日开盘决策前没有任何历史数据可看
                return self._get_price_empty(secs, fields, panel)
            lo = next((k for k, d in enumerate(dates) if d >= sd), 0)
            hi = next((k for k, d in enumerate(dates) if d > ed), len(dates))
        rows = []
        f_now = {s: self._fq_ref(s) for s in secs}
        for j in range(lo, min(hi, len(dates))):
            day = dates[j]
            bars = self._bars_by_day.get(day, {})
            row: dict = {"day": day}
            for s in secs:
                b = bars.get(s)
                if b:
                    row[s] = {f: self._bar_field_fq(s, b, f, fq, f_now[s])
                              for f in fields}
            rows.append(row)
        # 聚宽语义：单标的 → 列=fields（history 才是单字段→列=证券，二者不同）；
        # 多标的 → (标的, 字段) MultiIndex。
        index = [str(r["day"]) for r in rows]  # 与 history/attribute_history 统一为字符串日期
        if _JQFrame is not None:
            import pandas as pd
            if not panel and len(secs) > 1:
                # 老聚宽 panel=False 语义：行=(日期, 标的) MultiIndex，列=fields
                flat = _JQFrame(
                    [{f: r.get(s, {}).get(f) for f in fields}
                     for r in rows for s in secs],
                    index=pd.MultiIndex.from_product(
                        [[str(r["day"]) for r in rows], secs],
                        names=["day", "code"]),
                )
                return flat
            if len(secs) == 1:
                data = {f: [r.get(secs[0], {}).get(f) for r in rows] for f in fields}
                return _JQFrame(data, index=index)
            cols = pd.MultiIndex.from_product([secs, fields])
            out = _JQFrame(index=index, columns=cols)
            for s in secs:
                for f in fields:
                    out[(s, f)] = [r.get(s, {}).get(f) for r in rows]
            return out
        # 无 pandas：单标的 {field: [...]}, 多标的 {sec: {field: [...]}}
        if len(secs) == 1:
            return {f: [r.get(secs[0], {}).get(f) for r in rows] for f in fields}
        return {s: {f: [r.get(s, {}).get(f) for r in rows] for f in fields} for s in secs}

    def _get_price_empty(self, secs: list[str], fields: list[str], panel: bool):
        """首日决策前无历史数据，返回与 _get_price 相同形状的空结果。"""
        if _JQFrame is None:
            if len(secs) == 1:
                return {f: [] for f in fields}
            return {s: {f: [] for f in fields} for s in secs}
        import pandas as pd
        if not panel and len(secs) > 1:
            return _JQFrame(
                index=pd.MultiIndex.from_product([[], secs], names=["day", "code"]))
        if len(secs) == 1:
            return _JQFrame({f: [] for f in fields})
        return _JQFrame(columns=pd.MultiIndex.from_product([secs, fields]))

    # ---- 下单 ----

    def _submit(self, sym: str, amount: float, limit_price: float | None = None):
        """聚宽 order：按股数下单，即时以当前时点参考价撮合。

        limit_price 非空 = 限价单（当日有效）：成交价劣于限价则作废。
        """
        if abs(amount) < 1:
            return None
        try:
            parse_symbol(sym)
        except ValueError as e:
            raise ValueError(f"证券代码不合法: {sym}（需带后缀如 600519.SH）") from e
        rules = self._rules.get(sym)
        if rules is None:
            self.res.rejected.append((str(self._today), sym, "不在数据集"))
            return None
        if self._is_delisted_now(sym):
            self.res.rejected.append((str(self._today), sym, "已退市"))
            return None
        side = Side.BUY if amount > 0 else Side.SELL
        qty = abs(amount)
        px = self._ref_price(sym)
        if not px or not math.isfinite(px) or px <= 0:
            self.res.rejected.append((str(self._today), sym, "无参考价"))
            return None
        if side == Side.SELL:
            pos = self.account.positions.get(sym)
            avail = pos.available_at(self._today, rules, self._date_index) if pos else 0.0
            qty = min(qty, avail)
        else:
            # 资金约束：按参考价 + 粗略费用能买多少买多少
            afford = self.account.cash / (px * (1 + rules.commission.rate + 0.001))
            qty = min(qty, afford)
        qty = self._round_lot(sym, qty)
        if qty <= 0:
            self.res.rejected.append((str(self._today), sym,
                                      "可卖不足一手或资金不足" if side == Side.SELL else "资金不足一手"))
            return None
        self._touched.add(sym)
        o = Order(order_id=f"jq{self._seq}", symbol=sym, side=side, qty=qty,
                  limit_price=limit_price)
        self._seq += 1
        bar = self._bars_today.get(sym)
        if bar is None:
            self.res.rejected.append((str(self._today), sym, "无行情"))
            return None
        max_qty = bar.volume * self._participation if bar.volume > 0 else None
        fill = self._broker.match(o, self._synth_bar(bar, px), self._today, max_qty=max_qty)
        if fill is None:
            self.res.rejected.append((str(self._today), sym, o.reason or "未成交"))
            return None
        self.account.apply_fill(fill)
        self.res.trades.append(fill)
        return o

    def _synth_bar(self, bar: Bar, ref: float) -> Bar:
        """以参考价为撮合价的合成 bar（撮合器取 bar.open 作成交价）。

        停牌/零成交标记必须透传 —— 丢了就会把不可交易的 bar 当成可成交。
        """
        return Bar(symbol=bar.symbol, trade_date=bar.trade_date, open=ref, high=bar.high,
                   low=bar.low, close=bar.close, pre_close=bar.pre_close,
                   volume=bar.volume, amount=bar.amount, halted=bar.halted,
                   suspended=bar.suspended, no_volume=bar.no_volume)

    # ---- 调度 ----

    # 日频引擎只有「开盘 / 收盘」两个执行桶。显式时刻据此归档：
    # 尾盘时刻（>= 14:30）按收盘价成交，否则按开盘价 —— 把 '14:50' 一律塞进
    # open 桶会让「尾盘下单」用开盘价成交，语义系统性偏离。
    _TAIL_BUCKET_MINUTES = 14 * 60 + 30

    @classmethod
    def _bucket_of(cls, t: str) -> str:
        if t in ("close", "after_close"):
            return "close"
        if t not in _RUN_DAILY_KEYWORDS and _parse_clock(t) >= cls._TAIL_BUCKET_MINUTES:
            return "close"
        return "open"

    def _due_funcs(self, d: date, i: int) -> list:
        """当日到期 [(fn, 时点桶)]。周按 ISO 周几，月按「第 N 个交易日」。

        调度键格式：
          d:<every_bar|open|close|after_close|HH:MM>   日频
          w<1-7>:<time>                                周频（ISO 周几）
          m<N>:<time>                                  月频（N>0 第 N 个交易日）
          m<-N>:<time>                                 月频（N<0 倒数第 |N| 个交易日）
        """
        week = d.isocalendar().weekday
        ym = (d.year, d.month)
        # 本月在交易日序列中的 [lo, hi] 闭区间 —— 必须用整月长度算「倒数第 N 个」，
        # 只从当天往后数会得到「今天到月末 = 1 天」这种错值。
        lo = i
        while lo > 0 and (self._dates[lo - 1].year, self._dates[lo - 1].month) == ym:
            lo -= 1
        hi = i
        while hi + 1 < len(self._dates) and \
                (self._dates[hi + 1].year, self._dates[hi + 1].month) == ym:
            hi += 1
        nth_in_month = i - lo + 1        # 月内第几个交易日（1-based）
        n_in_month = hi - lo + 1         # 本月交易日总数
        back_from_end = n_in_month - nth_in_month + 1   # 倒数第几个（1-based）

        out = []
        for fn, when in self._sched:
            kind, _, t = when.partition(":")
            if kind == "d":
                pass                                  # 日频：每天都到期
            elif kind.startswith("w"):
                if int(kind[1:]) != week:
                    continue
            elif kind.startswith("m"):
                n = int(kind[1:])
                if n < 0:
                    # 月末倒数：run_monthly(fn, -1) = 本月最后一个交易日。
                    # 历史 bug：只比较正数，负数永远不等于 nth_in_month → 静默不触发。
                    if n != -back_from_end:
                        continue
                elif n != nth_in_month:
                    continue
            else:
                t = when
            out.append((fn, self._bucket_of(t)))
        return out

    # ---- 主循环 ----

    def _get_factor_values(self, formula: str, security_list=None,
                           count: int = 1) -> dict[str, list[float]]:
        """聚宽 get_factor_values：{security: [值...]}，严格截至当日（无未来函数）。"""
        f = str(formula).upper()
        panel = self._factor_panels.get(f)
        if panel is None:
            raise ValueError(f"因子 {formula} 未注册（回测请求需带 factor_formulas）")
        secs = [str(s) for s in (security_list or sorted(self._bars_today))]
        i = self._day_index
        out: dict[str, list[float]] = {}
        for s in secs:
            out[s] = [panel[(self._dates[j], s)]
                      for j in range(max(0, i - int(count) + 1), i + 1)
                      if (self._dates[j], s) in panel]
        return out

    def _build_factor_panels(self, raw_df: pl.DataFrame | None
                             ) -> dict[str, dict[tuple[date, str], float]]:
        """一次性算好全部因子面板 {formula.upper(): {(date, symbol): value}}。"""
        if not self._factor_formulas or raw_df is None:
            return {}
        df2, colmap = compute_factor_columns(raw_df, self._factor_formulas)
        panels: dict[str, dict[tuple[date, str], float]] = {}
        for f, col in colmap.items():
            sub = df2.select(["trade_date", "symbol", col]).drop_nulls()
            # NaN（如停牌前 pct_change 的溢出值）与 null 一并剔除
            sub = sub.filter(pl.col(col).is_not_nan())
            panels[f.upper()] = {(r[0], r[1]): float(r[2]) for r in sub.iter_rows()}
        return panels


    def _call_user(self, fn, *args):
        """调用用户钩子：设置了墙钟预算时走 trace 看门狗（可抢占死循环）。"""
        if self._timeout_s is None:
            return fn(*args)
        return run_with_deadline(fn, *args, timeout_s=self._timeout_s,
                                 deadline=self._deadline)

    def run(self, data) -> JQResult:
        import time as _time

        if self._timeout_s is not None:
            self._deadline = _time.monotonic() + float(self._timeout_s)
        if self._factor_formulas and not isinstance(data, pl.DataFrame):
            raise ValueError(
                "factor_formulas 需要 DataFrame 输入（bars_by_day dict 不支持因子面板）")
        raw_df = data if isinstance(data, pl.DataFrame) else None
        bars_by_day = data if isinstance(data, dict) else Engine.prepare(data)
        self._bars_by_day = bars_by_day
        self._dates = sorted(bars_by_day)
        if not self._dates:
            return self.res

        # 因子面板：panel.compute_factor_columns 统一算内置/DSL 因子（一次，主循环外）
        self._factor_panels = self._build_factor_panels(raw_df)

        symbols = sorted({s for b in bars_by_day.values() for s in b})
        self.account = Account(cash=self.initial_cash)
        self._seq = 0
        self._last_close: dict[str, float] = {}   # 每只股票最近一次有 bar 的 close

        # initialize 先跑：set_order_cost / set_benchmark 要在规则构建前生效
        if self._initialize_fn:
            try:
                self._call_user(self._initialize_fn, self.context)
            except Exception as e:            # noqa: BLE001
                self.res.error = f"initialize 异常: {e}\n{traceback.format_exc(limit=4)}"
                return self.res
            self.benchmark = self._set_benchmark_arg or self.default_benchmark
        self._build_sched()

        ruleset = self._ruleset
        if self._fee_override:
            fo = self._fee_override
            if ruleset is None:
                from lquant.backtest.rules.loader import load_ruleset
                ruleset = load_ruleset()
            import copy
            ruleset = copy.deepcopy(ruleset)
            ruleset.default["commission"] = {"rate": fo["comm_rate"], "min": fo["min"],
                                             "per_order": True}
            ruleset.default["tax"] = {"rate": fo["tax_rate"]}
        # per-instrument 元数据同源：显式注入优先，逐键覆盖 DB（security 表）。
        # 驱动 get_current_data()[sym] 的 is_st / 名称、ST 分板涨跌停、
        # fund_type→T+0、ETF 跟踪指数涨跌幅、退市核销。
        self._security_meta_resolved = merge_meta(
            _load_security_meta(), self._security_meta)
        self._rules = build_rules(symbols, ruleset, self._security_meta_resolved,
                                  with_db_meta=False)
        self._broker = Broker(self._rules, self._slippage)
        self._date_index = {d: i for i, d in enumerate(self._dates)}
        self._last_factor = {}
        self._delisted = set()

        for i, d in enumerate(self._dates):
            self._today = d
            self._day_index = i
            self._bars_today = bars_by_day[d]
            # 0) 公司行为：除权日按复权因子比放大持仓份额。
            # JQ 路径此前完全没有这一步 —— 同一天同一持仓，Engine 路径净值连续、
            # JQ 路径在除权日凭空跳空（2:1 拆股即 -50%），两条路径不可比。
            self._apply_corporate_actions(self._bars_today)
            for _s, _b in self._bars_today.items():
                if _b.adj_factor > 0:
                    self._last_factor[_s] = _b.adj_factor
            # 0b) 退市核销
            self._apply_delistings(d)
            # 每日执行策略前绑定 get_fundamentals 的当日交易日与股票池（per-runner 状态）
            self._jf_state.set_day(d, sorted(self._bars_today))
            self.context.current_dt = datetime.combine(d, dtime(9, 30))
            self.context.previous_date = self._dates[i - 1] if i > 0 else None

            # 盘前钩子（bucket=open）
            if self._before_trading_fn and (err := self._run_hook(
                    self._before_trading_fn, d, "open")):
                self.res.error = err
                return self.res

            for fn, bucket in self._due_funcs(d, i):
                self._bucket = bucket
                self.context.current_dt = datetime.combine(
                    d, dtime(9, 30) if bucket == "open" else dtime(15, 0))
                try:
                    if bucket == "close" and fn is self._handle_data_fn:
                        continue               # handle_data 不重复在 close 跑
                    if fn is self._handle_data_fn and fn.__code__.co_argcount >= 2:
                        # 聚宽标准签名 handle_data(context, data) —— data 是当日 bar 视图
                        self._call_user(fn, self.context, _DataProxy(self))
                    else:
                        self._call_user(fn, self.context) if fn.__code__.co_argcount else self._call_user(fn)
                except Exception as e:        # noqa: BLE001
                    self.res.error = (f"{d} {self._bucket} 调度 {getattr(fn, '__name__', '?')} "
                                      f"异常: {e}\n{traceback.format_exc(limit=4)}")
                    return self.res

            # 盘后钩子（bucket=close）
            if self._after_trading_fn and (err := self._run_hook(
                    self._after_trading_fn, d, "close")):
                self.res.error = err
                return self.res

            # 收盘估值 + 持仓快照（NAV≤0 爆仓 → 记为 res.error 收场，
            # 与 run() 的"错误进 res.error"契约一致，也让调用方统一走 error 分支）
            try:
                self._settle(d)
            except ValueError as e:
                self.res.error = str(e)
                return self.res

        self._finalize()
        return self.res

    def _settle(self, d: date) -> None:
        """收盘估值 + 持仓快照。"""
        self._bucket = "close"
        prices = {s: b.close for s, b in self._bars_today.items()}
        self._last_close.update(prices)
        nav = self.account.nav(prices, self._last_close)
        if nav <= 0:
            # 爆仓日不能静默跳过：跳日会让净值序列断档，后续指标系统性偏乐观
            # （与 engine.py 的 NAV≤0 快速失败口径一致）。
            raise ValueError(
                f"{d} NAV={nav:.2f} ≤ 0，账户已爆仓（现金不足扣费/杠杆漏洞），"
                f"回测终止。请检查策略仓位与费率设置")
        self.res.nav.append((d, nav))
        self.res.positions[d] = {
            s: p.qty for s, p in self.account.positions.items() if p.qty}

    def _finalize(self) -> None:
        rets = [self.res.nav[i][1] / self.res.nav[i - 1][1] - 1
                for i in range(1, len(self.res.nav)) if self.res.nav[i - 1][1] > 0]
        dates = [d for d, _ in self.res.nav][1:1 + len(rets)]
        perf = perf_from_returns(rets, dates=dates)
        perf.pop("nav", None)
        to = turnover_from_trades(
            [(f.trade_date, f.qty * f.price) for f in self.res.trades],
            nav=self.res.nav)
        self.res.metrics = {
            **perf,
            "initial_cash": self.initial_cash,
            "final_nav": self.res.nav[-1][1] if self.res.nav else self.initial_cash,
            "n_trades": len(self.res.trades),
            "n_rejected": len(self.res.rejected),
            "total_fee": sum(f.fee for f in self.res.trades),
            "turnover": to,
            "benchmark": self.benchmark,
        }
