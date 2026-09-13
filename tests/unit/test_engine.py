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
from lquant.backtest.slippage import NoSlippage, make_slippage
from lquant.backtest.strategy import get_strategy
from lquant.backtest.strategy.base import Context, Strategy

# ---------- 合成数据 ----------

def make_single(rows) -> pl.DataFrame:
    """确定性单标的 panel，供撮合价格断言。"""
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


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


# ---------- 撮合模式 ----------

def _match_mode_panel() -> pl.DataFrame:
    """单标的 600000；day2 的 open/close/vwap 互不相同，便于断言各撮合模式的成交价。

    所有价格都落在 pre_close±10% 涨停带内（A 股物理约束），且 day2 开盘
    不跳空 —— 资金充足性护栏会把「T 收盘定价、跳高开成交」的买单作废，
    测试数据若跳空，断言的就不是撮合模式本身的成交价了。
    """
    return make_single([
        # day1 收盘生成信号；vwap=amount/volume
        {"trade_date": date(2026, 1, 5), "symbol": "600000", "open": 10.0,
         "high": 10.2, "low": 9.8, "close": 10.0, "pre_close": 9.9,
         "volume": 100000.0, "amount": 1_000_000.0},
        # day2 pre_close=10.0（day1 收盘）；open=10.0（平开）, close=10.2,
        # vwap=2_050_000/200000=10.25 —— 三价互不相同且在 [9.0,11.0] 内
        {"trade_date": date(2026, 1, 6), "symbol": "600000", "open": 10.0,
         "high": 10.3, "low": 9.9, "close": 10.2, "pre_close": 10.0,
         "volume": 200000.0, "amount": 2_050_000.0},
        {"trade_date": date(2026, 1, 7), "symbol": "600000", "open": 10.2,
         "high": 10.4, "low": 10.1, "close": 10.3, "pre_close": 10.2,
         "volume": 100000.0, "amount": 1_030_000.0},
    ])


def _first_fill_price(mode: str) -> tuple[float, date]:
    df = _match_mode_panel()
    # 无滑点：断言的是撮合模式本身的成交价，不应被默认 pct 滑点污染。
    # 半仓目标：留出资金余量，避免资金护栏把「T 收盘定价、次日微幅波动」
    # 的正常成交误杀 —— 本测试只关心成交价，不关心资金边界。

    class Half(Strategy):
        def on_bar(self, ctx, bars):
            return [("600000", 0.5)]

    eng = Engine(Half(), config=EngineConfig(price_mode=mode, participation=1.0),
                 slippage=NoSlippage())
    res = eng.run(df)
    trades = res.trades_frame()
    assert trades is not None and len(trades) > 0
    f = trades.row(0, named=True)
    return f["price"], f["trade_date"]


def test_match_mode_next_open_fills_t1_open():
    price, d = _first_fill_price("next_open")
    assert price == pytest.approx(10.0)          # T+1 开盘（平开）
    assert d == date(2026, 1, 6)


def test_match_mode_next_vwap_fills_t1_vwap():
    price, d = _first_fill_price("next_vwap")
    assert price == pytest.approx(2_050_000.0 / 200_000.0)   # T+1 amount/volume
    assert d == date(2026, 1, 6)


def test_match_mode_next_close_fills_t1_close():
    price, d = _first_fill_price("next_close")
    assert price == pytest.approx(10.2)          # T+1 收盘
    assert d == date(2026, 1, 6)


def test_match_mode_same_close_fills_same_day_at_close():
    price, d = _first_fill_price("same_close")
    assert price == pytest.approx(10.0)          # T 日收盘
    assert d == date(2026, 1, 5)


def test_match_mode_limit_band_guards_fill_price():
    """平开但收盘封板的 bar：next_close 以涨停收盘价成交 → 不可成交。

    涨停校验必须落在**实际成交价**上（收盘/VWAP），不能只查开盘价 ——
    否则 next_close 会把根本买不进去的涨停价计进收益。
    """
    df = make_single([
        {"trade_date": date(2026, 1, 5), "symbol": "600000", "open": 10.0,
         "high": 10.0, "low": 9.8, "close": 10.0, "pre_close": 9.9,
         "volume": 100000.0, "amount": 1_000_000.0},
        # day2 平开 10.0，收盘直接封到涨停 11.0（pre_close=10.0 → +10%）
        {"trade_date": date(2026, 1, 6), "symbol": "600000", "open": 10.0,
         "high": 11.0, "low": 10.0, "close": 11.0, "pre_close": 10.0,
         "volume": 200000.0, "amount": 2_000_000.0},
    ])
    eng = Engine(AllInOne(), config=EngineConfig(price_mode="next_close",
                                                 participation=1.0),
                 slippage=NoSlippage())
    res = eng.run(df)
    # 买单被拒：以 11.0（涨停价）成交不可信
    assert res.trades == []
    assert any("涨停" in r[2] for r in res.rejected)


