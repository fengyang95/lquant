"""内置基准策略库 —— 引擎准确性交叉验证与前端一键回测。

这些策略全部来自公开来源（backtrader 教程 / 聚宽社区 / 雪球 / 券商研报），
规则完全可复现，BENCHMARK_META 附带出处与公开回测数字。
验证方法论见 docs/BACKTEST_VALIDATION_BENCHMARKS.md：
- 与 backtrader（独立第三方引擎）在同一份真实数据上逐日对账净值；
- 公开数字只做数量级/方向性对照（公开回测的数据源、复权方式、费率细节
  无法完全一致，逐位对账不现实，能对上趋势与量级即说明引擎无系统性偏差）。

注意：时序类策略内部累积 bar 历史，引擎只在调仓日调用 on_bar ——
因此这类策略必须搭配 rebalance="daily" 使用（run_benchmark 已固定）。
"""
from __future__ import annotations

from lquant.backtest.events import Bar
from lquant.backtest.strategy import register_strategy
from lquant.backtest.strategy.base import Context, Strategy

__all__ = ["BENCHMARK_META", "make_benchmark",
           "SmaCrossStrategy", "MomentumRotationStrategy",
           "TurtleDonchianStrategy", "GridTradingStrategy"]


# ---------- 公开出处与参考数字 ----------

BENCHMARK_META: dict[str, dict] = {
    "sma_cross": {
        "label": "双均线金叉死叉（5/20）",
        "symbols": ["600519.SH"],
        "description": "SMA5 上穿 SMA20 满仓，下穿清仓。单标的择时，最简可复现。",
        "reference": "backtrader 教程案例（CSDN 163407944）：600519 前复权 2024-01-02~2026-07-24，"
                     "佣金万3，累计 -34.68%，20 笔交易 5 笔盈利 —— 负收益结果可防「回测虚高」bug",
    },
    "momentum_rotation": {
        "label": "宽基 ETF 动量单强轮动（22日）",
        "symbols": ["510300.SH", "159915.SZ"],
        "description": "每日看 22 交易日涨幅，满仓动量最高者；全体为负时空仓。",
        "reference": "雪球（384548905）：510300/510500/159915 三池轮动 2011~2026，"
                     "双边成本 0.2%，年化 18.72%，最大回撤 27.35%，夏普 0.90",
    },
    "turtle_donchian": {
        "label": "海龟唐奇安通道突破（ATR仓位）",
        "symbols": ["600519.SH"],
        "description": "20 日高点突破入场、10 日低点破位离场，仓位按 ATR 反比（单日风险 1%）。",
        "reference": "聚宽 33591 海龟组合（2017~2020）：年化 33%，最大回撤 20.28%，盈亏比 3.13",
    },
    "grid_trading": {
        "label": "网格交易（5% 网格）",
        "symbols": ["510300.SH"],
        "description": "锚定首日收盘价，价格每偏离一格线性加减仓位，震荡市收割、单边市受限。",
        "reference": "银河期货《量化回测漫谈》：沪深300 网格 2015.7~2024.6，"
                     "年化 2.78%，最大回撤 10.08%",
    },
}


def make_benchmark(key: str, **params) -> Strategy:
    """按 key 实例化基准策略（API 入口）。"""
    cls = {
        "sma_cross": SmaCrossStrategy,
        "momentum_rotation": MomentumRotationStrategy,
        "turtle_donchian": TurtleDonchianStrategy,
        "grid_trading": GridTradingStrategy,
    }.get(key)
    if cls is None:
        raise KeyError(f"未知基准策略 {key!r}，可选: {sorted(BENCHMARK_META)}")
    return cls(**params)


# ---------- 策略实现 ----------

class _HistoryStrategy(Strategy):
    """累积 bar 历史的基类：引擎只在调仓日调 on_bar，历史由策略自己记。"""

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._hist: dict[str, dict[str, list[float]]] = {}

    def _remember(self, sym: str, b: Bar) -> None:
        h = self._hist.setdefault(sym, {"close": [], "high": [], "low": []})
        h["close"].append(float(b.close))
        h["high"].append(float(b.high))
        h["low"].append(float(b.low))


@register_strategy("benchmark_sma_cross",
                   {"label": "基准 · 双均线金叉死叉",
                    "params": {"symbol": "标的", "fast": "快线窗口", "slow": "慢线窗口"}})
class SmaCrossStrategy(_HistoryStrategy):
    """SMA(fast) 上穿 SMA(slow) → 满仓；下穿 → 清仓。"""

    def __init__(self, symbol: str = "600519.SH", fast: int = 5, slow: int = 20) -> None:
        super().__init__(symbol=symbol, fast=fast, slow=slow)

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        sym = self.params["symbol"]
        if sym not in bars:
            return []
        self._remember(sym, bars[sym])
        closes = self._hist[sym]["close"]
        f, s = self.params["fast"], self.params["slow"]
        if len(closes) < s:
            return []                      # 慢线未成形，不交易
        fast_ma = sum(closes[-f:]) / f
        slow_ma = sum(closes[-s:]) / s
        return [(sym, 1.0 if fast_ma > slow_ma else 0.0)]


