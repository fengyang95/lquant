"""深度 review 修复项的回归锁定（数据完备性 × 计算准确性）。

每条测试对应一次已修复的缺陷；注释给出根因摘要，回归失败时先读这里。
分组：北交所代码解析 / 移动止盈峰值复权方向 / DSL 负窗口门禁 /
TS 算子窗口校验 / no-trade band 杠杆吸收 / Rust 参考实现奇偶性 /
CS 算子 NaN 语义 / financial checkpoint 路由 / JQ open 桶防未来。
"""
from __future__ import annotations

import math
from datetime import date

import polars as pl
import pytest

from lquant._rust.broker_ref import match_order
from lquant._rust.metrics_ref import rank_ic
from lquant._rust.ops_ref import ts_regbeta
from lquant.backtest.events import Bar
from lquant.backtest.exit.base import ExitContext, ExitStrategy, PositionView
from lquant.core.errors import FactorError, LookaheadError
from lquant.core.types import parse_symbol
from lquant.factors.dsl.analyzer import check as dsl_check
from lquant.factors.dsl.parser import parse as dsl_parse
from lquant.factors.ops import ts_ops  # noqa: F401  触发算子注册
from lquant.factors.ops.cs_ops import (
    demean as cs_demean,
)
from lquant.factors.ops.cs_ops import (
    rank as cs_rank,
)
from lquant.factors.ops.cs_ops import (
    scale as cs_scale,
)
from lquant.factors.ops.cs_ops import (
    zscore as cs_zscore,
)
from lquant.portfolio.weighting import apply_no_trade_band

# ------------------------------------------------------- 北交所代码解析


@pytest.mark.parametrize("raw,ex", [
    ("430047", "BJ"), ("830799", "BJ"), ("873223", "BJ"),
    ("920001", "BJ"), ("882001", "BJ"),
    ("600000", "SH"), ("000001", "SZ"), ("300750", "SZ"),
])
def test_parse_symbol_bj_codes(raw: str, ex: str) -> None:
    """北交所 6 位代码此前被 5 位正则漏掉（4/8 开头仅匹配 5 位）。"""
    assert parse_symbol(raw).exchange == ex


# ----------------------------------------------- 移动止盈峰值：除权方向


class _PeakOnly(ExitStrategy):
    name = "peak_only"

    def on_bar(self, ctx: ExitContext) -> list:
        self.track_peak(ctx)
        return []


def _bar(sym: str, d: date, high: float, f: float) -> Bar:
    return Bar(symbol=sym, trade_date=d, open=high, high=high, low=high,
               close=high, pre_close=high, volume=1e6, amount=high * 1e6,
               adj_factor=f)


def _step(s: _PeakOnly, sym: str, d: date, high: float, f: float,
          avg_cost: float = 10.0) -> None:
    pos = PositionView(sym, 1000, 1000, avg_cost)
    s.track_peak(ExitContext(trade_date=d, phase="close", positions=[pos],
                             bars={sym: _bar(sym, d, high, f)}))


def test_track_peak_ex_dividend_direction() -> None:
    """除权日峰值必须 /= ratio（与账户层 avg_cost 同向）。

    10 送 5（ratio=1.5）：旧峰值 10 元 ÷1.5 = 6.67，与新除权价持平 ——
    除权本身不产生盈亏。此前误写 prev * ratio，峰值被抬到 15 而成本缩到
    6.67，假浮盈把止盈线抬到现价之上，每次除权后移动止盈立即误触发。
    """
    s = _PeakOnly()
    sym = "600000.SH"
    _step(s, sym, date(2026, 1, 5), 10.0, 1.0)
    assert s._peak[sym] == pytest.approx(10.0)
    # 除权日：因子 1.0 → 1.5，原始价 10 → 10/1.5
    _step(s, sym, date(2026, 1, 6), 10.0 / 1.5, 1.5)
    assert s._peak[sym] == pytest.approx(10.0 / 1.5)
    # 反弹创新高：峰值正常上移
    _step(s, sym, date(2026, 1, 7), 7.5, 1.5)
    assert s._peak[sym] == pytest.approx(7.5)


