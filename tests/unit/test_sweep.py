"""参数扫描（sweep）：逐档改 strategy 参数跑回测，返回统一指标表。

数据合成（确定性种子），不依赖网络与本地库。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
from lquant.backtest.sweep import SweepSpec, run_sweep


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