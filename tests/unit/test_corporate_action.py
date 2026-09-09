"""公司行为（分红/送转）的除权调整测试。

数据层只有后复权因子（adj_factor），没有分红现金金额，
引擎采用**份额调整法**：除权日 qty ×= factor_t / factor_{t-1}，
等价于分红全部再投资，净值连续无跳变。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.rules.model import RuleSet
from lquant.backtest.strategy.base import Context, Strategy


def zero_fee_ruleset() -> RuleSet:
    base = {
        "commission": {"rate": 0.0, "min": 0.0, "per_order": True},
        "tax": {"rate": 0.0},
        "transfer_fee": {"rate": 0.0},
        "lot_size": 1,
        "t_plus": 1,
        "price_limit": {"mode": "by_board", "values": {"main": 0.10}},
    }
    return RuleSet(market="CN", currency="CNY", default=dict(base),
                   etf=dict(base), exceptions={})


def exdiv_df(adj_factor_day3: float, day3_px: float) -> pl.DataFrame:
    """3 天 1 标的：d2 开盘买入满仓，d3 除权（价格降到 day3_px，因子跳变）。

    d1/d2: 价格 10，因子 1.0；d3: 价格 day3_px，因子 adj_factor_day3。
    d3 的 pre_close 用交易所口径（除权调整后昨收 = day3_px），避免误判涨跌停。
    """
    rows = []
    d0 = date(2026, 1, 5)
    series = [(10.0, 1.0), (10.0, 1.0), (day3_px, adj_factor_day3)]
    pre = 10.0
    for i, (px, f) in enumerate(series):
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "600000.SH",
                     "open": px, "high": px, "low": px, "close": px, "pre_close": pre,
                     "volume": 2e9, "amount": 2e9 * px, "adj_factor": f})
        pre = day3_px if i == 1 else px
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


class BuyHold(Strategy):
    def on_bar(self, ctx: Context, bars):
        return [("600000.SH", 1.0)]


def run_exdiv(adj_factor_day3: float, day3_px: float):
    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=1_000, participation=0.5)
    eng = Engine(BuyHold(), ruleset=zero_fee_ruleset(), config=cfg)
    return eng, eng.run(exdiv_df(adj_factor_day3, day3_px))


def test_cash_dividend_nav_continuity():
    """分红除权：d3 价格 10 → 9（每股 1 元分红，因子比 10/9）。

    d2 买入 100,000 股 @10；d3 开盘前份额 ×10/9 → 111,111.11 股，
    价值 = 111,111.11 × 9 = 1,000,000 —— 净值连续，无跳变。
    """
    eng, res = run_exdiv(10.0 / 9.0, 9.0)
    navs = dict(res.nav)
    assert navs[date(2026, 1, 6)] == pytest.approx(1_000_000, abs=1e-6)
    assert navs[date(2026, 1, 7)] == pytest.approx(1_000_000, abs=1e-6)

    pos = eng.account.positions["600000.SH"]
    assert pos.qty == pytest.approx(100_000 * 10 / 9, rel=1e-12)
    # 总成本恒等：qty × avg_cost 不变（100,000 × 10 = 1,000,000）
    assert pos.qty * pos.avg_cost == pytest.approx(1_000_000, abs=1e-6)
    # lots 同步放大（T+N 可卖约束基于份额）
    assert pos.lots[0][1] == pytest.approx(100_000 * 10 / 9, rel=1e-12)


def test_split_qty_doubles():
    """2 送 2（1 拆 2）：价格减半，份额翻倍，净值不变。"""
    eng, res = run_exdiv(2.0, 5.0)
    pos = eng.account.positions["600000.SH"]
    assert pos.qty == pytest.approx(200_000)
    assert res.nav[-1][1] == pytest.approx(1_000_000, abs=1e-6)


def test_no_adj_factor_column_unchanged():
    """没有 adj_factor 列时行为与旧版完全一致（默认因子 1.0，不调整）。"""
    df = exdiv_df(10.0 / 9.0, 9.0).drop("adj_factor")
    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=1_000, participation=0.5)
    res = Engine(BuyHold(), ruleset=zero_fee_ruleset(), config=cfg).run(df)
    # 净值在除权日会真实下跌（分红现金未入账 —— 无因子数据时预期行为）
    assert dict(res.nav)[date(2026, 1, 7)] == pytest.approx(900_000, abs=1e-6)


def test_suspended_day_catches_up():
    """除权日停牌（无 bar）：复牌后按累计因子比一次性补齐。"""
    rows = []
    d0 = date(2026, 1, 5)
    # d3 该标的整行缺失（停牌），d4 复牌价 5、因子 2.0
    series = [(10.0, 1.0), (10.0, 1.0), None, (5.0, 2.0)]
    pre = 10.0
    for i, s in enumerate(series):
        if s is None:
            continue
        px, f = s
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "600000.SH",
                     "open": px, "high": px, "low": px, "close": px, "pre_close": pre,
                     "volume": 2e9, "amount": 2e9 * px, "adj_factor": f})
        pre = px
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=1_000, participation=0.5)
    eng = Engine(BuyHold(), ruleset=zero_fee_ruleset(), config=cfg)
    res = eng.run(df)
    pos = eng.account.positions["600000.SH"]
    assert pos.qty == pytest.approx(200_000)
    assert res.nav[-1][1] == pytest.approx(1_000_000, abs=1e-6)