def test_track_peak_share_consolidation() -> None:
    """缩股（ratio<1）：每股价值按 1/ratio 放大，峰值同步放大。"""
    s = _PeakOnly()
    sym = "000001.SZ"
    _step(s, sym, date(2026, 2, 2), 10.0, 1.0)
    _step(s, sym, date(2026, 2, 3), 20.0, 0.5)  # 2 并 1：f 1.0 → 0.5
    assert s._peak[sym] == pytest.approx(20.0)


def test_track_peak_drop_and_reset() -> None:
    s = _PeakOnly()
    sym = "600000.SH"
    _step(s, sym, date(2026, 1, 5), 10.0, 1.0)
    # 清仓：峰值与因子缓存一并丢弃
    s.track_peak(ExitContext(trade_date=date(2026, 1, 6), phase="close",
                             positions=[], bars={}))
    assert sym not in s._peak and sym not in s._prev_adj
    s.reset()
    assert not s._peak and not s._prev_adj


# ------------------------------------------------ DSL 负窗口 / 窗口校验


def test_dsl_negative_window_rejected() -> None:
    """Ts_Delay($close, -5) 经 UnaryOp 解析，曾绕过静态检查直通 shift(-5)。"""
    for expr in ("Ts_Delay($close, -5)", "Ts_Return($close, -1)",
                 "Ts_Delta($close, -3)"):
        with pytest.raises(LookaheadError):
            dsl_check(dsl_parse(expr))


def test_dsl_fractional_ts_param_allowed() -> None:
    """正小数是合法数学参数（Ts_Quantile 的 q=0.8），不得误杀。"""
    dsl_check(dsl_parse("Ts_Quantile($close, 20, 0.8)"))


@pytest.mark.parametrize("n", [0, -1, -5])
def test_ts_ops_require_positive(n: int) -> None:
    """算子构造期校验：负/零窗口 raise，与 DSL 门禁构成双保险。"""
    with pytest.raises(FactorError):
        ts_ops.ts_delay(pl.col("close"), n)
    with pytest.raises(FactorError):
        ts_ops.ts_return(pl.col("close"), n)
    with pytest.raises(FactorError):
        ts_ops.ts_delta(pl.col("close"), n)


# ------------------------------------------------ no-trade band 双向吸收


def test_band_absorbs_without_hidden_leverage() -> None:
    """带内锚不动、带外缩放：sum>1 时不得产出隐性杠杆。

    prev A=0.6/B=0.4，new A=0.55（带内 → 保持 0.6）、B=0.55（带外）。
    原始 sum=1.15；缩放后 B = 0.55 - 0.15 = 0.40，sum 恰为 1。
    """
    out, n_changed = apply_no_trade_band(
        {"A": 0.55, "B": 0.55}, {"A": 0.6, "B": 0.4}, band=0.1)
    assert out["A"] == pytest.approx(0.6)          # 带内锚不被缩放
    assert out["B"] == pytest.approx(0.40)         # 只缩带外
    assert sum(out.values()) <= 1.0 + 1e-9
    assert n_changed == 1


def test_band_cash_buffer_below_one_is_kept() -> None:
    """sum<1 是现金缓冲（特性不是 bug），保持原样。"""
    out, _ = apply_no_trade_band({"A": 0.5}, {"A": 0.52}, band=0.05)
    assert out["A"] == pytest.approx(0.52)
    assert sum(out.values()) < 1.0


def test_band_requires_positive() -> None:
    with pytest.raises(ValueError):
        apply_no_trade_band({"A": 1.0}, {"A": 1.0}, band=0.0)


# --------------------------------------------- Rust 参考实现奇偶性


