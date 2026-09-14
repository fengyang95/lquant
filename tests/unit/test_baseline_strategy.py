"""基准多因子策略测试:评分函数单测 + 沙箱源码执行 + 防前视断言。"""
from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.store import catalog
from lquant.research.strategies.baseline_multifactor import (
    FACTOR_FORMULAS,
    NET_PROFIT_YOY_ITEM,
    STRATEGY_CODE,
    composite_score,
)

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pandas")


@pytest.fixture()
def tmp_catalog(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "lq.duckdb"
    con = duckdb.connect(str(db))
    from lquant.data.store.ddl import DDL_STATEMENTS

    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    @contextmanager
    def _writer():
        c = duckdb.connect(str(db))
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @contextmanager
    def _reader():
        c = duckdb.connect(str(db))
        try:
            yield c
        finally:
            c.close()

    monkeypatch.setattr(catalog, "writer", _writer)
    monkeypatch.setattr(catalog, "reader", _reader)


def _make_df(n_days: int = 90, n_stocks: int = 4) -> pl.DataFrame:
    """n_days 个交易日 × n_stocks 只股票的行情面板(不同走势,因子有区分度)。"""
    syms = [f"60000{i}.SH" for i in range(n_stocks)]
    rows = []
    base_px = {s: 10.0 * (i + 1) for i, s in enumerate(syms)}
    drift = {s: 0.002 * (i + 1) for i, s in enumerate(syms)}
    for i in range(n_days):
        d = date.fromordinal(date(2025, 9, 1).toordinal() + i)
        for s in syms:
            p = base_px[s] * (1 + drift[s]) ** i
            pre = base_px[s] * (1 + drift[s]) ** (i - 1) if i else p
            rows.append(dict(trade_date=d, symbol=s, open=p, high=p * 1.005,
                             low=p * 0.995, close=p, pre_close=pre,
                             volume=1e8, amount=p * 1e8,
                             pe_ttm=10.0 * (syms.index(s) + 1),
                             pb_mrq=2.0, total_mv=1e10, float_mv=8e9,
                             turnover_rate=1.0))
    return pl.DataFrame(rows)


def _seed_financial(rows: list[dict]) -> None:
    df = pl.DataFrame(rows, schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    from lquant.data.store.catalog import FinancialRepo

    FinancialRepo().upsert(df)


@pytest.fixture()
def bars_file(tmp_path: Path, monkeypatch) -> pl.DataFrame:
    bars = _make_df()
    root = tmp_path / "data" / "daily" / "year=2025"
    root.mkdir(parents=True)
    bars.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")
    return bars


# ---------- composite_score 单测 ----------


def test_composite_score_orders_and_tolerates_nan():
    pe = pl.DataFrame({"code": ["A", "B", "C", "D"], "pe": [10.0, 20.0, 5.0, None]})
    yoy = pl.DataFrame({"code": ["A", "B", "C", "D"], "yoy": [0.1, 0.3, 0.2, 0.0]})
    mom = pl.DataFrame({"code": ["A", "B", "C", "D"], "mom": [0.05, -0.2, 0.01, 0.0]})
    vol = pl.DataFrame({"code": ["A", "B", "C", "D"], "vol": [0.2, 0.4, 0.3, 0.1]})
    out = composite_score(pe, yoy, mom, vol)
    assert set(out["code"]) == {"A", "B", "C", "D"}
    assert out["score"].is_sorted(descending=True)
    # D 缺 pe:pe 项按 0 计,不整行丢弃
    assert out.filter(pl.col("code") == "D")["score"][0] != 0.0


def test_composite_score_signs_low_pe_wins():
    """同分布下,低 pe + 高同比 + 高动量 + 低波动应排前。"""
    codes = ["A", "B", "C", "D"]
    pe = pl.DataFrame({"code": codes, "pe": [5.0, 50.0, 20.0, 30.0]})
    yoy = pl.DataFrame({"code": codes, "yoy": [0.4, 0.1, 0.2, 0.3]})
    mom = pl.DataFrame({"code": codes, "mom": [0.3, 0.0, 0.1, 0.2]})
    vol = pl.DataFrame({"code": codes, "vol": [0.05, 0.5, 0.3, 0.2]})
    out = composite_score(pe, yoy, mom, vol)
    assert out["code"][0] == "A"
    assert out["code"][-1] == "B"


def test_composite_score_all_nan_factor_zeroed():
    """整列缺失的因子按 0 合成(std=0 → z=0),不产生 NaN score。"""
    codes = ["A", "B"]
    pe = pl.DataFrame({"code": codes, "pe": [None, None]})
    yoy = pl.DataFrame({"code": codes, "yoy": [0.1, 0.2]})
    mom = pl.DataFrame({"code": codes, "mom": [0.1, 0.2]})
    vol = pl.DataFrame({"code": codes, "vol": [0.1, 0.2]})
    out = composite_score(pe, yoy, mom, vol)
    assert out["score"].null_count() == 0
    assert out["score"].is_sorted(descending=True)


def test_composite_score_real_nan_never_ranks_first():
    """G10:真实 pandas NaN(不是 null)不得穿透评分。

    fill_null 接不住 NaN:NaN 会让全截面 z 分数变 NaN,sort(descending)
    时 NaN 排第一 → 缺数据的股票最先入池。修复后 NaN 因子按 0 计。
    """
    codes = ["A", "B", "C"]
    pe = pl.DataFrame({"code": codes, "pe": [10.0, float("nan"), 5.0]})
    yoy = pl.DataFrame({"code": codes, "yoy": [0.3, 0.1, 0.2]})
    mom = pl.DataFrame({"code": codes, "mom": [0.3, 0.0, 0.1]})
    vol = pl.DataFrame({"code": codes, "vol": [0.1, 0.4, 0.2]})
    out = composite_score(pe, yoy, mom, vol)
    assert out["score"].null_count() == 0
    assert all(math.isfinite(s) for s in out["score"])
    # NaN pe 按 0 计后 B 其余因子也差 → B 排最后,不得因 NaN 排第一
    assert out["code"][0] == "A"
    assert out["code"][-1] == "B"
    assert out["score"].is_sorted(descending=True)


def test_sandbox_score_real_nan_isolated_and_deterministic():
    """G10 沙箱路径:`or 0.0` 接不住 NaN,一个 NaN 会污染全截面 z 分数。"""
    ns = _exec_sandbox()
    scored = ns["_composite_score_xs"](
        ["A", "B", "C"],
        {"A": 10.0, "B": float("nan"), "C": 5.0},
        {"A": 0.1, "B": 0.2, "C": 0.3},
        {"A": 0.1, "B": 0.2, "C": 0.3},
        {"A": 0.1, "B": 0.2, "C": 0.3},
    )
    scores = dict(scored)
    assert all(math.isfinite(s) for s in scores.values())
    # NaN pe 按 0 计 → 可手算:score A=-2.0、B=C=1.0(稳定排序保持 B、C 原序)
    assert [c for c, _ in scored] == ["B", "C", "A"]
    assert scores["A"] == pytest.approx(-2.0, abs=1e-9)
    assert scores["B"] == pytest.approx(1.0, abs=1e-9)


# ---------- 合成小样本全语义单测:PIT / 剔除 / 调仓 / T+1 ----------
#
# 面板统一用「平价」行情:4(或 60)只股票 130 个交易日,close 恒 10.0、
# pe_ttm 恒 20 → pe/mom/vol 三个因子横截面方差为 0,z 分数全 0,
# score = z_yoy。财务行是唯一的排名来源,便于手算期望排名。
# 调仓日 = 月内第 1 个交易日:07-01 / 08-01 / 09-01 / 10-01。
# 注意 MIN_LISTED_DAYS=60:08-01 的 attribute_history 恰好 60 行 → 可交易;
# 06-02、07-01 不足 60 行 → 必然空仓。


def _flat_panel_rows(symbols: list[str], start: date, n_days: int,
                     skip: set[tuple[str, date]] | None = None) -> list[dict]:
    """平价行情面板行;skip 里的 (symbol, date) 缺 bar → 当日停牌。"""
    from datetime import timedelta

    skip = skip or set()
    rows = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        for s in symbols:
            if (s, d) in skip:
                continue
            rows.append(dict(trade_date=d, symbol=s, open=10.0, high=10.05,
                             low=9.95, close=10.0, pre_close=10.0,
                             volume=1e8, amount=1e9, pe_ttm=20.0, pb_mrq=2.0,
                             total_mv=1e10, float_mv=8e9, turnover_rate=1.0))
    return rows


@pytest.fixture()
def bars_flat_130(tmp_path: Path, monkeypatch) -> pl.DataFrame:
    """130 个交易日 × 4 只股票的平价面板(2025-06-02 起)。"""
    syms = [f"60000{i}.SH" for i in range(4)]
    bars = pl.DataFrame(_flat_panel_rows(syms, date(2025, 6, 2), 130))
    root = tmp_path / "data" / "daily" / "year=2025"
    root.mkdir(parents=True)
    bars.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")
    return bars


def _fin_row(symbol: str, stat: date, pub: date, value: float) -> dict:
    return {"symbol": symbol, "stat_date": stat, "pub_date": pub,
            "report_type": f"{stat.year}Q{(stat.month - 1) // 3 + 1}",
            "item": NET_PROFIT_YOY_ITEM, "value": value}


def _run_baseline(bars: pl.DataFrame, cash: float = 10_000_000.0):
    from lquant.backtest.jqapi import JQRunner

    return JQRunner(STRATEGY_CODE, initial_cash=cash,
                    factor_formulas=FACTOR_FORMULAS).run(bars)


def test_baseline_pit_no_lookahead(tmp_catalog, bars_flat_130):
    """pub_date 晚于调仓日的财报不可见:08-01 只能看 Q1(0.10),09-01 才见 Q2(0.99)。"""
    syms = [f"60000{i}.SH" for i in range(4)]
    _seed_financial(
        # Q1:pub 07-01 ≤ 08-01 → 四只全部可见,值都是 0.10
        [_fin_row(s, date(2025, 6, 30), date(2025, 7, 1), 0.10) for s in syms]
        # Q2(仅 600001):stat 更新但 pub 08-15 > 08-01 → 08-01 不可见、09-01 可见
        + [_fin_row("600001.SH", date(2025, 7, 31), date(2025, 8, 15), 0.99)]
        # 未来公告:pub 2026-01-05 > 全部交易日 → 判别值 999 任何调仓日都不可见
        + [_fin_row("600002.SH", date(2025, 11, 30), date(2026, 1, 5), 999.0)])
    res = _run_baseline(bars_flat_130)
    assert res.error is None, res.error

    yoy_max = dict(res.records.get("yoy_max", []))
    # 手算:08-01 调仓只看 pub<=08-01 的行 → 全部 0.10;
    # 09-01/10-01 起 600001 的 Q2(0.99)生效,且是可见最大值。
    assert yoy_max == {date(2025, 8, 1): pytest.approx(0.10),
                       date(2025, 9, 1): pytest.approx(0.99),
                       date(2025, 10, 1): pytest.approx(0.99)}
    # 判别值 999(未来财报)在所有 record 曲线中一次都不出现
    assert not any(v == pytest.approx(999.0)
                   for recs in res.records.values() for _, v in recs)
    # 08-01 调仓按旧 yoy(0.10)买满 4 只
    n_pos = dict(res.records.get("n_positions", []))
    assert n_pos[date(2025, 8, 1)] == 4
    assert n_pos[date(2025, 9, 1)] == 4


def test_baseline_excludes_halted(tmp_catalog, tmp_path: Path, monkeypatch):
    """当日无 bar 的股票不得进入持仓:600003 在 08-01 停牌 → 不买入;复牌后买入。"""
    syms = [f"60000{i}.SH" for i in range(4)]
    _seed_financial([_fin_row(s, date(2025, 7, 31), date(2025, 7, 20), 0.10)
                     for s in syms])
    # 08-01 600003 缺 bar → 当日停牌
    bars = pl.DataFrame(_flat_panel_rows(
        syms, date(2025, 6, 2), 130, skip={("600003.SH", date(2025, 8, 1))}))
    root = tmp_path / "data" / "daily" / "year=2025"
    root.mkdir(parents=True)
    bars.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")

    res = _run_baseline(bars)
    assert res.error is None, res.error

    p0801 = res.positions.get(date(2025, 8, 1), {})
    # 手算:08-01 可交易 = 其余 3 只;600003 无当日 bar → paused → 剔除
    assert set(p0801) == {"600000.SH", "600001.SH", "600002.SH"}
    # 08-01 全天没有任何 600003 的成交
    assert not [t for t in res.trades
                if t.trade_date == date(2025, 8, 1) and t.symbol == "600003.SH"]
    # 复牌后(10-01 调仓)恢复买入;注意 09-01 仍被 MIN_LISTED_DAYS 剔除:
    # attribute_history(60) 恰好跨过停牌日 → 只有 59 行(缺 bar 的历史行不补)。
    assert "600003.SH" in res.positions.get(date(2025, 10, 1), {})


def test_baseline_excludes_st(tmp_catalog, bars_flat_130):
    """security 表 is_st 标记的股票不得进入持仓(G1 接线已修复)。"""
    syms = [f"60000{i}.SH" for i in range(4)]
    _seed_financial([_fin_row(s, date(2025, 7, 31), date(2025, 7, 20), 0.10)
                     for s in syms])
    with catalog.writer() as con:
        con.execute("INSERT INTO security VALUES "
                    "('600003.SH', '*ST测试', 'stock', 'main', "
                    "DATE '2020-01-01', NULL, TRUE, 'test', now())")
    res = _run_baseline(bars_flat_130)
    assert res.error is None, res.error
    p0801 = res.positions.get(date(2025, 8, 1), {})
    # 手算:600003 标记 ST → 剔除;其余 3 只正常买入
    assert "600003.SH" not in p0801
    assert set(p0801) == {"600000.SH", "600001.SH", "600002.SH"}


def test_baseline_monthly_topn_exit_rule(tmp_catalog, tmp_path: Path, monkeypatch):
    """月初调仓买 top N;次月跌出 exit N 的持仓被卖;名次中间的保留不动。

    60 只股票、平价面板 → score = z_yoy,排名完全由财务行手算控制:
    - 08-01:只有 20 只 H(6001xx)有可见财报(yoy=100..81)→ top20 = H 全体,全仓买入;
    - 09-01:新财报(pub 08-25)重排:H0..H9 仍 top20(第 1-10 名),N0..N9
      升到第 11-20 名(新买入),H10..H14 落到第 21-25 名(keep 50 内 → 不动),
      H15..H19 跌到第 56-60 名(keep 50 外 → 卖出),N10..N39 第 26-55 名。
    现金约束:卖出 5 只释放 5×per,买入按排名顺序只能成交 N0..N4。
    """
    h = [f"6001{i:02d}.SH" for i in range(20)]
    n = [f"6002{i:02d}.SH" for i in range(40)]
    bars = pl.DataFrame(_flat_panel_rows(h + n, date(2025, 6, 2), 130))
    root = tmp_path / "data" / "daily" / "year=2025"
    root.mkdir(parents=True)
    bars.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")

    yoy2: dict[str, float] = {}
    for i, s in enumerate(h):
        yoy2[s] = (60.0 - i) if i < 10 else ((40.0 - (i - 10)) if i < 15 else 0.0)
    for j, s in enumerate(n):
        yoy2[s] = (50.0 - j) if j < 10 else 35.0
    _seed_financial(
        [_fin_row(s, date(2025, 7, 31), date(2025, 7, 20), 100.0 - i)
         for i, s in enumerate(h)]
        + [_fin_row(s, date(2025, 8, 31), date(2025, 8, 25), v)
           for s, v in yoy2.items()])

    res = _run_baseline(bars)
    assert res.error is None, res.error

    p0801 = res.positions.get(date(2025, 8, 1), {})
    assert set(p0801) == set(h)                      # 手算:20 只 H 全部买入
    p0901 = res.positions.get(date(2025, 9, 1), {})
    # top20 内的老持仓保留(仍持有)
    assert all(s in p0901 for s in h[:10])
    # 名次中间(21-50)的持仓保留不动:仍持有且当日无任何成交
    assert all(s in p0901 for s in h[10:15])
    sells0901 = {t.symbol for t in res.trades
                 if t.trade_date == date(2025, 9, 1) and t.side.value == "sell"}
    buys0901 = {t.symbol for t in res.trades
                if t.trade_date == date(2025, 9, 1) and t.side.value == "buy"}
    assert not (set(h[:15]) & sells0901)             # 中间名次未被卖出
    # 跌出 exit 50 的持仓被卖:不再持有,当日有卖出成交
    assert all(s not in p0901 for s in h[15:])
    assert {t.symbol for t in res.trades if t.side.value == "sell"} >= set(h[15:])
    assert sells0901 == set(h[15:])
    # 新 top20 买入(现金只够 5 只:600200..600204)
    assert set(n[:5]) <= set(p0901) and set(n[:5]) <= buys0901
    assert not (set(n[:5]) & sells0901)


def test_baseline_t_plus_one_fill(tmp_catalog, bars_flat_130):
    """T+1:开仓当日成交于当日开盘价;当日买入的份额当日不可卖,次日可卖。

    引擎口径(与聚宽一致,见 jqapi 模块注释):open 时点的委托按**当日**
    开盘价即时撮合,不存在「次日成交」;T+1 体现在可卖约束(sellable_after_days)。
    当日净值包含已成交仓位(没有未成交挂单)。
    """
    syms = [f"60000{i}.SH" for i in range(4)]
    _seed_financial([_fin_row(s, date(2025, 7, 31), date(2025, 7, 20), 0.10)
                     for s in syms])
    res = _run_baseline(bars_flat_130)
    assert res.error is None, res.error

    # 信号日 08-01 的买入全部当日开盘价成交:手算 10.0 × (1+默认滑点 0.0005) = 10.005
    fills0801 = [t for t in res.trades if t.trade_date == date(2025, 8, 1)]
    assert fills0801 and all(t.side.value == "buy" and t.price == pytest.approx(10.005)
                             for t in fills0801)
    # 当日收盘净值已包含仓位(已成交,无挂单)
    nav0801 = dict(res.nav).get(date(2025, 8, 1))
    assert nav0801 == pytest.approx(10_000_000.0, rel=1e-3)
    assert set(res.positions.get(date(2025, 8, 1), {})) == set(syms)

    # T+1 可卖约束:baseline 不会当日反手,用最小策略直接验证引擎规则
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(trade, time="open")

def trade(context):
    d = context.current_dt.date()
    if d.month == 8 and d.day == 1:
        order("600000.SH", 500)               # 开盘买入 500 股 @10.0
        order_target("600000.SH", 0)          # 当日卖出 → T+1 拒单
    if d.month == 8 and d.day == 2:
        order_target("600000.SH", 0)          # 按日历日 T+1:次日卖出成交
'''
    from lquant.backtest.jqapi import JQRunner

    t1 = JQRunner(code, initial_cash=1_000_000).run(bars_flat_130)
    assert t1.error is None, t1.error
    trades = [(t.trade_date, t.side.value, t.qty, t.price) for t in t1.trades]
    # 手算:08-01 买入 500@10.005(委托即时按当日开盘价 10.0 + 万五滑点成交);
    # 当日卖出被 T+1 拒单(无成交);08-02 卖出 500@9.995(10.0 × (1-0.0005))。
    assert trades == [(date(2025, 8, 1), "buy", 500.0, pytest.approx(10.005)),
                      (date(2025, 8, 2), "sell", 500.0, pytest.approx(9.995))]
    # 当日拒单进 rejected(T+1 可卖不足)
    assert ("2025-08-01", "600000.SH", "可卖不足一手或资金不足") in t1.rejected
    # 当日收盘持仓在册,净值手算 = 1,000,000 − 500×10.005(买入成本) + 500×10(收盘估值)
    assert dict(t1.nav)[date(2025, 8, 1)] == pytest.approx(999_997.5)
    assert t1.positions[date(2025, 8, 1)] == {"600000.SH": 500.0}


def _exec_sandbox() -> dict:
    """把 STRATEGY_CODE 编译进独立命名空间,返回其 globals。"""
    ns: dict = {}
    exec(compile(STRATEGY_CODE, "<strategy>", "exec"), ns)  # noqa: S102 - 测试用途
    return ns


def test_strategy_code_compiles_and_score_parity():
    """STRATEGY_CODE 可编译;内嵌评分与 composite_score 数值对拍(防两实现漂移)。"""
    ns = _exec_sandbox()
    assert "rebalance" in ns and "initialize" in ns
    scored = ns["_composite_score_xs"](
        ["A", "B", "C", "D"],
        {"A": 10.0, "B": 20.0, "C": 5.0, "D": None},
        {"A": 0.1, "B": 0.3, "C": 0.2, "D": 0.0},
        {"A": 0.05, "B": -0.2, "C": 0.01, "D": 0.0},
        {"A": 0.2, "B": 0.4, "C": 0.3, "D": 0.1},
    )
    codes = ["A", "B", "C", "D"]
    pe = pl.DataFrame({"code": codes, "pe": [10.0, 20.0, 5.0, None]})
    yoy = pl.DataFrame({"code": codes, "yoy": [0.1, 0.3, 0.2, 0.0]})
    mom = pl.DataFrame({"code": codes, "mom": [0.05, -0.2, 0.01, 0.0]})
    vol = pl.DataFrame({"code": codes, "vol": [0.2, 0.4, 0.3, 0.1]})
    expected = composite_score(pe, yoy, mom, vol)
    assert [c for c, _ in scored] == expected["code"].to_list()
    for (c, s), (e_code, e_score) in zip(scored, expected.iter_rows(), strict=True):
        assert c == e_code
        assert s == pytest.approx(e_score, abs=1e-9)


def test_strategy_uses_declared_factors_and_no_date_override():
    """策略只用声明的因子公式;不自带日期参数(PIT 由沙箱绑定保证)。"""
    assert FACTOR_FORMULAS == ["pct_change_20", "rolling_std_20"]
    assert "get_fundamentals(query(" in STRATEGY_CODE
    assert "date=" not in STRATEGY_CODE          # 不覆盖交易日 → 不可绕开 PIT
    assert NET_PROFIT_YOY_ITEM.count(".") == 1
    _tbl, _attr = NET_PROFIT_YOY_ITEM.split(".")
    assert f"{_tbl}.{_attr}" in STRATEGY_CODE


def test_strategy_runs_in_jq_sandbox(tmp_catalog, bars_file):
    """STRATEGY_CODE 全链路:基本面×技术因子 → 调仓 → record 观测。"""
    _seed_financial([
        {"symbol": "600000.SH", "stat_date": date(2025, 7, 31), "pub_date": date(2025, 8, 15),
         "report_type": "2025Q2", "item": NET_PROFIT_YOY_ITEM, "value": 0.35},
        {"symbol": "600001.SH", "stat_date": date(2025, 7, 31), "pub_date": date(2025, 8, 15),
         "report_type": "2025Q2", "item": NET_PROFIT_YOY_ITEM, "value": 0.15},
        {"symbol": "600002.SH", "stat_date": date(2025, 7, 31), "pub_date": date(2025, 8, 15),
         "report_type": "2025Q2", "item": NET_PROFIT_YOY_ITEM, "value": 0.25},
        {"symbol": "600003.SH", "stat_date": date(2025, 7, 31), "pub_date": date(2025, 8, 15),
         "report_type": "2025Q2", "item": NET_PROFIT_YOY_ITEM, "value": 0.05},
    ])
    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(STRATEGY_CODE, initial_cash=1_000_000,
                   factor_formulas=FACTOR_FORMULAS).run(bars_file)
    assert res.error is None, res.error
    recs = res.records.get("n_positions", [])
    assert recs and any(v > 0 for _, v in recs)


def test_strategy_no_lookahead_in_sandbox(tmp_catalog, bars_file):
    """防前视:pub_date 晚于全部交易日的财务行(判别值 999.0)绝不可见。"""
    syms = ["600000.SH", "600001.SH", "600002.SH", "600003.SH"]
    _seed_financial([
        {"symbol": s, "stat_date": date(2025, 7, 31), "pub_date": date(2025, 8, 15),
         "report_type": "2025Q2", "item": NET_PROFIT_YOY_ITEM, "value": 0.10}
        for s in syms
    ] + [
        # 未来公告:pub_date=2026-01-05 > 全部交易日(2025-09-01..11-29)
        {"symbol": "600001.SH", "stat_date": date(2025, 11, 30), "pub_date": date(2026, 1, 5),
         "report_type": "2025Q4", "item": NET_PROFIT_YOY_ITEM, "value": 999.0},
    ])
    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(STRATEGY_CODE, initial_cash=1_000_000,
                   factor_formulas=FACTOR_FORMULAS).run(bars_file)
    assert res.error is None, res.error
    # 策略内可见 yoy 最大值是记录观测;若 resolve 泄漏未来行,会看到 999.0
    yoy_max = {v for _, v in res.records.get("yoy_max", [])}
    assert yoy_max and max(yoy_max) == pytest.approx(0.10)
