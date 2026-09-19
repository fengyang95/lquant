"""审查修复回归测试：MDD 初始峰值 / Sortino 标准口径 / get_price 防未来函数 /
trace 看门狗超时 / turnover 净值归一 / PARTIAL 挂单跨日续撮 / risk_vs_benchmark 对齐校验。

每个用例对应一次真实缺陷 —— 指标错了不抛异常、只让策略看起来更好，所以必须钉死。
"""
from __future__ import annotations

import math
from datetime import date

import polars as pl
import pytest

from lquant.backtest.engine import Context, Engine, EngineConfig, Strategy
from lquant.backtest.metrics import perf_from_returns, turnover_from_trades
from lquant.backtest.sandbox import StrategyExecutionTimeout, run_with_deadline

# ---------- metrics ----------

def test_mdd_includes_initial_peak():
    """首日亏 10% 后反弹 5%：MDD 必须是 -10%（相对初始净值 1.0），而不是 0。"""
    p = perf_from_returns([-0.10, 0.05])
    assert p["max_drawdown"] == pytest.approx(-0.10)


def test_sortino_uses_standard_downside_deviation():
    """下行偏差 = sqrt(mean(min(r,0)^2))（全周期计入），不是负收益子集的样本 std。"""
    import numpy as np

    r = np.array([0.05, 0.03, -0.02, -0.04])
    p = perf_from_returns(r)
    dd = math.sqrt(float(np.mean(np.minimum(r, 0.0) ** 2))) * math.sqrt(252)
    assert p["sortino"] == pytest.approx(p["annual_return"] / dd, rel=1e-9)
    # 全正收益：下行偏差为 0 → Sortino 无意义，必须是 nan 而不是 +inf
    p2 = perf_from_returns([0.01] * 10)
    assert math.isnan(p2["sortino"])


def test_turnover_ratio_normalized_by_nav():
    """有净值序列时 turnover_per_period 是无量纲比率 = 总成交额/天数/平均净值。"""
    trades = [(date(2026, 1, 5), 100.0), (date(2026, 1, 6), -50.0)]
    nav = [(date(2026, 1, 5), 1_000.0), (date(2026, 1, 6), 1_100.0)]
    to = turnover_from_trades(trades, nav=nav)
    assert to["unit"] == "ratio"
    expected = 150.0 / 2 / 1_050.0
    assert to["turnover_per_period"] == pytest.approx(expected)
    # 无净值时不给假换手率
    to2 = turnover_from_trades(trades)
    assert to2["turnover_per_period"] is None and to2["unit"] == "amount"


# ---------- sandbox 看门狗 ----------

def test_run_with_deadline_preempts_infinite_loop():
    def _spin():
        while True:
            pass

    with pytest.raises(StrategyExecutionTimeout):
        run_with_deadline(_spin, timeout_s=0.3)


def test_run_with_deadline_returns_normally():
    assert run_with_deadline(lambda x: x + 1, 1, timeout_s=5) == 2


# ---------- jqapi get_price 防未来函数 ----------

def _bar(sym="600000.SH", close=10.5, trade_date=None):
    from lquant.backtest.events import Bar

    return Bar(symbol=sym, trade_date=trade_date or date(2026, 1, 5), open=10.0,
               high=11.0, low=9.0, close=close, pre_close=10.0, volume=1e8, amount=1e9)


def _runner_with_bars():
    from lquant.backtest.jqapi import JQRunner

    runner = JQRunner("def initialize(context):\n    pass\n")
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    closes = [10.0, 11.0, 12.0]
    runner._dates = dates
    runner._bars_by_day = {
        d: {"600000.SH": _bar(close=c, trade_date=d)}
        for d, c in zip(dates, closes, strict=True)
    }
    runner._day_index = 2          # 站在 01-07 开盘决策时点
    return runner


def test_get_price_end_date_clamped_to_prev_day():
    """显式传 end_date=当日也拿不到当日 close —— 与 history 口径一致。"""
    runner = _runner_with_bars()
    df = runner._get_price("600000.SH", start_date="2026-01-05",
                           end_date="2026-01-07", fields=["close"])
    got = [float(v) for v in df["close"].to_list()]
    assert got == [10.0, 11.0]     # 12.0 是当日（未来）数据，必须被钳掉


