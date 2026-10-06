"""基本面 API 的分支级补充：缓存生命周期、行业清单、勾稽自动取数、降级路径。

这些分支在「一切正常」的集成测试里走不到，但它们正是线上最容易出事的地方：
缓存永不失效、湖目录不可读、勾稽端点拿不到报表、估值读取抛错。
"""
from __future__ import annotations

import time
from datetime import date

import polars as pl
import pytest
from fastapi import HTTPException

from lquant.server.api import fundamental as fd

PUB = date(2026, 3, 20)
Q1 = date(2025, 3, 31)
Q2 = date(2025, 6, 30)
Q4_PREV = date(2024, 12, 31)


@pytest.fixture(autouse=True)
def _clear_cache():
    fd.clear_fundamental_cache()
    yield
    fd.clear_fundamental_cache()


# --------------------------------------------------------------------------
# 缓存：TTL 与容量
# --------------------------------------------------------------------------

def test_cache_get_misses_when_empty():
    assert fd._cache_get(("nope",)) is None


def test_cache_returns_value_within_ttl():
    fd._cache_put(("k",), {"v": 1})
    assert fd._cache_get(("k",)) == {"v": 1}


def test_cache_entry_expires_and_is_evicted(monkeypatch):
    """过期条目必须被**移除**，而不只是返回 None —— 否则缓存会慢慢吃掉内存。"""
    fd._cache_put(("k",), "v")
    later = time.monotonic() + fd._CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(fd.time, "monotonic", lambda: later)
    assert fd._cache_get(("k",)) is None
    assert ("k",) not in fd._PANEL_CACHE


def test_cache_evicts_oldest_when_over_capacity(monkeypatch):
    """容量上限必须真的兜住：按 asof 遍历时不能无限增长。"""
    clock = {"t": 0.0}
    monkeypatch.setattr(fd.time, "monotonic", lambda: clock["t"])
    for i in range(fd._CACHE_MAX_ENTRIES + 3):
        clock["t"] = float(i)          # 保证有明确的「最旧」
        fd._cache_put((i,), i)
    assert len(fd._PANEL_CACHE) == fd._CACHE_MAX_ENTRIES
    assert (fd._CACHE_MAX_ENTRIES + 2,) in fd._PANEL_CACHE   # 最新写入的还在
    assert (0,) not in fd._PANEL_CACHE                        # 最旧的被淘汰


def test_clear_cache_empties_everything():
    fd._cache_put(("a",), 1)
    fd._cache_put(("b",), 2)
    fd.clear_fundamental_cache()
    assert fd._PANEL_CACHE == {}


# --------------------------------------------------------------------------
# 可用性诊断
# --------------------------------------------------------------------------

def test_data_availability_reports_both_sources():
    out = fd._data_availability(date(2026, 4, 1), 1234, 0)
    assert out["financial_pit"]["available"] is True
    assert out["financial_pit"]["lookback_years"] == fd._LOOKBACK_YEARS
    assert out["financial_pit"]["hint"] is None
    assert out["valuation_lake"]["available"] is False
    assert "估值" in out["valuation_lake"]["hint"]


def test_data_availability_survives_unreadable_lake(monkeypatch):
    """湖目录不可读（权限/路径异常）不该让接口 500，应降级为「不可用」。"""
    import lquant.data.store.parquet as pq

    def boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(pq, "lake_is_empty", boom)
    out = fd._data_availability(date(2026, 4, 1), 10, 0)
    assert out["valuation_lake"]["daily_empty"] is True
    assert out["valuation_lake"]["daily_basic_empty"] is True


