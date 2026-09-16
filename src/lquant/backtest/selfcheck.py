"""引擎自检套件 —— 手算金标准 + 性质测试，供 API /validation 与单测共用。

与 tests/unit/test_backtest_accuracy.py 同源（L1~L4 方法论）：
- 零费率规则集 + 手写确定性价格，全部数字可笔算，误差 < 1e-6；
- 真实费率下现金守恒、防未来函数截断不变性、涨跌停拒单、T+N 约束；
- 印花税生效区间边界、最低佣金按订单累计（撮合层）；
- 指标用独立公式重算对照。

每个检查独立 try/except，任何单项异常不影响其余项 ——
自检报告的目的是「告诉用户哪里坏了」，不是中途炸掉。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.backtest.broker import Broker
from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.metrics import max_drawdown
from lquant.backtest.rules.model import (
    Commission,
    InstrumentRules,
    PriceLimit,
    RuleSet,
    TaxSchedule,
)
from lquant.backtest.strategy.base import Context, Strategy
from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["run_selfcheck"]

STAMP_CUT = date(2023, 8, 28)     # 印花税千一 → 万五 的生效日


def _zero_fee_ruleset() -> RuleSet:
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


def _golden_df() -> pl.DataFrame:
    """4 天 2 标的手写价格（与 test_backtest_accuracy 同一份数据）。"""
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


class _BuyA(Strategy):
    def on_bar(self, ctx: Context, bars) -> list[tuple[str, float]]:
        return [("600000.SH", 1.0)]


def _cfg(**kw) -> EngineConfig:
    d = dict(initial_cash=1_000_000, slippage="none", cash_buffer=0.0,
             min_order_value=50_000, participation=0.5)
    d.update(kw)
    return EngineConfig(**d)


def run_selfcheck() -> list[dict]:
    checks: list[dict] = []

    def add(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as e:                 # noqa: BLE001
            log.exception(f"自检项 {name} 异常")
            ok, detail = False, f"异常: {type(e).__name__}: {e}"
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    # L1 零费率金标准净值（手算）
    def _golden() -> tuple[bool, str]:
        res = Engine(_BuyA(), ruleset=_zero_fee_ruleset(), config=_cfg()).run(_golden_df())
        expect = [(date(2026, 1, 5), 1_000_000.0), (date(2026, 1, 6), 1_110_000.0),
                  (date(2026, 1, 7), 1_160_000.0), (date(2026, 1, 8), 1_090_000.0)]
        ok = len(res.nav) == 4 and all(
            d == ed and abs(v - ev) < 1e-6 for (d, v), (ed, ev) in zip(res.nav, expect, strict=False))
        return ok, f"净值序列 {[(str(d), round(v, 2)) for d, v in res.nav]}"

    add("金标准净值（手算逐日对账，零费率）", _golden)

    # 真实费率现金守恒
    def _cash_identity() -> tuple[bool, str]:
        base = dict(_zero_fee_ruleset().default)
        base["commission"] = {"rate": 0.0003, "min": 5.0, "per_order": True}
        base["tax"] = {"rate": 0.0005}
        base["transfer_fee"] = {"rate": 0.00001}
        rs = RuleSet(market="CN", currency="CNY", default=base, etf=dict(base), exceptions={})
        cfg = _cfg(slippage="pct", slippage_params={"rate": 0.001})
        eng = Engine(_BuyA(), ruleset=rs, config=cfg)
        res = eng.run(_golden_df())
        buys = sum(t.qty * t.price for t in res.trades if t.side.value == "buy")
        sells = sum(t.qty * t.price for t in res.trades if t.side.value == "sell")
        fees = sum(t.fee for t in res.trades)
        expect = 1_000_000 - buys + sells - fees
        return abs(eng.account.cash - expect) < 1e-4 and fees > 0, \
            f"现金 {eng.account.cash:.4f} = 1,000,000 − 买入 {buys:.2f} + 卖出 {sells:.2f} − 费用 {fees:.2f}"

    add("现金守恒恒等式（真实费率 + 滑点）", _cash_identity)

    # 防未来函数：截断不变性
    def _no_lookahead() -> tuple[bool, str]:
        df = _golden_df()
        full = Engine(_BuyA(), ruleset=_zero_fee_ruleset(), config=_cfg()).run(df)
        part = Engine(_BuyA(), ruleset=_zero_fee_ruleset(), config=_cfg()).run(df.head(6))
        full_map = dict(full.nav)
        ok = all(abs(full_map[d] - v) < 1e-9 for d, v in part.nav)
        return ok, "前 3 天净值在全量与截断数据下逐日一致"

    add("防未来函数（截断不变性）", _no_lookahead)

    # 涨跌停拒单
    def _limit_up() -> tuple[bool, str]:
        df = _golden_df().with_columns(
            pl.when((pl.col("symbol") == "600000.SH")
                    & (pl.col("trade_date") == date(2026, 1, 6)))
            .then(11.0).otherwise(pl.col("open")).alias("open"))
        res = Engine(_BuyA(), ruleset=_zero_fee_ruleset(), config=_cfg()).run(df)
        ok = any("涨停" in r[2] for r in res.rejected)
        ok = ok and not any(t.side.value == "buy" and t.trade_date == date(2026, 1, 6)
                            for t in res.trades)
        return ok, f"开盘封涨停当日买入被拒：{res.rejected}"

    add("涨跌停拒单（开盘封板不可买）", _limit_up)

    # T+N 卖出约束
    def _t_plus_n() -> tuple[bool, str]:
        class Flip(Strategy):
            def on_bar(self, ctx: Context, bars):
                return [("600000.SH", 1.0)] if ctx.trade_date <= date(2026, 1, 6) \
                    else [("000001.SZ", 1.0)]
        res = Engine(Flip(), ruleset=_zero_fee_ruleset(), config=_cfg(),
                     meta={"600000.SH": {"sellable_after_days": 10}}).run(_golden_df())
        sells = [t for t in res.trades if t.side.value == "sell" and t.symbol == "600000.SH"]
        return sells == [], f"T+10 内卖出信号全部被拦（{len(sells)} 笔成交）"

    add("T+N 可卖约束（买入未满 N 日不可卖）", _t_plus_n)

    # 印花税生效区间边界
    def _tax_schedule() -> tuple[bool, str]:
        r = InstrumentRules(
            symbol=__import__("lquant.core.types", fromlist=["Symbol"]).Symbol("600000", "SH"),
            sec_type=__import__("lquant.core.types", fromlist=["SecType"]).SecType.STOCK,
            commission=Commission(rate=0.0, min=0.0, per_order=True),
            tax=TaxSchedule([(date(2000, 1, 1), STAMP_CUT - timedelta(days=1), 0.001),
                             (STAMP_CUT, date(9999, 12, 31), 0.0005)]),
            transfer_fee_rate=0.0,
            price_limit=PriceLimit("by_board", {"main": 0.10}),
            lot_size=100, sellable_after_days=1)
        b = Broker({"600000.SH": r})
        fee = {}
        for label, d in (("降税前", date(2023, 8, 25)), ("降税当日", STAMP_CUT)):
            o = Order("t1", "600000.SH", Side.SELL, 100)
            bar = Bar("600000.SH", d, 10.0, 10.1, 9.9, 10.0, 10.0, 1e6, 1e7)
            f = b.match(o, bar, d)
            fee[label] = f.fee if f else None
        return (fee["降税前"] is not None and fee["降税当日"] is not None
                and abs(fee["降税前"] - 1000 * 0.001) < 1e-9
                and abs(fee["降税当日"] - 1000 * 0.0005) < 1e-9), \
            f"卖出 1000 元：降税前印花税 {fee['降税前']:.4f}（千一），降税当日 {fee['降税当日']:.4f}（万五）"

    add("印花税生效区间（2023-08-28 千一→万五）", _tax_schedule)

    # 最低佣金按订单累计（部分成交不重复收）
    def _min_comm() -> tuple[bool, str]:
        r = InstrumentRules(
            symbol=__import__("lquant.core.types", fromlist=["Symbol"]).Symbol("600000", "SH"),
            sec_type=__import__("lquant.core.types", fromlist=["SecType"]).SecType.STOCK,
            commission=Commission(rate=0.00025, min=5.0, per_order=True),
            tax=TaxSchedule([(date(2000, 1, 1), date(9999, 12, 31), 0.0)]),
            transfer_fee_rate=0.0,
            price_limit=PriceLimit("by_board", {"main": 0.10}),
            lot_size=100, sellable_after_days=1)
        b = Broker({"600000.SH": r})
        o = Order("m1", "600000.SH", Side.BUY, 400)
        bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
        f1 = b.match(o, bar, date(2026, 1, 5), max_qty=200)
        f2 = b.match(o, bar, date(2026, 1, 5), max_qty=200)
        total = (f1.fee + f2.fee) if f1 and f2 else float("nan")
        return abs(total - 5.0) < 1e-6, f"两次部分成交合计佣金 {total:.4f} 元（最低 5 元只收一次）"

    add("最低佣金按订单累计（部分成交不重复收）", _min_comm)

    # 指标独立公式重算
    def _metrics() -> tuple[bool, str]:
        nav = [100.0, 110.0, 105.0, 120.0, 90.0, 100.0]
        from lquant.backtest.metrics import perf_from_nav
        perf = perf_from_nav(nav)
        rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
        total = nav[-1] / nav[0] - 1
        ann = (1 + total) ** (252 / (len(nav) - 1)) - 1
        mdd, _, _ = max_drawdown(nav)
        mdd_ref = min(nav[i] / max(nav[:i + 1]) - 1 for i in range(len(nav)))
        mean = sum(rets) / len(rets)
        vol = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * 252 ** 0.5
        sharpe_ref = ann / vol
        ok = (abs(perf["total_return"] - total) < 1e-12
              and abs(perf["annual_return"] - ann) < 1e-9
              and abs(mdd - mdd_ref) < 1e-12
              and abs(perf["sharpe"] - sharpe_ref) < 1e-9)
        return ok, (f"total={total:.6f} ann={ann:.6f} mdd={mdd:.6f} "
                    f"sharpe={perf['sharpe']:.6f} 全部与独立公式一致")

    add("绩效指标（几何年化/净值回撤/夏普 独立重算）", _metrics)

    return checks