def test_rank_ic_pairwise_nan_filter() -> None:
    """非有限值成对剔除（此前 NaN 会顶格排序/毒化相关系数）。"""
    f = [1.0, 2.0, 3.0, 4.0, float("nan"), float("inf")]
    r = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert rank_ic(f, r) == pytest.approx(1.0)     # 剩 4 对完全单调
    assert math.isnan(rank_ic([1.0, float("nan"), 3.0], [0.1, 0.2, 0.3]))


def test_ts_regbeta_empty_window_and_guard() -> None:
    """窗口全 None（长期停牌常态输入）：先判 cnt 再除，不炸 ZeroDivision。"""
    out = ts_regbeta([None] * 6, [None] * 6, 3)
    assert out == [None] * 6
    with pytest.raises(ValueError):
        ts_regbeta([1.0], [1.0], 0)
    # 完美线性：beta = 1
    out2 = ts_regbeta([1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert out2[:2] == [None, None]
    assert out2[2] == pytest.approx(1.0)


@pytest.mark.parametrize("dirty", [
    {"price": float("nan")}, {"price": 0.0}, {"price": -1.0},
    {"qty": float("inf")}, {"qty": -100.0},
    {"lot_size": 0.0}, {"lot_size": float("nan")},
])
def test_match_order_dirty_inputs_rejected(dirty: dict) -> None:
    """脏输入守卫与 Rust lib.rs 逐项同构：price/qty/lot_size 违规 → (0, 0)。"""
    base = dict(symbol="600000.SH", is_buy=True, qty=100.0, price=10.0,
                commission_rate=0.0, commission_min=0.0,
                transfer_fee_rate=0.0, tax_rate=0.0, lot_size=100.0)
    base.update(dirty)
    assert match_order(**base) == (0.0, 0.0)


# ------------------------------------------------------- CS 算子 NaN 语义


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "trade_date": ["2026-01-05"] * 4,
        "symbol": ["A", "B", "C", "D"],
        "x": [1.0, 2.0, 3.0, float("nan")],
    })


def test_cs_rank_nan_not_top_ranked() -> None:
    """polars rank 把 NaN 顶格排在有限值之上：无效因子 ≠ 因子最强。"""
    out = _panel().select(rk=cs_rank(pl.col("x")))
    vals = [float(v) if v is not None else None for v in out["rk"].to_list()]
    assert vals[:3] == pytest.approx([1.0, 2.0, 3.0])
    assert vals[3] is None


def test_cs_zscore_nan_not_poisoning_and_zero_var_floor() -> None:
    out = _panel().select(z=cs_zscore(pl.col("x")))
    vals = out["z"].to_list()
    assert vals[0] == pytest.approx(-1.0)
    assert vals[1] == pytest.approx(0.0, abs=1e-9)
    assert vals[2] == pytest.approx(1.0)
    assert vals[3] is None
    # 零方差截面：分母兜底 1.0 → 全 0 而非 NaN（样本蒸发）
    flat = pl.DataFrame({
        "trade_date": ["d"] * 3, "symbol": ["A", "B", "C"],
        "x": [2.0, 2.0, 2.0],
    })
    z = flat.select(z=cs_zscore(pl.col("x")))["z"].to_list()
    assert z == pytest.approx([0.0, 0.0, 0.0])


def test_cs_demean_scale_nan_semantics() -> None:
    dm = _panel().select(v=cs_demean(pl.col("x")))["v"].to_list()
    assert dm[:3] == pytest.approx([-1.0, 0.0, 1.0])
    assert dm[3] is None
    sc = _panel().select(v=cs_scale(pl.col("x")))["v"].to_list()
    assert sc[:3] == pytest.approx([1.0 / 6.0, 2.0 / 6.0, 3.0 / 6.0])
    assert sc[3] is None


# --------------------------------------- financial checkpoint 名路由