def _bundle(**over):
    """构造一个**列齐全**的 PanelBundle。

    默认值故意给全 snapshot / detail 的列：之前给的是空 DataFrame，
    于是测试失败在「列不存在」，而不是真的在验证被测分支。
    """
    avail = {
        "financial_pit": {"available": True, "rows": 10, "hint": None},
        "valuation_lake": {"available": True, "rows": 5, "hint": None},
    }
    avail.update(over.pop("availability", {}))
    snap = over.pop("snapshot", pl.DataFrame({
        "symbol": ["600000.SH"], "industry": ["银行"], "n_scored": [10],
        "n_metrics": [17], "coverage": [0.6], "raw_score": [40.0],
        "available_max": [80.0], "normalized_score": [50.0], "rating": ["一般"],
        **{f"score_{m}": [5.0] for m in
           ("profitability", "cashflow", "efficiency", "solvency", "valuation")},
    }))
    detail = over.pop("detail", pl.DataFrame({
        "symbol": ["600000.SH"], "module": ["profitability"],
    }))
    return fd.PanelBundle(detail, snap, date(2026, 4, 1), avail)


def test_unavailable_hint_points_at_the_missing_source():
    """hint 必须指向**真正缺的那个来源**，否则用户会去修错的东西。"""
    fin_dead = _bundle(availability={"financial_pit": {
        "available": False, "rows": 0, "hint": "财务数据为空"}})
    assert fd._unavailable_hint(fin_dead) == "财务数据为空"

    val_dead = _bundle(availability={"valuation_lake": {
        "available": False, "rows": 0, "hint": "估值列为空"}})
    assert "分位样本不足" in fd._unavailable_hint(val_dead)
    assert "估值列为空" in fd._unavailable_hint(val_dead)

    both_ok = _bundle()
    assert "分位样本不足" in fd._unavailable_hint(both_ok)


# --------------------------------------------------------------------------
# 行业清单
# --------------------------------------------------------------------------

def test_list_industries_degrades_when_no_snapshot(monkeypatch):
    monkeypatch.setattr(fd, "_build_panel", lambda *a, **k: fd.PanelBundle(
        pl.DataFrame(), pl.DataFrame(), date(2026, 4, 1),
        {"financial_pit": {"available": False, "rows": 0, "hint": "空"}}))
    out = fd.list_industries(asof="2026-04-01", min_samples=5)
    assert out["available"] is False and out["rows"] == []
    assert out["availability"]["financial_pit"]["available"] is False


def test_list_industries_counts_and_sorts(monkeypatch):
    snap = pl.DataFrame({
        "symbol": ["a", "b", "c", None],
        "industry": ["银行", "银行", "白酒", None],
        "normalized_score": [60.0, 70.0, 80.0, 90.0],
    })
    monkeypatch.setattr(fd, "_build_panel",
                        lambda *a, **k: _bundle(snapshot=snap))
    out = fd.list_industries(asof="2026-04-01", min_samples=5)
    assert out["available"] is True
    # 按标的数降序；industry 为空的行被剔除（用户不该选中一个空行业）
    assert [r["industry"] for r in out["rows"]] == ["银行", "白酒"]
    assert out["rows"][0]["n"] == 2
    assert out["rows"][0]["avg_score"] == pytest.approx(65.0)


def test_list_industries_passes_min_samples_through(monkeypatch):
    """行业清单与排名必须用**同一个** min_samples，否则筛选器会列出空行业。"""
    seen: dict = {}

    def fake(asof, min_samples):
        seen["min_samples"] = min_samples
        return _bundle()

    monkeypatch.setattr(fd, "_build_panel", fake)
    fd.list_industries(asof="2026-04-01", min_samples=37)
    assert seen["min_samples"] == 37


# --------------------------------------------------------------------------
# 排序白名单
# --------------------------------------------------------------------------

def test_score_many_rejects_unknown_sort_column(monkeypatch):
    monkeypatch.setattr(fd, "_build_panel", lambda *a, **k: _bundle())
    with pytest.raises(HTTPException) as e:
        fd.score_many(fd.UniverseIn(asof="2026-04-01", sort_by="drop table"))
    assert e.value.status_code == 422


@pytest.mark.parametrize("col", sorted(fd._SORTABLE))
def test_every_sortable_column_is_accepted(monkeypatch, col):
    """白名单里的每一列都必须真的排得动 —— 名不副实的白名单等于 500。"""
    monkeypatch.setattr(fd, "_build_panel", lambda *a, **k: _bundle())
    out = fd.score_many(fd.UniverseIn(asof="2026-04-01", sort_by=col))
    assert out["available"] is True and len(out["rows"]) == 1


