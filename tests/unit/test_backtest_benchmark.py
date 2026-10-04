"""回测基准链路（Phase 1.2）：指数序列读取、逐日对齐、相对指标。

要守住的三件事：
1. **对齐是逐日配对，不是尾部截断**。基准缺某天时该期两边一起丢弃；
   用 shift 硬凑会把不同日期的收益配成对，α/β 变成垃圾。
2. **基准缺失不阻断回测**。表未建/无数据/期数不足都降级成
   ``benchmark_available=False`` + 可读的 note，净值照常产出。
3. **指标口径可独立复算**。测试用 numpy 直接算 α/β/TE/IR 与引擎输出对拍，
   而不是再调一遍被测函数（那是自证）。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import duckdb
import numpy as np
import polars as pl
import pytest

from lquant.backtest.benchmark import (
    DEFAULT_BENCHMARK,
    benchmark_nav_aligned,
    benchmark_returns_by_date,
    equal_weight_benchmark,
    load_index_series,
    pair_returns_with_benchmark,
    parse_benchmark,
)
from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.strategy.base import Context, Strategy

D0 = date(2026, 1, 5)


def _days(n: int) -> list[date]:
    """n 个连续工作日（跳过周末，避免与交易日语义混淆）。"""
    out: list[date] = []
    d = D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def index_con():
    """内存 index_daily 表（真实 DuckDB，验证 SQL 而非打桩）。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE index_daily (trade_date DATE, symbol VARCHAR, close DOUBLE, "
        "pre_close DOUBLE)")
    yield con
    con.close()


def _seed(con, symbol: str, series: list[tuple[date, float]]) -> None:
    rows = []
    for i, (d, c) in enumerate(series):
        pc = series[i - 1][1] if i else c
        rows.append((d, symbol, c, pc))
    con.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?)", rows)


# ----------------------------------------------------------- 基准解析

def test_parse_benchmark_aliases_and_none():
    assert parse_benchmark("000300.SH") == "000300.SH"
    assert parse_benchmark("hs300") == "000300.SH"
    assert parse_benchmark("沪深300") == "000300.SH"
    assert parse_benchmark("000300") == "000300.SH"
    assert parse_benchmark("399006") == "399006.SZ"
    # None / 空 / off = 不挂基准
    assert parse_benchmark(None) is None
    assert parse_benchmark("") is None
    assert parse_benchmark("none") is None
    assert parse_benchmark("无") is None


def test_default_benchmark_is_hs300():
    assert DEFAULT_BENCHMARK == "000300.SH"


# ----------------------------------------------------------- 序列读取

def test_load_index_series_filters_window_and_sorts(index_con):
    ds = _days(6)
    _seed(index_con, "000300.SH", [(d, 3000.0 + i * 10) for i, d in enumerate(ds)])
    got = load_index_series("000300.SH", start=ds[1], end=ds[4], con=index_con)
    assert [d for d, _ in got] == ds[1:5]
    assert [c for _, c in got] == [3010.0, 3020.0, 3030.0, 3040.0]


def test_load_index_series_missing_table_returns_empty():
    con = duckdb.connect(":memory:")          # 无 index_daily 表
    try:
        assert load_index_series("000300.SH", con=con) == []
    finally:
        con.close()


def test_load_index_series_drops_nonpositive_close(index_con):
    ds = _days(4)
    _seed(index_con, "000300.SH", [(ds[0], 3000.0), (ds[1], 0.0),
                                   (ds[2], 3020.0), (ds[3], 3030.0)])
    got = load_index_series("000300.SH", con=index_con)
    assert [d for d, _ in got] == [ds[0], ds[2], ds[3]]


def test_benchmark_returns_by_date_basic():
    ds = _days(4)
    series = [(ds[0], 100.0), (ds[1], 110.0), (ds[2], 99.0), (ds[3], 99.0)]
    r = benchmark_returns_by_date(series)
    assert set(r) == {ds[1], ds[2], ds[3]}
    assert r[ds[1]] == pytest.approx(0.10)
    assert r[ds[2]] == pytest.approx(-0.10)
    assert r[ds[3]] == pytest.approx(0.0)


# ----------------------------------------------------------- 逐日对齐