def test_unknown_match_mode_rejected():
    from lquant.backtest.broker import MATCH_MODES

    with pytest.raises(ValueError):
        Engine(AllInOne(), config=EngineConfig(price_mode="vwap")).run(_match_mode_panel())
    assert "next_open" in MATCH_MODES


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
    assert perf["total_return"] > -1.0                     # 不会归零以下


def test_perf_zero_vol_sharpe_safe():
    """零波动序列 sharpe 为 NaN（0/0），不应抛异常。"""
    import math

    perf = perf_from_nav([100.0] * 60)
    assert math.isnan(perf["sharpe"]) or math.isfinite(perf["sharpe"])
    assert perf["total_return"] == 0.0


# ---------- 停牌拒单 ----------

def test_engine_suspended_day_order_rejected():
    """is_suspended=True 的 bar：订单被拒且 reason='suspended'，不成交。"""
    df = make_single([
        {"trade_date": date(2026, 1, 5), "symbol": "600000", "open": 10.0,
         "high": 10.2, "low": 9.8, "close": 10.0, "pre_close": 9.9,
         "volume": 100000.0, "amount": 1_000_000.0, "is_suspended": False},
        # day2 停牌：有承接报价（close=pre_close）但不可交易
        {"trade_date": date(2026, 1, 6), "symbol": "600000", "open": 10.0,
         "high": 10.0, "low": 10.0, "close": 10.0, "pre_close": 10.0,
         "volume": 0.0, "amount": 0.0, "is_suspended": True},
        {"trade_date": date(2026, 1, 7), "symbol": "600000", "open": 10.0,
         "high": 10.2, "low": 9.8, "close": 10.1, "pre_close": 10.0,
         "volume": 100000.0, "amount": 1_010_000.0, "is_suspended": False},
    ])
    eng = Engine(AllInOne(), config=EngineConfig(), slippage=NoSlippage())
    res = eng.run(df)
    # 停牌日（1/6）不成交；复牌后（1/7）正常承接订单（引擎每日重出信号）
    assert all(f.trade_date != date(2026, 1, 6) for f in res.trades)
    suspended_rejects = [r for r in res.rejected if r[2] == "suspended"]
    assert suspended_rejects, "停牌日订单应被拒且 reason='suspended'"


# ---------- 补充：调仓频率 / 公司行为 / 成交量约束 ----------

def _zf_ruleset():
    from lquant.backtest.rules.model import RuleSet
    base = {
        "commission": {"rate": 0.0, "min": 0.0, "per_order": True},
        "tax": {"rate": 0.0}, "transfer_fee": {"rate": 0.0},
        "lot_size": 1, "t_plus": 1,
        "price_limit": {"mode": "by_board", "values": {"main": 0.10}},
    }
    return RuleSet(market="CN", currency="CNY", default=dict(base),
                   etf=dict(base), exceptions={})


def _bars_df():
    """10 个交易日单标的线性价格，open=close=前收（无跳空）。"""
    from datetime import date, timedelta

    import polars as pl
    d0 = date(2026, 1, 5)
    rows = []
    closes = [10 + i * 0.1 for i in range(10)]
    pre = closes[0]
    for i, c in enumerate(closes):
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "600000.SH",
                     "open": pre, "high": c * 1.001, "low": c * 0.999,
                     "close": c, "pre_close": pre, "volume": 2e9, "amount": 2e9 * c})
        pre = c
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


class _Always(Strategy):
    def on_bar(self, ctx, bars):
        return [("600000.SH", 1.0)]


def test_weekly_rebalance_only_first_day_of_week():
    cfg = EngineConfig(initial_cash=100_000, slippage="none", cash_buffer=0.0,
                       min_order_value=100, rebalance="weekly")
    res = Engine(_Always(), ruleset=_zf_ruleset(), config=cfg).run(_bars_df())
    # 2026-01-05 是周一：只有周一调仓 → 全程只有 1 笔买入
    buys = [t for t in res.trades if t.side.value == "buy"]
    assert len(buys) == 1


def test_monthly_rebalance_only_first_trading_day():
    cfg = EngineConfig(initial_cash=100_000, slippage="none", cash_buffer=0.0,
                       min_order_value=100, rebalance="monthly")
    res = Engine(_Always(), ruleset=_zf_ruleset(), config=cfg).run(_bars_df())
    buys = [t for t in res.trades if t.side.value == "buy"]
    # 信号在首个交易日生成，次日开盘成交 → 全月唯一一笔，成交日是第 2 个交易日
    assert len(buys) == 1 and buys[0].trade_date.day == 6


def test_rebalance_none_never_trades():
    cfg = EngineConfig(initial_cash=100_000, slippage="none", cash_buffer=0.0,
                       min_order_value=100, rebalance="none")
    res = Engine(_Always(), ruleset=_zf_ruleset(), config=cfg).run(_bars_df())
    assert res.trades == []


