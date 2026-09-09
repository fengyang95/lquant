"""Cash conservation invariant tests: random data, real fees, slippage, halts, T+1."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.rules.model import RuleSet
from lquant.backtest.strategy.base import Context, Strategy

SYMBOLS = ("600000.SH", "000001.SZ", "300750.SZ")


def fee_ruleset() -> RuleSet:
    base = {
        "commission": {"rate": 0.0003, "min": 5.0, "per_order": True},
        "tax": {"rate": 0.0005},
        "transfer_fee": {"rate": 0.00001},
        "lot_size": 100,
        "t_plus": 1,
        "price_limit": {"mode": "by_board", "values": {"main": 0.10}},
    }
    return RuleSet(market="CN", currency="CNY", default=dict(base),
                   etf=dict(base), exceptions={})


def random_df(n_days: int = 80) -> pl.DataFrame:
    """GBM prices + random halts + momentum factor."""
    rng = np.random.default_rng(7)
    rows = []
    d0 = date(2026, 1, 5)
    dates = [d0 + timedelta(days=i) for i in range(n_days)]
    for sym in SYMBOLS:
        px = 10.0
        for d in dates:
            px = max(px * (1 + rng.normal(0.001, 0.02)), 1.0)
            halted = rng.random() < 0.03
            vol = 0.0 if halted else float(rng.integers(1e5, 1e6))
            o = px * (1 + rng.normal(0, 0.005))
            rows.append({
                "trade_date": d, "symbol": sym,
                "open": o, "high": max(o, px) * 1.001, "low": min(o, px) * 0.999,
                "close": px, "pre_close": 0.0,
                "volume": vol, "amount": vol * px,
                "mom": rng.normal(0, 1),
            })
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    df = df.sort(["symbol", "trade_date"]).with_columns(
        pl.col("close").shift(1).over("symbol").fill_null(pl.col("open")).alias("pre_close"))
    return df


class Top2(Strategy):
    """每日等权持有动量最高的 2 只，换手充分。"""

    def on_bar(self, ctx: Context, bars):
        scored = [(s, b.fields.get("mom", 0.0)) for s, b in bars.items() if not b.halted]
        scored.sort(key=lambda x: -x[1])
        return [(s, 0.5) for s, _ in scored[:2]]


def run_random():
    cfg = EngineConfig(initial_cash=1_000_000, slippage="pct",
                       slippage_params={"rate": 0.001},
                       cash_buffer=0.001, min_order_value=1_000,
                       participation=0.05)
    eng = Engine(Top2(), ruleset=fee_ruleset(), config=cfg)
    res = eng.run(random_df(), extra_fields=["mom"])
    return eng, res


def test_cash_conservation_random():
    """守恒式：initial − Σ买 + Σ卖 − Σ费 ≡ 期末现金；final_nav ≡ 现金 + 持仓市值。"""
    eng, res = run_random()
    a = eng.account
    buys = sum(t.qty * t.price for t in res.trades if t.side.value == "buy")
    sells = sum(t.qty * t.price for t in res.trades if t.side.value == "sell")
    fees = sum(t.fee for t in res.trades)
    assert fees > 0 and len(res.trades) > 10

    assert a.cash == pytest.approx(1_000_000 - buys + sells - fees, rel=1e-9, abs=1e-4)

    last = random_df().filter(pl.col("trade_date") == pl.col("trade_date").max())
    closes = dict(zip(last["symbol"], last["close"], strict=True))
    mv = sum(p.qty * closes[s] for s, p in a.positions.items() if p.qty)
    assert res.nav[-1][1] == pytest.approx(a.cash + mv, rel=1e-9, abs=1e-4)

    assert res.metrics["total_fee"] == pytest.approx(fees, rel=1e-9)
    assert res.metrics["n_trades"] == len(res.trades)
    assert all(np.isfinite(v) and v > 0 for _, v in res.nav)


def test_corporate_action_does_not_touch_cash():
    """除权份额调整只动份额/成本，不动现金；守恒式不受影响。"""
    eng, res = run_random()
    a = eng.account
    for sym, pos in list(a.positions.items()):
        if pos.qty > 0:
            qty_before = pos.qty
            cost_before = pos.qty * pos.avg_cost
            a.apply_corporate_action(sym, 1.5)
            assert pos.qty == pytest.approx(qty_before * 1.5)
            assert pos.qty * pos.avg_cost == pytest.approx(cost_before, abs=1e-6)

    buys = sum(t.qty * t.price for t in res.trades if t.side.value == "buy")
    sells = sum(t.qty * t.price for t in res.trades if t.side.value == "sell")
    fees = sum(t.fee for t in res.trades)
    assert a.cash == pytest.approx(1_000_000 - buys + sells - fees, rel=1e-9, abs=1e-4)