def test_get_price_first_day_returns_empty():
    """首日决策前无任何历史数据 → 空结果而非当日数据。"""
    runner = _runner_with_bars()
    runner._day_index = 0
    df = runner._get_price("600000.SH", fields=["close"])
    assert len(df) == 0


# ---------- PARTIAL 挂单跨日续撮 ----------

class _AllIn(Strategy):
    def on_bar(self, ctx: Context, bars):
        return [(sorted(bars)[0], 1.0)]


def test_partial_order_carryover_across_days():
    """participation 截断产生的 PARTIAL 订单必须保留到次日继续撮合。

    成交量只有 1000 股、participation=0.1 → 每日最多成交 100 股；
    周频调仓下目标 10 万股只能靠同一订单跨日续撮 —— 若 PARTIAL 被丢弃，
    同一 order_id 的成交永远只出现在单日（下一次调仓的 cancel-and-replace
    语义下旧单被新目标单取代，属预期行为，不在本测试范围）。
    """
    n = 15
    d0 = date(2026, 1, 5)
    dates = [date.fromordinal(d0.toordinal() + i) for i in range(n)]
    rows = []
    for d in dates:
        rows.append({"trade_date": d, "symbol": "600000", "open": 10.0,
                     "high": 10.1, "low": 9.9, "close": 10.0, "pre_close": 10.0,
                     "volume": 1000.0, "amount": 10000.0})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    eng = Engine(_AllIn(), config=EngineConfig(initial_cash=1_000_000,
                                               rebalance="weekly"))
    res = eng.run(df)
    per_order_days: dict[str, set] = {}
    for f in res.trades:
        per_order_days.setdefault(f.order_id, set()).add(f.trade_date)
    multi_day = [oid for oid, ds in per_order_days.items() if len(ds) >= 3]
    assert multi_day, "PARTIAL 订单未跨日续撮：被当日丢弃了"


# ---------- risk_vs_benchmark 对齐契约 ----------

def test_risk_vs_benchmark_rejects_misaligned_lengths():
    from lquant.backtest.attribution import risk_vs_benchmark

    with pytest.raises(ValueError, match="逐日对齐"):
        risk_vs_benchmark([0.01] * 30, [0.008] * 25)


# ---------- Engine 多次 run 状态重置 ----------

def test_engine_rerun_reproduces_first_run():
    df = pl.DataFrame([
        {"trade_date": d, "symbol": "600000", "open": 10.0, "high": 10.1,
         "low": 9.9, "close": 10.0 + 0.1 * i, "pre_close": 10.0 if i == 0 else 10.0 + 0.1 * (i - 1),
         "volume": 1e6, "amount": 1e7}
        for i, d in enumerate([date.fromordinal(date(2026, 1, 5).toordinal() + i)
                               for i in range(10)])
    ]).with_columns(pl.col("trade_date").cast(pl.Date))
    eng = Engine(_AllIn(), config=EngineConfig(initial_cash=1_000_000))
    r1 = eng.run(df)
    r2 = eng.run(df)
    assert [n for _, n in r1.nav] == pytest.approx([n for _, n in r2.nav])
    assert r1.trades[0].order_id == "o1" and r2.trades[0].order_id == "o1"


def test_engine_rerun_drops_stale_partial_orders():
    """run() 必须清空上一次 run 残留的 PARTIAL 挂单 —— 否则第二次 run
    首日就会把旧单剩余量撮合进去（结果静默错位）。"""
    n = 15
    d0 = date(2026, 1, 5)
    dates = [date.fromordinal(d0.toordinal() + i) for i in range(n)]

    def _df() -> pl.DataFrame:
        rows = [{"trade_date": d, "symbol": "600000", "open": 10.0, "high": 10.1,
                 "low": 9.9, "close": 10.0, "pre_close": 10.0,
                 "volume": 1000.0, "amount": 10000.0} for d in dates]
        return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))

    eng = Engine(_AllIn(), config=EngineConfig(initial_cash=1_000_000,
                                               rebalance="weekly"))
    r1 = eng.run(_df())
    assert eng._pending, "场景失效：第一次 run 结束时应留有 PARTIAL 残单"
    r2 = eng.run(_df())
    assert [(f.order_id, str(f.trade_date), f.qty) for f in r2.trades] == \
           [(f.order_id, str(f.trade_date), f.qty) for f in r1.trades]
    assert [n for _, n in r2.nav] == pytest.approx([n for _, n in r1.nav])