def test_pair_returns_drops_gap_dates_on_both_sides(index_con):
    """基准缺一天时该期必须**两边一起丢弃**，不许 shift 硬凑。"""
    ds = _days(5)
    _seed(index_con, "000300.SH", [(ds[0], 100.0), (ds[1], 110.0),
                                   # ds[2] 缺失
                                   (ds[3], 121.0), (ds[4], 121.0)])
    port = [0.01, 0.02, 0.03, 0.04]        # 对应 ds[1..4]
    p, b, note = pair_returns_with_benchmark(ds, port, "000300.SH", con=index_con)
    # ds[2] 的基准收益可由 ds[1]→ds[3] 得到（缺口跨期），故只丢 ds[1] ？
    # 不：基准序列里 ds[2] 不存在 → benchmark_returns_by_date 只产出
    # ds[1]（100→110）、ds[3]（110→121）、ds[4]（121→121）。
    # 因此对齐后是 ds[1], ds[3], ds[4] 三期，丢掉 ds[2] 一期。
    assert len(p) == 3
    assert p == [0.01, 0.03, 0.04]
    assert b == pytest.approx([0.10, 0.10, 0.0])
    assert "3/4" in note
    assert "不补造" in note


def test_pair_returns_empty_when_no_index_data(index_con):
    ds = _days(3)
    p, b, note = pair_returns_with_benchmark(ds, [0.01, 0.02], "000300.SH", con=index_con)
    assert p == [] and b == []
    assert "无数据" in note


def test_pair_returns_length_matches_when_full_coverage(index_con):
    ds = _days(6)
    _seed(index_con, "000300.SH", [(d, 100.0 * (1.01 ** i)) for i, d in enumerate(ds)])
    port = [0.0, 0.01, -0.02, 0.03, 0.0]
    p, b, note = pair_returns_with_benchmark(ds, port, "000300.SH", con=index_con)
    assert len(p) == len(port) == len(b)
    assert b[0] == pytest.approx(0.01)


# ----------------------------------------------------------- 基准净值

def test_benchmark_nav_aligned_normalizes_first_day(index_con):
    ds = _days(4)
    _seed(index_con, "000300.SH", [(ds[0], 4000.0), (ds[1], 4040.0),
                                   (ds[2], 4040.0), (ds[3], 4200.0)])
    nav, label = benchmark_nav_aligned(set(ds), "000300.SH", con=index_con)
    assert label == "沪深300"
    assert nav[0]["nav"] == 1.0
    assert nav[1]["nav"] == pytest.approx(1.01)
    assert nav[3]["nav"] == pytest.approx(1.05)


def test_benchmark_nav_aligned_empty_without_data(index_con):
    ds = _days(4)
    nav, reason = benchmark_nav_aligned(set(ds), "000300.SH", con=index_con)
    assert nav == []
    assert "无数据" in reason


def test_equal_weight_benchmark_compounds_mean_returns():
    ds = _days(3)
    out = equal_weight_benchmark({ds[0]: 0.01, ds[1]: -0.02, ds[2]: None}, set(ds))
    navs = [x["nav"] for x in out]
    assert navs[0] == pytest.approx(1.01)
    assert navs[1] == pytest.approx(1.01 * 0.98)
    assert navs[2] == pytest.approx(1.01 * 0.98)     # 缺收益 = 不动


# ----------------------------------------------------------- 引擎接线

class _Half(Strategy):
    """固定两只票各半仓 —— 净值有波动，便于与基准对拍。"""

    def on_bar(self, ctx: Context, bars):
        syms = sorted(bars)[:2]
        return [(s, 1.0 / len(syms)) for s in syms]


def _panel(n_days: int = 90, seed: int = 11) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    ds = _days(n_days)
    for s in ("600000.SH", "000001.SZ"):
        px = 10.0
        for d in ds:
            pre = px
            px = max(pre * (1 + rng.normal(0.0004, 0.015)), 1.0)
            rows.append({"trade_date": d, "symbol": s, "open": pre,
                         "high": max(pre, px) * 1.01, "low": min(pre, px) * 0.99,
                         "close": px, "pre_close": pre,
                         "volume": 5e6, "amount": 5e6 * px})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def _bench_series(dates: list[date], seed: int = 3) -> list[tuple[date, float]]:
    rng = np.random.default_rng(seed)
    px = 3000.0
    out = [(dates[0], px)]
    for d in dates[1:]:
        px = px * (1 + rng.normal(0.0003, 0.01))
        out.append((d, px))
    return out