def test_score_many_industry_filter_and_empty_result(monkeypatch):
    snap = pl.DataFrame({
        "symbol": ["a", "b"], "industry": ["银行", "白酒"], "n_scored": [10, 10],
        "n_metrics": [17, 17], "coverage": [0.6, 0.6], "raw_score": [40.0, 40.0],
        "available_max": [80.0, 80.0], "normalized_score": [50.0, 55.0],
        "rating": ["一般", "一般"],
    })
    monkeypatch.setattr(fd, "_build_panel", lambda *a, **k: _bundle(snapshot=snap))

    keep = fd.score_many(fd.UniverseIn(asof="2026-04-01", industries=["银行"]))
    assert [r["symbol"] for r in keep["rows"]] == ["a"]
    # n_total 是**行业筛选后**的总数，不是全市场 —— 否则「命中 1/12」会误导
    assert keep["n_total"] == 1

    none = fd.score_many(fd.UniverseIn(asof="2026-04-01", industries=["不存在"]))
    assert none["available"] is True and none["rows"] == []
    assert none["n_total"] == 0


# --------------------------------------------------------------------------
# 分位与来源标注
# --------------------------------------------------------------------------

def test_percentiles_industry_filter(monkeypatch):
    detail = pl.DataFrame({
        "symbol": ["a", "b"], "industry": ["银行", "白酒"],
        "item": ["indicator.roe"] * 2, "p25": [8.0, 10.0], "p50": [12.0, 15.0],
        "p75": [16.0, 20.0], "n": [40, 20],
        "label": ["净资产收益率"] * 2, "module": ["profitability"] * 2,
    })
    monkeypatch.setattr(fd, "_build_panel", lambda *a, **k: _bundle(detail=detail))

    all_rows = fd.percentiles(asof="2026-04-01", min_samples=5, industry=None)
    assert len(all_rows["rows"]) == 2

    only_bank = fd.percentiles(asof="2026-04-01", min_samples=5, industry="银行")
    assert [r["industry"] for r in only_bank["rows"]] == ["银行"]
    assert only_bank["rows"][0]["p50"] == pytest.approx(12.0)


def test_source_of_falls_back_to_pit():
    """未知 item 归为 pit（而不是抛错）—— 明细里出现意外键时不该 500。"""
    assert fd._source_of("indicator.roe") == "pit"
    assert fd._source_of("derived.cfo_to_np") == "derived"
    assert fd._source_of("valuation.pe_ttm") == "valuation"
    assert fd._source_of("who.knows") == "pit"


def test_valuation_of_returns_none_on_error(monkeypatch):
    """估值读取抛错不该让评分接口 500。"""
    import lquant.fundamental as f

    def boom(*_a, **_k):
        raise OSError("lake broken")

    monkeypatch.setattr(f, "valuation_frame", boom)
    assert fd._valuation_of(date(2026, 4, 1), "600519.SH") is None


def test_valuation_of_returns_none_when_symbol_absent(monkeypatch):
    import lquant.fundamental as f

    monkeypatch.setattr(f, "valuation_frame", lambda *a, **k: pl.DataFrame(
        {"symbol": ["000001.SZ"], "pe_ttm": [9.0]}))
    assert fd._valuation_of(date(2026, 4, 1), "600519.SH") is None

    monkeypatch.setattr(f, "valuation_frame", lambda *a, **k: pl.DataFrame(
        {"symbol": ["600519.SH"], "pe_ttm": [19.3], "pb": [6.25]}))
    got = fd._valuation_of(date(2026, 4, 1), "600519.SH")
    assert got == {"pe_ttm": 19.3, "pb": 6.25}


# --------------------------------------------------------------------------
# 勾稽：累计口径折算
# --------------------------------------------------------------------------