def test_participation_cap_limits_qty():
    """单日成交量 300 股、participation=0.5 → 单笔最多 150 股。"""
    from datetime import date, timedelta

    import polars as pl
    d0 = date(2026, 1, 5)
    rows = []
    for i in range(3):
        c = 10.0 + i
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "600000.SH",
                     "open": c, "high": c * 1.001, "low": c * 0.999, "close": c,
                     "pre_close": c, "volume": 300.0, "amount": 300.0 * c})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    cfg = EngineConfig(initial_cash=100_000, slippage="none", cash_buffer=0.0,
                       min_order_value=0.0, participation=0.5, rebalance="daily")
    res = Engine(_Always(), ruleset=_zf_ruleset(), config=cfg).run(df)
    assert res.trades and all(t.qty <= 150 for t in res.trades)


def test_corporate_action_adjusts_position():
    """复权因子跳变日：持仓份额按因子比放大，总市值连续（分红再投资口径）。"""
    from datetime import date, timedelta

    import polars as pl
    d0 = date(2026, 1, 5)
    rows = []
    # day3 因子 1.0 → 2.0（10送10），价格减半、收盘不涨不跌
    data = [(1.0, 10.0, 10.0), (2.0, 5.0, 5.0), (2.0, 5.0, 5.0)]
    pre = 10.0
    for i, (af, o, c) in enumerate(data):
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "600000.SH",
                     "open": o, "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                     "close": c, "pre_close": pre, "volume": 2e9, "amount": 2e9 * c,
                     "adj_factor": af})
        pre = c
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    eng = Engine(_Always(), ruleset=_zf_ruleset(),
                 config=EngineConfig(initial_cash=100_000, slippage="none",
                                     cash_buffer=0.0, min_order_value=100))
    res = eng.run(df)
    pos_day2 = res.positions[d0 + timedelta(days=2)]
    assert pos_day2["600000.SH"] == pytest.approx(100_000 / 10 * 2, rel=1e-6)  # 2万股
    # 市值连续性：除权日收盘市值 ≈ 除权前一日（价格减半、股数翻倍，无真实涨跌）
    assert res.nav[2][1] == pytest.approx(res.nav[1][1], rel=0.01)


def test_unknown_symbol_target_ignored():
    """目标里含无行情标的：被忽略，不产生委托也不炸。"""

    class Bad(Strategy):
        def on_bar(self, ctx, bars):
            return [("NOPE.SH", 0.5), ("600000.SH", 0.5)]

    res = Engine(Bad(), ruleset=_zf_ruleset(),
                 config=EngineConfig(initial_cash=100_000, slippage="none",
                                     cash_buffer=0.0, min_order_value=100)).run(_bars_df())
    assert all(t.symbol == "600000.SH" for t in res.trades)


def test_buy_rejected_on_gap_up_cash_shortfall():
    """跳高开：T 收盘定价的买单金额超出 T+1 可用现金 → 整单作废
    （与 backtrader / 真实券商「资金不足废单」语义一致），现金绝不变负。"""
    from datetime import date, timedelta

    import polars as pl
    d0 = date(2026, 1, 5)
    rows = [
        # day1: close 10 → 信号全仓（按 10 元估 100,000 股）
        {"trade_date": d0, "symbol": "600000.SH", "open": 10.0, "high": 10.1,
         "low": 9.9, "close": 10.0, "pre_close": 10.0, "volume": 2e9, "amount": 2e9 * 10},
        # day2: 跳空高开 9% → 实际成本约 1,090,000 > 现金 1,000,000
        # （开盘不能正好是涨停价 11.0，否则先触发涨跌停拒单）
        {"trade_date": d0 + timedelta(days=1), "symbol": "600000.SH", "open": 10.9,
         "high": 10.95, "low": 10.8, "close": 10.9, "pre_close": 10.0,
         "volume": 2e9, "amount": 2e9 * 10.9},
    ]
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    eng = Engine(_Always(), ruleset=_zf_ruleset(),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     cash_buffer=0.0, min_order_value=100))
    res = eng.run(df)
    assert res.trades == [], "资金不足必须整单作废"
    assert any("资金不足" in r[2] for r in res.rejected)
    assert eng.account.cash == 1_000_000, "现金不能变负"
    # day2 若价格回落，后续信号正常成交
    rows.append({"trade_date": d0 + timedelta(days=2), "symbol": "600000.SH",
                 "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                 "pre_close": 10.9, "volume": 2e9, "amount": 2e9 * 10})
    df2 = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    res2 = Engine(_Always(), ruleset=_zf_ruleset(),
                  config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                      cash_buffer=0.0, min_order_value=100)).run(df2)
    assert any(t.side.value == "buy" for t in res2.trades)