def test_post_sync_financial_uses_actual_checkpoint(monkeypatch) -> None:
    """checkpoint 键含源名：回落 baostock 时检查必须用 financial_pit_baostock。

    此前硬编码 tushare → 检查恒 no_checkpoint → 每轮作业误判 partial。
    """
    from lquant.sync import manager

    seen: dict = {}

    def fake_check(name: str, day: object, max_lag: int = 2) -> dict:
        seen["name"] = name
        return {"ok": True}

    monkeypatch.setattr(manager, "_checkpoint_lag_check", fake_check)
    manager._post_sync_check(
        "financial", "ok",
        {"checkpoint": "financial_pit_baostock", "done": 1}, {})
    assert seen["name"] == "financial_pit_baostock"
    # 异常路径（detail 无 checkpoint）回退旧名，检查降级不误报
    manager._post_sync_check("financial", "ok", {}, {})
    assert seen["name"] == "financial_pit_tushare"


def test_backfill_financial_returns_checkpoint_name(monkeypatch) -> None:
    """返回值必须带出实际 checkpoint 名，供后置检查按源路由。"""
    import lquant.data.ingest.financial as fin

    class FakeCP:
        def __init__(self, name: str) -> None:
            self.name = name

        def covers(self, *a: object) -> bool:
            return True

        def covered_until(self, *a: object) -> None:
            return None

        def record_coverage(self, *a: object) -> None:
            return None

        def unmark(self, *a: object) -> None:
            return None

    class FakeRepo:
        def upsert(self, df: object) -> None:
            return None

        def count(self) -> int:
            return 0

    class FakeProvider:
        name = "dummy"

        def financial_pit(self, *a: object) -> pl.DataFrame:
            return pl.DataFrame()

    monkeypatch.setattr(fin, "Checkpoint", FakeCP)
    monkeypatch.setattr(fin, "FinancialRepo", FakeRepo)
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: FakeProvider())

    res = fin.backfill_financial(symbols=["600000.SH"],
                                 start="2026-01-01", end="2026-01-05")
    assert res["checkpoint"] == "financial_pit_dummy"
    assert res["done"] == 0 and res["skipped_covered"] == 1


# --------------------------------------------- JQ open 桶防未来


def _jq_df(days: int = 6) -> pl.DataFrame:
    rows = []
    px = {"600000.SH": 100.0, "000001.SZ": 50.0}
    start = date(2026, 1, 5)
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        for s, base in px.items():
            p = base * (1.02 ** i) if s.startswith("6") else base * (0.99 ** i)
            pre = p if i == 0 else (
                base * (1.02 ** (i - 1)) if s.startswith("6")
                else base * (0.99 ** (i - 1)))
            rows.append(dict(trade_date=d, symbol=s, open=p, high=p * 1.005,
                             low=p * 0.995, close=p, pre_close=pre,
                             volume=1e8, amount=p * 1e8))
    return pl.DataFrame(rows)


_PROBE = """
_seen = []

def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(_probe, time="{bucket}")

def _probe(context):
    vals = get_factor_values("$close", ["600000.SH"], count=100)
    _seen.append(len(vals["600000.SH"]))
"""


def test_jq_get_factor_values_open_bucket_excludes_today() -> None:
    """open 桶决策时当日收盘尚未发生：因子面板必须截到前一交易日。

    因子面板第 i 行用当日收盘价算出；open 桶若把第 i 行给策略，
    等于开盘决策用当日收盘 —— 未来函数。
    """
    from lquant.backtest.jqapi import JQRunner

    runner = JQRunner(_PROBE.format(bucket="open"), initial_cash=1_000_000,
                      factor_formulas=["$close"])
    res = runner.run(_jq_df())
    assert res.error is None
    assert runner.ns["_seen"] == [0, 1, 2, 3, 4, 5]


def test_jq_get_factor_values_close_bucket_includes_today() -> None:
    from lquant.backtest.jqapi import JQRunner

    runner = JQRunner(_PROBE.format(bucket="close"), initial_cash=1_000_000,
                      factor_formulas=["$close"])
    res = runner.run(_jq_df())
    assert res.error is None
    assert runner.ns["_seen"] == [1, 2, 3, 4, 5, 6]