def test_quarterly_value_leaves_point_in_time_fields_alone():
    """资产负债表类（时点值）不折算，直接返回当期值。"""
    assert fd._quarterly_value("delta_retained", 5.0, 1.0, Q2, Q1) == 5.0
    assert fd._quarterly_value("balance_cash_change", 5.0, 1.0, Q2, Q1) == 5.0


def test_quarterly_value_returns_none_without_current():
    assert fd._quarterly_value("net_income", None, 1.0, Q2, Q1) is None


def test_quarterly_value_without_previous_period_only_q1_is_valid():
    """没有上期时，只有 Q1 的累计值恰好等于单季值。"""
    assert fd._quarterly_value("net_income", 7.0, None, Q1, None) == 7.0
    assert fd._quarterly_value("net_income", 7.0, None, Q2, None) is None


def test_quarterly_value_subtracts_within_same_year():
    assert fd._quarterly_value("net_income", 10.0, 4.0, Q2, Q1) == pytest.approx(6.0)


def test_quarterly_value_cross_year_treats_current_as_single_quarter():
    assert fd._quarterly_value("net_income", 8.0, 30.0, Q1, Q4_PREV) == 8.0
    # 跨年但本期不是 Q1 → 口径无法成立（数据错乱时的保守处理）
    assert fd._quarterly_value("net_income", 8.0, 30.0, Q2, Q4_PREV) is None


# --------------------------------------------------------------------------
# 勾稽：自动取数
# --------------------------------------------------------------------------

class _FakeCon:
    def __init__(self, frame: pl.DataFrame | None = None, exc: Exception | None = None):
        self._frame, self._exc = frame, exc

    def execute(self, *_a, **_k):
        if self._exc:
            raise self._exc
        return self

    def pl(self):
        return self._frame


def _rows(frame: pl.DataFrame):
    return frame


def _mkframe(entries: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"stat_date": d, "item": i, "pub_date": PUB, "value": v}
         for d, i, v in entries])


def test_auto_reconcile_degrades_when_table_unreadable():
    con = _FakeCon(exc=RuntimeError("no such table"))
    inputs, meta = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))
    assert inputs == {} and "不可读" in meta["auto_error"]


def test_auto_reconcile_reports_missing_data():
    con = _FakeCon(_mkframe([]))
    inputs, meta = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))
    assert inputs == {} and "没有可用的报表数据" in meta["auto_error"]


def test_auto_reconcile_single_period_q2_cannot_de_cumulate():
    """只有一个报告期时，Q2 的累计值无法折算成单季 → 累计类科目不应给出。"""
    con = _FakeCon(_mkframe([
        (Q2, "income.n_income_attr_p", 100.0),
        (Q2, "balancesheet.undistr_porfit", 500.0),
    ]))
    inputs, meta = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))
    assert "net_income" not in inputs             # 累计口径，无法折算
    assert meta["previous_stat_date"] is None
    assert meta["period_basis"] == "quarterly"


def test_auto_reconcile_single_period_q1_is_valid():
    con = _FakeCon(_mkframe([
        (Q1, "income.n_income_attr_p", 100.0),
        (Q1, "cashflow.n_cashflow_act", 80.0),
    ]))
    inputs, _ = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))
    assert inputs["net_income"] == pytest.approx(100.0)
    assert inputs["operating_cashflow"] == pytest.approx(80.0)


