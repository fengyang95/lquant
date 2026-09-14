"""基准多因子策略测试:评分函数单测 + 沙箱源码执行 + 防前视断言。"""
from __future__ import annotations

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


# ---------- 沙箱源码:可编译、评分对拍、执行、防前视 ----------


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
    assert NET_PROFIT_YOY_ITEM.startswith("growth.")
    assert f"growth.{NET_PROFIT_YOY_ITEM.split('.')[1]}" in STRATEGY_CODE


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
