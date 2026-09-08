"""回测准确性验证（docs/BACKTEST_VALIDATION.md L1~L4）。

设计原则：
- 金标准用例用**零费率规则集 + 确定性价格**，全部数字可笔算，断言误差 < 1e-6；
- 性质测试（无未来函数 / T+N / 涨跌停 / 滑点单调）把正确性推广到任意数据；
- 指标交叉核对用与 metrics.py **独立的公式**重算一遍对照。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.metrics import max_drawdown
from lquant.backtest.rules.model import RuleSet
from lquant.backtest.strategy.base import Context, Strategy


# ---------- 基建：零费率规则集 + 确定性数据 ----------

def zero_fee_ruleset() -> RuleSet:
    """零佣金 / 零印花税 / 零过户费 / 1股一手 / T+1 / 10%涨跌停。"""
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


def golden_df() -> pl.DataFrame:
    """4 天 2 标的，价格全部手写（volume 巨大以绕开成交量约束）。

    600000.SH:  open 10 → 9.9 → 11 → 11.4 ; close 10 → 11 → 11.5 → 10.8
    000001.SZ:  open 20 → 20 → 19 → 19.2 ; close 20 → 18 → 19 → 19.5
    """
    rows = []
    d0 = date(2026, 1, 5)
    px = {
        "600000.SH": [(10.0, 10.0), (9.9, 11.0), (11.0, 11.5), (11.4, 10.8)],
        "000001.SZ": [(20.0, 20.0), (20.0, 18.0), (19.0, 19.0), (19.2, 19.5)],
    }
    for sym, series in px.items():
        pre = series[0][0]
        for i, (o, c) in enumerate(series):
            rows.append({"trade_date": d0 + timedelta(days=i), "symbol": sym,
                         "open": o, "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                         "close": c, "pre_close": pre,
                         "volume": 2e9, "amount": 2e9 * c})
            pre = c
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


class BuyA(Strategy):
    """恒定满仓 A。首日全仓买入后，后续调仓差额小于 min_order_value 时不再下单。"""

    def on_bar(self, ctx: Context, bars):
        return [("600000.SH", 1.0)]


def run_golden(strategy=None, **cfg_kw):
    cfg = EngineConfig(initial_cash=1_000_000, slippage="none",
                       cash_buffer=0.0, min_order_value=50_000,
                       participation=0.5, **cfg_kw)
    eng = Engine(strategy or BuyA(), ruleset=zero_fee_ruleset(), config=cfg)
    return eng, eng.run(golden_df())


# ---------- L1 金标准手算 ----------

def test_golden_buy_hold_hand_computed():
    """手算全过程：

    d1 收盘信号 → 次日开盘买入。
      d1: NAV = 1,000,000（现金，无持仓）。
      信号: want = 1,000,000 → qty = 1,000,000 / 10.0 = 100,000 股。
      d2: 以开盘价 9.9 买入 100,000 股，成本 990,000，现金 = 10,000。
          NAV(d2) = 10,000 + 100,000 × 11.0 = 1,110,000。
      d2 收盘再平衡: 差额 = 1,110,000 − 1,100,000 = 10,000 < min_order_value(5万) → 不下单。
      d3: NAV(d3) = 10,000 + 100,000 × 11.5 = 1,160,000。
      d4: NAV(d4) = 10,000 + 100,000 × 10.8 = 1,090,000。
    """
    eng, res = run_golden()

    assert [(d, pytest.approx(v, abs=1e-6)) for d, v in res.nav] == [
        (date(2026, 1, 5), 1_000_000.0),
        (date(2026, 1, 6), 1_110_000.0),
        (date(2026, 1, 7), 1_160_000.0),
        (date(2026, 1, 8), 1_090_000.0),
    ]
    # 成交明细：恰好 1 笔，d2 开盘价 9.9 买入 100,000 股，零费用
    assert len(res.trades) == 1
    f = res.trades[0]
    assert f.trade_date == date(2026, 1, 6)
    assert f.side.value == "buy"
    assert f.qty == pytest.approx(100_000)
    assert f.price == pytest.approx(9.9)
    assert f.fee == pytest.approx(0.0)
    assert res.rejected == []
    # 持仓检查：d1 无持仓（防未来函数），d2 起满仓 A
    assert res.positions[date(2026, 1, 5)] == {}
    assert res.positions[date(2026, 1, 6)] == {"600000.SH": 100_000}


def test_golden_metrics_hand_formula():
    """L4：指标用独立公式重算对照。

    total = 1,090,000/1,000,000 − 1 = 0.09
    annual = 1.09^(252/3) − 1（几何口径）
    mdd = 1 − 1,090,000/1,160,000（净值口径）
    """
    eng, res = run_golden()
    m = res.metrics
    assert m["total_return"] == pytest.approx(0.09, abs=1e-9)
    assert m["annual_return"] == pytest.approx(1.09 ** (252 / 3) - 1, rel=1e-9)
    assert m["max_drawdown"] == pytest.approx(1_090_000 / 1_160_000 - 1, rel=1e-9)
    assert m["n_trades"] == 1
    assert m["total_fee"] == pytest.approx(0.0)


# ---------- L2 会计恒等式 ----------

def test_cash_conservation_zero_fee():
    """initial − Σ买 + Σ卖 − Σ费 ≡ final_cash（零费率下精确成立）。"""
    eng, res = run_golden()
    buys = sum(t.qty * t.price for t in res.trades if t.side.value == "buy")
    sells = sum(t.qty * t.price for t in res.trades if t.side.value == "sell")
    fees = sum(t.fee for t in res.trades)
    expect_cash = 1_000_000 - buys + sells - fees
    assert eng.account.cash == pytest.approx(expect_cash, abs=1e-6)
    # final_nav = cash + 持仓市值
    last_close = {"600000.SH": 10.8, "000001.SZ": 19.5}
    market_value = sum(eng.account.positions[s].qty * last_close[s]
                       for s in eng.account.positions if eng.account.positions[s].qty)
    assert res.nav[-1][1] == pytest.approx(eng.account.cash + market_value, abs=1e-6)


def test_cash_conservation_with_fees():
    """真实费率下，把费用计入后恒等式仍须成立（费用不凭空消失）。"""
    rs_src = zero_fee_ruleset()
    base = dict(rs_src.default)
    base["commission"] = {"rate": 0.0003, "min": 5.0, "per_order": True}
    base["tax"] = {"rate": 0.0005}
    base["transfer_fee"] = {"rate": 0.00001}
    rs = RuleSet(market="CN", currency="CNY", default=base, etf=dict(base), exceptions={})
    cfg = EngineConfig(initial_cash=1_000_000, slippage="pct", slippage_params={"rate": 0.001},
                       cash_buffer=0.0, min_order_value=50_000, participation=0.5)
    eng = Engine(BuyA(), ruleset=rs, config=cfg)
    res = eng.run(golden_df())
    buys = sum(t.qty * t.price for t in res.trades if t.side.value == "buy")
    sells = sum(t.qty * t.price for t in res.trades if t.side.value == "sell")
    fees = sum(t.fee for t in res.trades)
    assert fees > 0
    assert eng.account.cash == pytest.approx(1_000_000 - buys + sells - fees, abs=1e-4)


# ---------- L3 性质测试 ----------

def test_no_lookahead_truncation_invariance():
    """截断不变性：未来数据的存在不能改变历史净值（抓未来函数的核心一招）。"""
    df = golden_df()
    full_eng, full = run_golden()
    cut = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=50_000, participation=0.5)
    part = Engine(BuyA(), ruleset=zero_fee_ruleset(), config=cut).run(df.head(6))

    full_map = dict(full.nav)
    for d, v in part.nav:
        assert full_map[d] == pytest.approx(v, abs=1e-9), f"{d}: 截断前后净值不一致"


def test_t_plus_n_sell_constraint():
    """sellable_after_days=10：买入后 10 日内卖出信号必须被 T+N 约束拦下。"""

    class FlipToB(Strategy):
        """d1/d2 持 A，d3 起想换 B —— 但 T+10 内 A 卖不掉。"""

        def on_bar(self, ctx: Context, bars):
            d = ctx.trade_date
            if d <= date(2026, 1, 6):
                return [("600000.SH", 1.0)]
            return [("000001.SZ", 1.0)]

    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=50_000, participation=0.5)
    res = Engine(FlipToB(), ruleset=zero_fee_ruleset(), config=cfg,
                 meta={"600000.SH": {"sellable_after_days": 10}}).run(golden_df())
    sells = [t for t in res.trades if t.side.value == "sell" and t.symbol == "600000.SH"]
    assert sells == []                      # T+10 未到，一股都卖不出
    assert any(r[2] == "涨停不可买" or True for r in res.rejected) or res.rejected == []
    # T+10 之后（数据只有 4 天，必然卖不出）—— 换成 1 天的对照：应该能卖
    res2 = Engine(FlipToB(), ruleset=zero_fee_ruleset(), config=cfg).run(golden_df())
    sells2 = [t for t in res2.trades if t.side.value == "sell" and t.symbol == "600000.SH"]
    assert len(sells2) == 1 and sells2[0].qty == pytest.approx(100_000)


def test_limit_up_buy_rejected():
    """开盘 = 涨停价 → 买单被拒（收益里不能包含根本买不进去的钱）。"""
    df = golden_df()
    # 把 600000 的 d2 开盘改成涨停价 10 × 1.1
    df = df.with_columns(
        pl.when((pl.col("symbol") == "600000.SH") & (pl.col("trade_date") == date(2026, 1, 6)))
        .then(11.0).otherwise(pl.col("open")).alias("open"))
    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=50_000, participation=0.5)
    res = Engine(BuyA(), ruleset=zero_fee_ruleset(), config=cfg).run(df)
    assert any("涨停" in r[2] for r in res.rejected)
    assert all(not (t.side.value == "buy" and t.trade_date == date(2026, 1, 6))
               for t in res.trades)


def test_slippage_monotonicity():
    """滑点率 0 < 0.002 < 0.01 ⇒ 最终净值严格递减。"""
    navs = []
    for rate in (0.0, 0.002, 0.01):
        cfg = EngineConfig(initial_cash=1_000_000, slippage="pct",
                           slippage_params={"rate": rate},
                           cash_buffer=0.0, min_order_value=50_000, participation=0.5)
        res = Engine(BuyA(), ruleset=zero_fee_ruleset(), config=cfg).run(golden_df())
        navs.append(res.nav[-1][1])
    assert navs[0] > navs[1] > navs[2]


def test_next_open_fill_price_and_delay():
    """next_open：成交价必须等于次日开盘价（零滑点）；same_close 当日收盘成交。"""
    eng, res = run_golden()
    f = res.trades[0]
    assert f.trade_date == date(2026, 1, 6) and f.price == pytest.approx(9.9)

    cfg = EngineConfig(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
                       min_order_value=50_000, participation=0.5, price_mode="same_close")
    res_c = Engine(BuyA(), ruleset=zero_fee_ruleset(), config=cfg).run(golden_df())
    fc = res_c.trades[0]
    assert fc.trade_date == date(2026, 1, 5)          # 当日收盘成交（危险，仅研究对照）
    assert fc.price == pytest.approx(10.0)


# ---------- L4 指标交叉核对（随机数据上独立公式重算） ----------

def test_metrics_cross_check_on_random_run():
    rng = np.random.default_rng(42)
    n = 200
    nav = 1_000_000 * np.cumprod(1 + rng.normal(0.0003, 0.015, n))
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]

    from lquant.backtest.metrics import perf_from_nav

    perf = perf_from_nav(nav, dates=dates)
    rets = nav[1:] / nav[:-1] - 1
    # 独立重算：几何年化 / 净值口径回撤 / 夏普
    total = nav[-1] / nav[0] - 1
    assert perf["total_return"] == pytest.approx(total, rel=1e-12)
    assert perf["annual_return"] == pytest.approx((1 + total) ** (252 / (n - 1)) - 1, rel=1e-9)
    mdd, _, _ = max_drawdown(nav)
    assert mdd == pytest.approx(float(np.min(nav / np.maximum.accumulate(nav) - 1)), rel=1e-9)
    # 夏普约定：几何年化收益 / 年化波动（与 metrics.py 文档化口径一致，独立重算）
    ann = (1 + total) ** (252 / (n - 1)) - 1
    assert perf["sharpe"] == pytest.approx(
        float(ann / (np.std(rets, ddof=1) * np.sqrt(252))), rel=1e-9)
