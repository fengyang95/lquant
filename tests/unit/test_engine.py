"""回测引擎 + 绩效指标 + 滑点单元测试。

数据全部合成（确定性种子），不依赖网络与本地库。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.metrics import max_drawdown, perf_from_nav
from lquant.backtest.slippage import make_slippage
from lquant.backtest.strategy import get_strategy
from lquant.backtest.strategy.base import Context, Strategy


# ---------- 合成数据 ----------

def make_daily(n_days: int = 120, symbols: tuple[str, ...] = ("600000", "000001", "300750"),
               seed: int = 7) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    prices = {s: 10.0 + i * 5 for i, s in enumerate(symbols)}
    d0 = date(2026, 1, 5)
    dates = [d0 + timedelta(days=i // 5 * 7 + i % 5) for i in range(n_days)]
    dates = sorted(set(dates))
    for d in dates:
        for s in symbols:
            ret = rng.normal(0.0005, 0.02)
            pre = prices[s]
            close = max(pre * (1 + ret), 1.0)
            prices[s] = close
            open_ = pre * (1 + rng.normal(0, 0.005))
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            vol = float(rng.integers(1e5, 1e6))
            rows.append({"trade_date": d, "symbol": s, "open": round(open_, 2),
                         "high": round(high, 2), "low": round(low, 2),
                         "close": round(close, 2), "pre_close": round(pre, 2),
                         "volume": vol, "amount": vol * close})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def with_mom(df: pl.DataFrame, n: int = 5, col: str = "mom5") -> pl.DataFrame:
    return df.sort("trade_date").with_columns(
        (pl.col("close").pct_change(n).over("symbol")).alias(col))


class AllInOne(Strategy):
    """把全部权重压到第一只票 —— 断言可预期的行为。"""

    def on_bar(self, ctx: Context, bars):
        sym = sorted(bars)[0]
        return [(sym, 1.0)]


# ---------- 引擎 ----------

def test_engine_basic_nav_and_positions():
    df = make_daily()
    cfg = EngineConfig(initial_cash=1_000_000)
    eng = Engine(AllInOne(), config=cfg)
    res = eng.run(df)
    assert len(res.nav) == df["trade_date"].n_unique()
    first_d, first_nav = res.nav[0]
    assert first_nav == pytest.approx(1_000_000)          # 首日还没成交
    assert res.nav[-1][1] > 0
    assert all(np.isfinite(v) for _, v in res.nav)
    # 防未来函数：首日信号 T+1 开盘才成交 → 首日无持仓
    assert not res.positions[first_d]
    # 成交后持有单票
    held = [d for d, p in res.positions.items() if p]
    assert held and all(len(p) <= 1 for p in (res.positions[d] for d in held))


def test_engine_next_open_fill_delay():
    """策略 T 日出信号，T+1 开盘才可能见到第一笔成交。"""
    df = make_daily(n_days=30)
    eng = Engine(AllInOne(), config=EngineConfig())
    res = eng.run(df)
    trades = res.trades_frame()
    dates = sorted(set(df["trade_date"].to_list()))
    assert trades is not None and len(trades) > 0
    assert min(trades["trade_date"].to_list()) > dates[0]


def test_engine_factor_topn_strategy():
    df = with_mom(make_daily())
    strat = get_strategy("factor_topn", factor="mom5", top_n=2)
    eng = Engine(strat, config=EngineConfig())
    res = eng.run(df, extra_fields=["mom5"])
    assert res.nav[-1][1] > 0
    trades = res.trades_frame()
    if len(trades):
        # 单日买单数不超过 top_n（卖出是清仓换股，另算）
        buys = trades.filter(pl.col("side") == "buy") if "side" in trades.columns else trades
        per_day = buys.group_by("trade_date").len()
        assert per_day["len"].max() <= 2


def test_engine_rebalance_none_freezes_portfolio():
    df = make_daily()
    eng = Engine(AllInOne(), config=EngineConfig(rebalance="none"))
    res = eng.run(df)
    assert all(not p for p in res.positions.values())     # 永不下单


# ---------- 滑点 ----------

def test_slippage_pct_raises_buy_lowers_sell():
    from lquant.backtest.events import Side

    slip = make_slippage("pct", rate=0.01)
    assert slip.apply(100.0, Side.BUY) == pytest.approx(101.0)
    assert slip.apply(100.0, Side.SELL) == pytest.approx(99.0)


def test_slippage_cost_reduces_nav():
    df = with_mom(make_daily())
    strat = get_strategy("factor_topn", factor="mom5", top_n=1)
    nav_clean = Engine(strat, config=EngineConfig()).run(df, extra_fields=["mom5"]).nav[-1][1]
    nav_cost = Engine(strat, config=EngineConfig(slippage="pct", slippage_params={"rate": 0.005}),
                      ).run(df, extra_fields=["mom5"]).nav[-1][1]
    assert nav_cost < nav_clean


# ---------- 指标 ----------

def test_max_drawdown_known_series():
    nav = [1.0, 2.0, 1.5, 1.0, 1.2]
    mdd, peak_i, trough_i = max_drawdown(nav)
    assert mdd == pytest.approx(-0.5)                      # 2.0 → 1.0（负值口径）
    assert (peak_i, trough_i) == (1, 3)


def test_perf_from_nav_keys():
    df = with_mom(make_daily())
    res = Engine(AllInOne(), config=EngineConfig()).run(df)
    perf = perf_from_nav([v for _, v in res.nav],
                         dates=[d for d, _ in res.nav])
    for k in ("total_return", "annual_return", "sharpe", "max_drawdown"):
        assert k in perf
    assert -1.0 < perf["total_return"]                     # 不会归零以下


def test_perf_zero_vol_sharpe_safe():
    """零波动序列 sharpe 为 NaN（0/0），不应抛异常。"""
    import math

    perf = perf_from_nav([100.0] * 60)
    assert math.isnan(perf["sharpe"]) or math.isfinite(perf["sharpe"])
    assert perf["total_return"] == 0.0
