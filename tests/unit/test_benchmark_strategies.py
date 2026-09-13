"""内置基准策略测试：确定性合成数据上手算信号与净值。

覆盖：
- 双均线金叉/死叉的目标权重序列；
- 动量轮动的满仓切换与「动量全负 → 显式清仓」；
- 海龟唐奇安突破 + ATR 仓位（手算 ATR 与目标权重）；
- 网格的线性仓位表；
- 引擎级集成：zero_out 显式清仓语义（w<=0 真正卖出）；
- 注册表 / make_benchmark 元数据一致性。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.backtest.benchmarks import (BENCHMARK_META, GridTradingStrategy,
                                        MomentumRotationStrategy, SmaCrossStrategy,
                                        TurtleDonchianStrategy, make_benchmark)
from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.rules.model import RuleSet
from lquant.backtest.strategy import STRATEGIES
from lquant.backtest.strategy.base import Context, Strategy


def _ruleset() -> RuleSet:
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


def _df(closes: dict[str, list[float]], d0=date(2026, 1, 5)) -> pl.DataFrame:
    """等权生成日线：open=close=前收（无跳空），成交量巨大绕开约束。"""
    rows = []
    for sym, cs in closes.items():
        pre = cs[0]
        for i, c in enumerate(cs):
            rows.append({"trade_date": d0 + timedelta(days=i), "symbol": sym,
                         "open": pre, "high": c * 1.001, "low": c * 0.999,
                         "close": c, "pre_close": pre,
                         "volume": 2e9, "amount": 2e9 * c})
            pre = c
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


# ---------- 双均线 ----------

class TestSmaCross:
    def test_signal_sequence_hand_computed(self):
        """5/20 均线：前 20 天无信号；构造 V 型行情检查金叉/死叉目标。"""
        s = SmaCrossStrategy(symbol="600000.SH", fast=2, slow=4)  # 小窗口便于手算
        d0 = date(2026, 1, 5)
        # close: 10,10,10,10, 9,8（下行→死叉目标0），… 12（上行→金叉目标1）
        closes = [10, 10, 10, 10, 9, 8, 12, 13]
        for i, c in enumerate(closes):
            bar = {"600000.SH": type("B", (), {"close": c, "high": c, "low": c})()}
            got = s.on_bar(None, bar) if i else s.on_bar(None, bar)
            if len(s._hist["600000.SH"]["close"]) < 4:
                assert got == []
                continue
            h = s._hist["600000.SH"]["close"]
            fast = sum(h[-2:]) / 2
            slow = sum(h[-4:]) / 4
            expect = 1.0 if fast > slow else 0.0
            assert got == [("600000.SH", expect)], f"day{i}: fast={fast} slow={slow}"

    def test_engine_buy_and_liquidate(self):
        """引擎级：金叉买入后死叉显式清仓（w<=0 必须真的卖出）。"""
        closes = [10, 10, 10, 10, 9, 8, 7, 12, 13, 14, 6, 5]
        df = _df({"600000.SH": closes})
        eng = Engine(SmaCrossStrategy(symbol="600000.SH", fast=2, slow=4),
                     ruleset=_ruleset(),
                     config=EngineConfig(initial_cash=100_000, slippage="none",
                                         cash_buffer=0.0, min_order_value=100))
        res = eng.run(df)
        buys = [t for t in res.trades if t.side.value == "buy"]
        sells = [t for t in res.trades if t.side.value == "sell"]
        assert buys and sells, f"必须有买有卖: {res.trades}"
        # 死叉区间（close 7~8 附近）应清仓，卖在买入价之下
        assert sells[0].price < buys[0].price
        # 现金守恒（零费率）
        assert eng.account.cash == pytest.approx(
            100_000 - sum(t.qty * t.price for t in buys)
            + sum(t.qty * t.price for t in sells), abs=1e-6)


# ---------- 动量轮动 ----------

class TestMomentumRotation:
    def test_switches_to_strongest_and_flattens_on_all_negative(self):
        """A 涨 B 跌 → 满仓 A；随后 A 也转负 → 显式清仓（目标里出现 0 权重）。"""
        s = MomentumRotationStrategy(symbols=["A", "B"], window=3)
        closes_a = [10, 11, 12, 13, 14, 8]
        closes_b = [20, 20, 19, 18, 17, 16]
        d0 = date(2026, 1, 5)
        for i in range(len(closes_a)):
            bar = {"A": type("B", (), {"close": closes_a[i], "high": closes_a[i], "low": closes_a[i]})(),
                   "B": type("B", (), {"close": closes_b[i], "high": closes_b[i], "low": closes_b[i]})()}
            got = s.on_bar(None, bar)
            if len(s._hist["A"]["close"]) <= 3:
                assert got == []
                continue
            sa = closes_a[i] / closes_a[i - 4] - 1
            sb = closes_b[i] / closes_b[i - 4] - 1
            if sa > 0 and sa > sb:
                assert got == [("A", 1.0)]
            elif sb > 0:
                assert got == [("B", 1.0)]
            else:
                # 全负 → 清仓目标（w<=0 显式语义）
                assert all(w == 0.0 for _, w in got) and got

    def test_engine_flatten_semantics(self):
        """回归：[(s, 0.0)] 必须清仓，[] 必须不动 —— 择时策略的生命线。"""

        class FlushThenHold(Strategy):
            def __init__(self):
                super().__init__()
                self.day = 0

            def on_bar(self, ctx: Context, bars):
                self.day += 1
                if self.day <= 3:
                    return [("600000.SH", 1.0)]
                if self.day == 4:
                    return [("600000.SH", 0.0)]        # 显式清仓
                return []                          # 无操作

        df = _df({"600000.SH": [10, 10, 10, 10, 10, 10]})
        eng = Engine(FlushThenHold(), ruleset=_ruleset(),
                     config=EngineConfig(initial_cash=100_000, slippage="none",
                                         cash_buffer=0.0, min_order_value=100))
        res = eng.run(df)
        assert any(t.side.value == "buy" for t in res.trades)
        assert any(t.side.value == "sell" and t.qty > 0 for t in res.trades)
        assert res.positions[list(res.positions)[-1]] == {}   # 清仓后不再持有


# ---------- 海龟 ----------

class TestTurtleDonchian:
    def test_atr_sizing_hand_computed(self):
        """close 10,10,10,11,12,13（hi/lo = c±0.5）：
        第 4 根突破（11 > 前3日最高 10.5），前3日 TR = 1,1,1.5（跳空贡献）
        → ATR = 1.16667，weight = 0.01*11/1.16667；
        第 6 根（close 13）仍高于唐奇安上轨但持仓状态未变 → 不再下发信号（[]）。
        """
        s = TurtleDonchianStrategy(symbol="600000.SH", entry=3, exit=2,
                                   atr_window=3, daily_risk=0.01)
        closes = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0]
        got_by_day = []
        for c in closes:
            bar = {"600000.SH": type("B", (), {"close": c, "high": c + 0.5, "low": c - 0.5})()}
            got_by_day.append(s.on_bar(None, bar))
        assert got_by_day[:3] == [[], [], []]           # 窗口未成形
        assert got_by_day[3] == [("600000.SH", pytest.approx(0.11 / (3.5 / 3)))]
        assert got_by_day[4] == []                       # 持仓中，状态未翻转
        assert got_by_day[5] == []                       # 同上

    def test_exit_on_donchian_low(self):
        s = TurtleDonchianStrategy(symbol="600000.SH", entry=3, exit=2,
                                   atr_window=3, daily_risk=0.01)
        closes = [10.0, 10.0, 10.0, 15.0, 16.0, 8.0]
        last = None
        for i, c in enumerate(closes):
            hi, lo = c + 0.5, c - 0.5
            bar = {"600000.SH": type("B", (), {"close": c, "high": hi, "low": lo})()}
            last = s.on_bar(None, bar)
        assert last == [("600000.SH", 0.0)]        # 跌破 10 日低点 → 清仓


# ---------- 网格 ----------

class TestGridTrading:
    def test_linear_position_schedule(self):
        s = GridTradingStrategy(symbol="600000.SH", grid_pct=0.05, levels=4)
        first = s.on_bar(None, {"600000.SH": type("B", (), {"close": 100.0, "high": 100.0, "low": 100.0})()})
        assert first == [("600000.SH", 0.5)]       # 锚定日半仓
        # 涨 10% = 2 格 → frac = 0.5 - 0.5*2/4 = 0.25
        up = s.on_bar(None, {"600000.SH": type("B", (), {"close": 110.0, "high": 110.0, "low": 110.0})()})
        assert up == [("600000.SH", pytest.approx(0.25))]
        # 跌 10% = -2 格 → frac = 0.75
        down = s.on_bar(None, {"600000.SH": type("B", (), {"close": 90.0, "high": 90.0, "low": 90.0})()})
        assert down == [("600000.SH", pytest.approx(0.75))]
        # 极端上涨截断到 0
        cap = s.on_bar(None, {"600000.SH": type("B", (), {"close": 200.0, "high": 200.0, "low": 200.0})()})
        assert cap == [("600000.SH", 0.0)]


# ---------- 注册表与元数据 ----------

class TestRegistry:
    @pytest.mark.parametrize("key", sorted(BENCHMARK_META))
    def test_all_registered_and_instantiable(self, key):
        assert STRATEGIES.get(f"benchmark_{key}") is not None
        st = make_benchmark(key)
        assert st is not None
        assert BENCHMARK_META[key]["symbols"], "每个基准必须有默认标的"

    def test_unknown_key_raises(self):
        with pytest.raises(KeyError):
            make_benchmark("nope")