def test_auto_reconcile_de_cumulates_and_computes_deltas():
    con = _FakeCon(_mkframe([
        (Q1, "income.n_income_attr_p", 40.0),
        (Q2, "income.n_income_attr_p", 100.0),        # 上半年累计
        (Q1, "cashflow.n_cashflow_act", 30.0),
        (Q2, "cashflow.n_cashflow_act", 70.0),
        (Q1, "balancesheet.undistr_porfit", 1000.0),
        (Q2, "balancesheet.undistr_porfit", 1060.0),
        (Q1, "balancesheet.money_cap", 200.0),
        (Q2, "balancesheet.money_cap", 230.0),
        (Q1, "indicator.profit_dedt", 60.0),
        (Q2, "indicator.profit_dedt", 99.0),
        (Q1, "income.compr_inc_attr_p", 0.4),
        (Q2, "income.compr_inc_attr_p", 1.0),
    ]))
    inputs, meta = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))

    assert inputs["net_income"] == pytest.approx(60.0)            # 100 − 40
    assert inputs["operating_cashflow"] == pytest.approx(40.0)    # 70 − 30
    assert inputs["delta_retained"] == pytest.approx(60.0)        # 时点值直接相减
    assert inputs["balance_cash_change"] == pytest.approx(30.0)
    # 累计类科目同样先单季化（本期累计 − 上期累计）
    assert inputs["deducted_net_income"] == pytest.approx(39.0)   # 99 − 60
    assert inputs["other_comprehensive"] == pytest.approx(0.6)    # 1.0 − 0.4
    assert meta["latest_stat_date"] == Q2.isoformat()
    assert meta["previous_stat_date"] == Q1.isoformat()


def test_auto_reconcile_skips_deltas_when_previous_missing():
    """只有本期货币资金时不能编造变动 —— 缺失就是不填，不是 0。"""
    con = _FakeCon(_mkframe([
        (Q1, "balancesheet.money_cap", 200.0),
    ]))
    inputs, _ = fd._auto_reconcile_inputs(con, "600519.SH", date(2026, 4, 1))
    assert "balance_cash_change" not in inputs


# --------------------------------------------------------------------------
# 勾稽：端点合并逻辑
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, con):
        self._con = con

    def __enter__(self):
        return self._con

    def __exit__(self, *_a):
        return False


def test_reconcile_merges_request_values_over_auto(monkeypatch):
    """显式传入的值优先于自动取数 —— 否则用户的手工修正会被静默覆盖。"""
    con = _FakeCon(_mkframe([
        (Q1, "income.n_income_attr_p", 40.0),
        (Q2, "income.n_income_attr_p", 100.0),
        (Q1, "cashflow.n_cashflow_act", 30.0),
        (Q2, "cashflow.n_cashflow_act", 70.0),
    ]))
    monkeypatch.setattr(fd, "reader", lambda: _Reader(con))
    out = fd.run_reconcile(fd.ReconcileIn(symbol="600519.SH", net_income=1234.0))
    assert out["inputs"]["net_income"] == 1234.0
    assert "net_income" in out["inputs_meta"]["from_request"]
    assert "net_income" not in out["inputs_meta"]["from_financial_pit"]
    # 其它项仍然自动取数
    assert "operating_cashflow" in out["inputs_meta"]["from_financial_pit"]


def test_reconcile_auto_false_uses_only_request_values(monkeypatch):
    def explode():  # pragma: no cover - 走到就说明 auto=False 没生效
        raise AssertionError("auto=False 时不该读库")

    monkeypatch.setattr(fd, "reader", explode)
    out = fd.run_reconcile(fd.ReconcileIn(
        symbol="600519.SH", auto=False, net_income=100.0, operating_cashflow=50.0))
    assert out["inputs"] == {"net_income": 100.0, "operating_cashflow": 50.0}
    assert out["inputs_meta"]["from_financial_pit"] == []


def test_reconcile_surfaces_auto_error_without_failing(monkeypatch):
    monkeypatch.setattr(fd, "reader", lambda: _Reader(
        _FakeCon(exc=RuntimeError("no table"))))
    out = fd.run_reconcile(fd.ReconcileIn(symbol="600519.SH"))
    assert "不可读" in out["inputs_meta"]["auto_error"]
    # 一项都没查到 → 不算通过（数据缺失不能当质量优秀）
    assert out["passed"] is False


def test_reconcile_resolves_symbol_and_asof(monkeypatch):
    con = _FakeCon(_mkframe([(Q1, "income.n_income_attr_p", 5.0)]))
    monkeypatch.setattr(fd, "reader", lambda: _Reader(con))
    out = fd.run_reconcile(fd.ReconcileIn(symbol="600519", asof="2026-04-01"))
    assert out["symbol"] == "600519.SH"          # 裸码被归一
    assert out["inputs_meta"]["asof"] == "2026-04-01"