@register_strategy("benchmark_momentum_rotation",
                   {"label": "基准 · ETF 动量单强轮动",
                    "params": {"symbols": "标的池", "window": "动量窗口"}})
class MomentumRotationStrategy(_HistoryStrategy):
    """22 日涨幅最高者满仓；全部为负 → 空仓（显式清仓目标）。"""

    def __init__(self, symbols: list[str] | None = None, window: int = 22) -> None:
        super().__init__(symbols=symbols or ["510300.SH", "159915.SZ"], window=window)

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        syms = [s for s in self.params["symbols"] if s in bars]
        if not syms:
            return []
        for s in syms:
            self._remember(s, bars[s])
        w = self.params["window"]
        scores = {}
        for s in syms:
            closes = self._hist[s]["close"]
            if len(closes) > w and closes[-1 - w] > 0:
                scores[s] = closes[-1] / closes[-1 - w] - 1.0
        if not scores:
            return []
        best = max(scores, key=lambda k: scores[k])
        if scores[best] > 0:
            return [(best, 1.0)]
        # 动量全负 → 空仓：显式清仓所有持仓（引擎 zero_out 语义）
        # 用自己记过的历史 key，不依赖 ctx.account —— 便于纯单元测试
        return [(s, 0.0) for s in self._hist]


@register_strategy("benchmark_turtle_donchian",
                   {"label": "基准 · 海龟唐奇安突破",
                    "params": {"symbol": "标的", "entry": "入场窗口", "exit": "离场窗口",
                               "atr_window": "ATR 窗口", "daily_risk": "单日风险占比"}})
class TurtleDonchianStrategy(_HistoryStrategy):
    """唐奇安突破入场 + ATR 反比仓位：weight = daily_risk * close / ATR（封顶 1）。

    突破判定用**前 N 日**极值（不含当日），海龟法则原始口径。
    """

    def __init__(self, symbol: str = "600519.SH", entry: int = 20, exit: int = 10,
                 atr_window: int = 20, daily_risk: float = 0.01) -> None:
        super().__init__(symbol=symbol, entry=entry, exit=exit,
                         atr_window=atr_window, daily_risk=daily_risk)
        self._weight: float | None = None   # None = 尚无信号，不交易
        self._emitted: float | None = None  # 上次实际下发的目标（只在翻转时下发）
        self._prev_close: dict[str, float] = {}

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        sym = self.params["symbol"]
        if sym not in bars:
            return []
        b = bars[sym]
        self._remember(sym, b)
        h = self._hist[sym]
        n_in, n_out, n_atr = self.params["entry"], self.params["exit"], self.params["atr_window"]
        if len(h["high"]) <= max(n_in, n_atr):
            self._prev_close[sym] = float(b.close)
            return []

        don_high = max(h["high"][-n_in - 1:-1])           # 前 entry 日最高
        don_low = min(h["low"][-n_out - 1:-1])            # 前 exit 日最低
        # ATR：True Range = max(高低差, |高-昨收|, |低-昨收|) 的均值
        trs = []
        for i in range(-n_atr, 0):
            hi, lo = h["high"][i], h["low"][i]
            pc = h["close"][i - 1] if i - 1 >= -len(h["close"]) else hi
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        atr = sum(trs) / len(trs) if trs else float("nan")
        close = float(b.close)

        changed = False
        if close > don_high and atr > 0:
            # ATR 反比仓位：单日波动 = daily_risk * NAV
            new_w = min(1.0, self.params["daily_risk"] * close / atr)
            if self._weight is None or self._weight <= 0:
                self._weight = new_w
                changed = True
        elif close < don_low:
            if self._weight is None or self._weight > 0:
                self._weight = 0.0
                changed = True
        self._prev_close[sym] = close
        # 海龟语义：入场一次性建仓、出场一次性清仓 —— 只在状态翻转时下发目标，
        # 平时返回 []（no-op），避免引擎把固定权重误当成每日再平衡目标。
        if changed and self._weight != self._emitted:
            self._emitted = self._weight
            return [(sym, self._weight)]
        return []


@register_strategy("benchmark_grid_trading",
                   {"label": "基准 · 网格交易",
                    "params": {"symbol": "标的", "grid_pct": "网格间距", "levels": "半区格数"}})
class GridTradingStrategy(_HistoryStrategy):
    """锚定首日收盘价：每偏离一格（grid_pct）反向调 1/(2*levels) 仓位。

    涨得越满仓越低、跌得越深仓越重 —— 网格的本质（震荡市高抛低吸）。
    """

    def __init__(self, symbol: str = "510300.SH", grid_pct: float = 0.05,
                 levels: int = 8) -> None:
        super().__init__(symbol=symbol, grid_pct=grid_pct, levels=levels)
        self._anchor: float | None = None

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        sym = self.params["symbol"]
        if sym not in bars:
            return []
        close = float(bars[sym].close)
        if self._anchor is None:
            self._anchor = close
            return [(sym, 0.5)]
        rel = close / self._anchor - 1.0
        steps = rel / self.params["grid_pct"]
        frac = 0.5 - 0.5 * steps / self.params["levels"]
        return [(sym, min(1.0, max(0.0, frac)))]