def _run(n_days: int = 90, **cfg_kw):
    df = _panel(n_days)
    dates = sorted(set(df["trade_date"].to_list()))
    cfg_kw.setdefault("benchmark_series", _bench_series(dates))
    res = Engine(_Half(), config=EngineConfig(**cfg_kw)).run(df)
    return res, dates


def test_engine_emits_excess_te_ir_for_configured_benchmark():
    res, dates = _run()
    m = res.metrics
    assert m["benchmark"] == "000300.SH"
    assert m["benchmark_available"] is True
    for k in ("excess_return", "tracking_error", "information_ratio",
              "alpha_annual", "beta"):
        assert k in m and m[k] is not None, k
    assert m["benchmark_days"] == len(res.returns)


def test_engine_benchmark_metrics_match_independent_recompute():
    """用 numpy 独立复算 α/β/TE/IR，与引擎输出对拍（不自证）。"""
    res, dates = _run()
    series = _bench_series(dates)
    bmap = benchmark_returns_by_date(series)
    rets = res.returns
    pairs = [(r, bmap[d]) for d, r in zip(dates[1:], rets, strict=False)
             if d in bmap]
    s = np.array([p[0] for p in pairs])
    b = np.array([p[1] for p in pairs])
    beta = float(np.cov(s, b, ddof=1)[0, 1] / np.var(b, ddof=1))
    alpha = float((s.mean() - beta * b.mean()) * 252)
    te = float(np.std(s - b, ddof=1) * math.sqrt(252))
    ir = float((s - b).mean() / np.std(s - b, ddof=1) * math.sqrt(252))
    total_excess = float(np.prod(1 + s) / np.prod(1 + b) - 1)

    m = res.metrics
    assert m["beta"] == pytest.approx(round(beta, 4))
    assert m["alpha_annual"] == pytest.approx(round(alpha, 6))
    assert m["tracking_error"] == pytest.approx(round(te, 6))
    assert m["information_ratio"] == pytest.approx(round(ir, 4))
    assert m["excess_return"] == pytest.approx(round(total_excess, 6))


def test_engine_benchmark_none_disables_relative_metrics():
    res, _ = _run(benchmark=None)
    m = res.metrics
    assert m["benchmark"] is None
    assert m["benchmark_available"] is False
    assert "excess_return" not in m
    assert "未配置基准" in m["benchmark_note"]
    # 绝对收益仍然完整
    assert m["annual_return"] is not None


def test_engine_benchmark_missing_data_degrades_without_raising():
    """没有基准数据时回测照常完成，只标不可用 —— 绝不让基准拖垮主产物。"""
    res, _ = _run(benchmark_series=[])
    m = res.metrics
    assert m["benchmark_available"] is False
    assert "index_daily 无数据" in m["benchmark_note"]
    assert res.nav[-1][1] > 0


def test_engine_benchmark_too_few_aligned_periods_is_not_conclusive():
    """对齐期数 < 20 时不给结论（统计上无意义），而不是给个假数字。"""
    ds = _days(10)
    # 只有 10 天 → returns 9 期 < 20
    series = [(d, 100.0 + i) for i, d in enumerate(ds)]
    df = _panel(10)
    res = Engine(_Half(), config=EngineConfig(benchmark_series=series)).run(df)
    m = res.metrics
    assert m["benchmark_available"] is False
    assert "20" in m["benchmark_note"]


def test_engine_benchmark_days_drops_unmatched_dates():
    """基准注入里故意缺 3 天 → benchmark_days 必须少于总期数。"""
    df = _panel(60)
    dates = sorted(set(df["trade_date"].to_list()))
    series = [p for i, p in enumerate(_bench_series(dates)) if i not in (10, 20, 30)]
    res = Engine(_Half(), config=EngineConfig(benchmark_series=series)).run(df)
    m = res.metrics
    assert m["benchmark_days"] < len(res.returns)
    assert "缺口双边丢弃" in m.get("benchmark_note", "") or \
        "不补造" in m.get("benchmark_note", "")


def test_engine_default_benchmark_does_not_raise_without_db(monkeypatch):
    """默认配置（查 index_daily）在库不可用时也不能炸回测。"""
    import lquant.backtest.benchmark as bm

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(bm, "load_index_series", _boom)
    df = _panel(40)
    res = Engine(_Half(), config=EngineConfig()).run(df)
    m = res.metrics
    assert m["benchmark_available"] is False
    assert "基准读取失败" in m["benchmark_note"]
    assert res.nav[-1][1] > 0
