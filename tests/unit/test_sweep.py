"""参数扫描（sweep）：逐档改 strategy 参数跑回测，返回统一指标表。

数据合成（确定性种子），不依赖网络与本地库。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
from lquant.backtest.sweep import (
    SweepSpec,
    run_sweep,
    run_sweep_auto,
    run_sweep_vectorized,
)


def _panel(n_days: int = 120) -> pl.DataFrame:
    symbols = ("600000", "000001", "300750", "600036",
               "000002", "601318", "600519", "000333")
    rng = np.random.default_rng(7)
    rows = []
    prices = {s: 10.0 + i * 5 for i, s in enumerate(symbols)}
    d0 = date(2026, 1, 5)
    dates = sorted({d0 + timedelta(days=i // 5 * 7 + i % 5) for i in range(n_days)})
    for d in dates:
        for s in symbols:
            ret = rng.normal(0.0005, 0.02)
            pre = prices[s]
            close = max(pre * (1 + ret), 1.0)
            prices[s] = close
            rows.append({"trade_date": d, "symbol": s, "open": close,
                         "high": close * 1.01, "low": close * 0.99,
                         "close": close, "pre_close": pre,
                         "volume": 1e5, "amount": 1e5 * close})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    return df.sort("trade_date").with_columns(
        (pl.col("close").pct_change(5).over("symbol")).alias("mom5"))


def test_sweep_topn_returns_grid_table():
    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    out = run_sweep(df, "top_n", [1, 2, 3], spec, strategy_cls=FactorTopNStrategy)
    assert out["value"].to_list() == [1, 2, 3]
    assert "total_return" in out.columns
    assert len(out) == 3
    assert all(-1 < v < 10 for v in out["total_return"].to_list())


def test_sweep_more_topn_changes_nav():
    """持仓数不同 → 结果不同（否则扫了等于没扫）。"""
    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    out = run_sweep(df, "top_n", [1, 5], spec, strategy_cls=FactorTopNStrategy)
    r1 = out.filter(pl.col("value") == 1)["total_return"][0]
    r5 = out.filter(pl.col("value") == 5)["total_return"][0]
    assert r1 != r5


def test_sweep_unknown_param_rejected():
    df = _panel()
    spec = SweepSpec(factor="mom5")
    # 未知参数在构造 strategy 时即炸（TypeError/KeyError 都算 fail-fast）
    with pytest.raises(TypeError):
        run_sweep(df, "no_such_param", [1, 2], spec, strategy_cls=FactorTopNStrategy)


def test_sweep_output_schema_stable():
    """前端依赖：列集稳定 + 除 value/n_trades 外都是标量数值。

    专门盯 turnover：它源指标是个嵌套 dict，这里必须展平成单标量，
    否则前端画轴拿到一个 struct 直接崩。"""
    import polars as pl

    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    out = run_sweep(df, "top_n", [1, 2], spec, strategy_cls=FactorTopNStrategy)
    names = out.collect_schema().names()
    assert names == ["value", "total_return", "annual_return", "sharpe",
                     "max_drawdown", "n_trades", "turnover"]
    for c in ("total_return", "annual_return", "sharpe", "max_drawdown", "turnover"):
        assert out[c].dtype in (pl.Float64, pl.Int64), f"{c} 必须是标量数值，实际 {out[c].dtype}"
    # 数值语义：turnover 是单调标量（源指标是成交额，非 0-1 比率），NaN 是合法占位
    import math

    def _finite_nonneg(v):
        return math.isnan(v) or (v >= 0 and math.isfinite(v))

    assert all(_finite_nonneg(v) for v in out["turnover"].fill_null(0.0).to_list())

# --------------------------------------------------------------------------- #
# B5：Polars 向量化近似快扫
# --------------------------------------------------------------------------- #

def _panel_gapfree(n_days: int = 250, seed: int = 3,
                   noise: float = 0.004) -> pl.DataFrame:
    """无隔夜跳空 + 持续信号的合成面板（专供「向量化 vs 事件引擎」对比）。

    两点刻意设计，把撮合细节差异压到最小、留下纯策略语义对比：

    1. `open[d] = close[d-1]`：消除隔夜跳空。事件引擎「T 日收盘定信号、T+1 开盘
       成交、收盘估值」与向量化的「T+1 开盘买入持有」在**同一价格口径**上比较，
       否则每次换仓的跳空都会被资金不足拒单放大（回测差异不再是模型差异）。
    2. 每只标的带不同恒定漂移 mu，因子列直接等于 mu：topN 名单稳定、换手接近 0，
       成本与撮合随机性最小，只剩收益量级/排序的比较。
    """
    symbols = ("600000", "000001", "300750", "600036",
               "000002", "601318", "600519", "000333")
    mu = {s: 0.0009 * (i - (len(symbols) - 1) / 2) for i, s in enumerate(symbols)}
    rng = np.random.default_rng(seed)
    prices = {s: 10.0 + i * 5 for i, s in enumerate(symbols)}
    d0 = date(2026, 1, 5)
    dates = sorted({d0 + timedelta(days=i // 5 * 7 + i % 5) for i in range(n_days)})
    rows = []
    for d in dates:
        for s in symbols:
            pre = prices[s]
            op = pre                                   # 无隔夜跳空
            close = max(pre * (1 + mu[s] + rng.normal(0, noise)), 1.0)
            prices[s] = close
            rows.append({"trade_date": d, "symbol": s, "open": op,
                         "high": max(op, close) * 1.001, "low": min(op, close) * 0.999,
                         "close": close, "pre_close": pre,
                         "volume": 1e9, "amount": 1e9 * close})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    return df.sort("trade_date").with_columns(
        pl.col("symbol").replace_strict(mu, return_dtype=pl.Float64).alias("sig"))


def _order_corr(a: list[float], b: list[float]) -> float:
    """秩相关（Spearman 的等价写法，避免引入 scipy 依赖）。"""
    ra = np.argsort(np.argsort(np.asarray(a, dtype=float)))
    rb = np.argsort(np.argsort(np.asarray(b, dtype=float)))
    return float(np.corrcoef(ra, rb)[0, 1])


def test_sweep_vectorized_output_contract():
    """向量化路径输出与事件引擎同一契约（列集/类型/行序）。"""
    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    out = run_sweep_vectorized(df, "top_n", [3, 1, 2], spec)
    assert out.collect_schema().names() == [
        "value", "total_return", "annual_return", "sharpe",
        "max_drawdown", "n_trades", "turnover"]
    assert out["value"].to_list() == [3, 1, 2]        # 保持传入档序（同 run_sweep）
    for c in ("total_return", "annual_return", "sharpe", "max_drawdown", "turnover"):
        assert out[c].dtype in (pl.Float64, pl.Int64), f"{c} 必须是标量数值"
    assert out["n_trades"].dtype == pl.Int64
    assert out["total_return"].is_finite().all()


def test_sweep_vectorized_ranking_matches_event_engine():
    """B5 正确性闸：近似只用于筛参数，必须与事件引擎**同序**。

    在无隔夜跳空 + 持续信号的面板上，向量化的开盘价持有收益与引擎的
    「T+1 开盘成交 + 收盘估值」严格同口径，残差只来自近似成本模型：
    线性化佣金（引擎是最低 5 元/单）、等权漂移再平衡产生的小额委托、
    100 股手数取整、资金不足拒单。

    实测（见 tolerance 下方注释）：秩相关 = 1.0，|Δtotal_return| ≤ 2 个百分点。
    因此断言秩相关 ≥ 0.9、argmax 一致、绝对差 ≤ 0.02（2pp）——
    容差留给成本近似，不放宽到「排序都可以乱」。
    """
    df = _panel_gapfree()
    spec = SweepSpec(factor="sig", rebalance="monthly")
    values = [1, 2, 3, 4, 5]

    ev = run_sweep(df, "top_n", values, spec, strategy_cls=FactorTopNStrategy)
    ve = run_sweep_vectorized(df, "top_n", values, spec)

    e = ev["total_return"].to_list()
    v = ve["total_return"].to_list()
    assert _order_corr(e, v) >= 0.9, f"排序不一致: event={e} vector={v}"
    assert int(np.argmax(e)) == int(np.argmax(v))
    # 容差 2pp：纯近似成本模型导致，实测量级 < 1pp；放宽一倍防种子抖动
    assert max(abs(x - y) for x, y in zip(e, v, strict=True)) <= 0.02


def test_sweep_vectorized_cost_monotonicity():
    """成本单调性：滑点越高，收益不可能更好（同一档位逐个比）。"""
    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    values = [1, 2, 3]
    low = run_sweep_vectorized(df, "top_n", values, spec, slippage_rate=0.0)
    high = run_sweep_vectorized(df, "top_n", values, spec, slippage_rate=0.01)
    for a, b in zip(low["total_return"].to_list(),
                    high["total_return"].to_list(), strict=True):
        assert b <= a + 1e-12, f"高滑点收益反而更高: {a} -> {b}"


def test_sweep_vectorized_cancel_honoured():
    """cancel_check 返回 True → JobCanceled（协作式取消不得被跳过）。"""
    from lquant.server.jobs import JobCanceled

    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    with pytest.raises(JobCanceled):
        run_sweep_vectorized(df, "top_n", [1, 2], spec, cancel_check=lambda: True)


def test_sweep_vectorized_rejects_unsupported_param():
    """向量化路径只支持 top_n：其它参数必须显式拒绝，不得静默给错结果。"""
    df = _panel()
    with pytest.raises(NotImplementedError):
        run_sweep_vectorized(df, "factor", [1], SweepSpec(factor="mom5"))


def test_sweep_vectorized_rebalance_none_is_cash():
    """rebalance=none：永不调仓 → 净值恒为 1（与引擎「无委托」一致）。"""
    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="none")
    out = run_sweep_vectorized(df, "top_n", [1, 2], spec)
    assert out["total_return"].to_list() == [0.0, 0.0]
    assert out["n_trades"].to_list() == [0, 0]


def test_run_sweep_auto_switches_by_grid_size(monkeypatch):
    """auto 选择：小网格 = 事件引擎（真源），大网格 = 向量化（近似）。"""
    from lquant.backtest import sweep as sweep_mod

    df = _panel()
    spec = SweepSpec(factor="mom5", rebalance="monthly")
    seen: list[str] = []
    monkeypatch.setattr(sweep_mod, "run_sweep",
                        lambda *a, **k: seen.append("event") or pl.DataFrame())
    monkeypatch.setattr(sweep_mod, "run_sweep_vectorized",
                        lambda *a, **k: seen.append("vector") or pl.DataFrame())

    run_sweep_auto(df, "top_n", [1, 2], spec, engine="auto")
    run_sweep_auto(df, "top_n", list(range(1, sweep_mod.VECTOR_AUTO_MIN_POINTS + 1)),
                   spec, engine="auto")
    run_sweep_auto(df, "top_n", [1, 2], spec, engine="vector")
    assert seen == ["event", "vector", "vector"]
