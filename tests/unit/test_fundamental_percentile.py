"""基本面：PIT 面板解析、行业相对分位、聚合与覆盖率、滚动打分。

核心回归点是 **PIT**：公告日之前绝不能看到该期报表
（FinancialTool 正是在这里出现前视偏差）。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.fundamental import (
    METRICS,
    MODULE_WEIGHTS,
    PercentileBand,
    aggregate,
    metrics_by_module,
    percentile_bands,
    percentile_table,
    rating,
    resolve_industry,
    resolve_pit,
    score_by_percentile,
    score_history,
    score_snapshot,
    score_universe,
    total_weight,
    validate_modules,
)
from lquant.fundamental.metrics import RatioMetric

STAT = date(2025, 12, 31)
PUB = date(2026, 3, 20)
AFTER = date(2026, 4, 1)
BEFORE = date(2026, 1, 10)

#: 造数用的 item 必须取自**真实目录**。
#: 这里原先是写死的 ``profit.roeAvg`` / ``operation.NRTurnDays`` 等
#: BaoStock 遗留键 —— 恰好是当时 METRICS 的子集，于是测试全绿，
#: 而生产库（Tushare ``indicator.*``）17 个指标一个都取不到。
#: 契约由 tests/unit/test_fundamental_registry_contract.py 单独守。
FIXTURE_ITEMS = ("indicator.roe", "indicator.netprofit_margin", "indicator.assets_turn")
#: 一正一反，用于验证反向指标档位
FIXTURE_MIXED = ("indicator.roe", "indicator.debt_to_assets")


def make_panel(n_per_industry: int = 6, items: tuple[str, ...] | None = None
               ) -> tuple[pl.DataFrame, pl.DataFrame, list[str]]:
    items = items or FIXTURE_ITEMS
    symbols, industries = [], []
    rows = []
    for i in range(n_per_industry * 2):
        sym = f"6000{i:02d}.SH"
        symbols.append(sym)
        industries.append("白酒" if i < n_per_industry else "银行")
        for k, item in enumerate(items):
            rows.append({"symbol": sym, "stat_date": STAT, "pub_date": PUB,
                         "report_type": "2025Q4", "item": item,
                         "value": float(10 + i * 2 + k)})
    panel = pl.DataFrame(rows)
    ic = pl.DataFrame({"symbol": symbols, "std": "sw", "code": industries,
                       "name": industries, "std_date": [date(2024, 1, 1)] * len(symbols),
                       "source": ["test"] * len(symbols)})
    return panel, ic, symbols


# ---------- PIT 解析 ----------

def test_resolve_pit_hides_unpublished_reports():
    """公告日之前必须看不到该期报表 —— 这条挂了就是前视偏差。"""
    panel, _, _ = make_panel()
    assert resolve_pit(panel, BEFORE).height == 0
    assert resolve_pit(panel, PUB).height == 12          # pub_date <= asof（含当日）
    assert resolve_pit(panel, AFTER).height == 12


def test_resolve_pit_picks_latest_stat_date():
    panel = pl.DataFrame([
        {"symbol": "600000.SH", "stat_date": date(2025, 3, 31), "pub_date": date(2025, 4, 20),
         "item": "indicator.roe", "value": 5.0},
        {"symbol": "600000.SH", "stat_date": date(2025, 12, 31), "pub_date": date(2026, 3, 20),
         "item": "indicator.roe", "value": 9.0},
    ])
    got = resolve_pit(panel, AFTER)
    assert got["indicator.roe"][0] == 9.0
    assert got["stat_date"][0] == date(2025, 12, 31)


def test_resolve_pit_prefers_later_revision_same_period():
    panel = pl.DataFrame([
        {"symbol": "600000.SH", "stat_date": STAT, "pub_date": date(2026, 3, 20),
         "item": "indicator.roe", "value": 9.0},
        {"symbol": "600000.SH", "stat_date": STAT, "pub_date": date(2026, 3, 28),
         "item": "indicator.roe", "value": 11.0},
    ])
    assert resolve_pit(panel, AFTER)["indicator.roe"][0] == 11.0


def test_resolve_pit_item_filter_and_missing_columns():
    panel, _, _ = make_panel()
    got = resolve_pit(panel, AFTER, items=["indicator.roe"])
    assert "indicator.roe" in got.columns and "indicator.netprofit_margin" not in got.columns
    with pytest.raises(KeyError, match="缺列"):
        resolve_pit(pl.DataFrame({"symbol": ["x"]}), AFTER)


def test_resolve_pit_empty_panel():
    assert resolve_pit(pl.DataFrame(), AFTER).is_empty()


# ---------- 行业分类 PIT ----------

def test_resolve_industry_respects_std_date():
    ic = pl.DataFrame({"symbol": ["600000.SH"] * 2, "std": ["sw"] * 2,
                       "code": ["old", "new"], "name": ["旧行业", "新行业"],
                       "std_date": [date(2020, 1, 1), date(2026, 6, 1)]})
    assert resolve_industry(ic, date(2025, 1, 1))["industry"][0] == "旧行业"
    assert resolve_industry(ic, date(2026, 7, 1))["industry"][0] == "新行业"


def test_resolve_industry_std_filter_and_empty():
    ic = pl.DataFrame({"symbol": ["600000.SH"], "std": ["citics"], "code": ["x"],
                       "name": ["中信行业"], "std_date": [date(2020, 1, 1)]})
    assert resolve_industry(ic, AFTER, std="sw").is_empty()
    assert resolve_industry(ic, AFTER, std="citics").height == 1
    assert resolve_industry(pl.DataFrame(), AFTER).is_empty()


def test_resolve_industry_falls_back_to_code():
    ic = pl.DataFrame({"symbol": ["600000.SH"], "std": ["sw"], "code": ["801010"],
                       "std_date": [date(2020, 1, 1)]})
    assert resolve_industry(ic, AFTER)["industry"][0] == "801010"


# ---------- 分位与打分 ----------

def test_percentile_bands_min_samples_guard():
    assert percentile_bands([1, 2, 3, 4]) is None                 # 默认 5 个样本起
    band = percentile_bands([1, 2, 3, 4, 5], min_samples=5)
    assert band is not None and band.n == 5
    assert band.p50 == pytest.approx(3.0)
    assert percentile_bands([None, None], min_samples=1) is None


def test_score_by_percentile_higher_better():
    b = PercentileBand(p25=30.0, p50=50.0, p75=70.0, n=100)
    assert score_by_percentile(80.0, b) == 1.0
    assert score_by_percentile(70.0, b) == 1.0
    assert score_by_percentile(60.0, b) == 0.8
    assert score_by_percentile(40.0, b) == 0.5
    assert score_by_percentile(10.0, b) == 0.2


def test_score_by_percentile_lower_better():
    """周转天数、PE、资产负债率这类反向指标：越低越靠前。"""
    b = PercentileBand(p25=30.0, p50=50.0, p75=70.0, n=100)
    assert score_by_percentile(20.0, b, higher_better=False) == 1.0
    assert score_by_percentile(30.0, b, higher_better=False) == 1.0
    assert score_by_percentile(45.0, b, higher_better=False) == 0.8
    assert score_by_percentile(60.0, b, higher_better=False) == 0.5
    assert score_by_percentile(90.0, b, higher_better=False) == 0.2


def test_percentile_table_separates_industries():
    panel, ic, _ = make_panel()
    tbl = percentile_table(panel, ic, AFTER)
    assert set(tbl["industry"].unique()) == {"白酒", "银行"}
    assert tbl.height == 6                                    # 2 行业 × 3 指标
    assert tbl["n"].to_list() == [6] * 6


def test_percentile_table_empty_when_unpublished():
    panel, ic, _ = make_panel()
    assert percentile_table(panel, ic, BEFORE).is_empty()


def test_score_universe_columns_and_points():
    panel, ic, _ = make_panel()
    detail = score_universe(panel, ic, AFTER)
    assert detail.height == 36                                # 12 票 × 3 指标
    assert {"symbol", "item", "ratio", "points", "max_score"} <= set(detail.columns)
    assert all(r <= m for r, m in zip(detail["ratio"], detail["max_score"], strict=True))
    assert detail["points"].max() <= detail["max_score"].max()


# ---------- 聚合 ----------

def test_aggregate_normalized_and_coverage():
    panel, ic, _ = make_panel()
    snap = score_snapshot(panel, ic, AFTER)
    assert snap.height == 12
    row = snap.row(0, named=True)
    assert row["n_metrics"] == len(METRICS)
    assert row["coverage"] == pytest.approx(3 / len(METRICS))

    # 聚合数学必须与明细一致：raw = Σpoints，normalized = raw / Σmax
    detail = score_universe(panel, ic, AFTER)
    for sym in ("600000.SH", "600005.SH"):
        got = snap.filter(pl.col("symbol") == sym).row(0, named=True)
        sub = detail.filter(pl.col("symbol") == sym)
        raw = sub["points"].sum()
        avail = sub["max_score"].sum()
        assert got["raw_score"] == pytest.approx(raw)
        assert got["available_max"] == pytest.approx(avail)
        assert got["normalized_score"] == pytest.approx(raw / avail * 100.0)


def test_best_in_every_metric_reaches_full_normalized_score():
    """归一化分数的意义：覆盖度低也能拿满分，只要它在可比口径里最好。"""
    panel, ic, _ = make_panel(items=FIXTURE_MIXED)
    panel = panel.with_columns(
        pl.when(pl.col("symbol") == "600000.SH")
        .then(pl.when(pl.col("item") == "indicator.debt_to_assets").then(-1e6).otherwise(1e6))
        .otherwise(pl.col("value")).alias("value"))
    snap = score_snapshot(panel, ic, AFTER)
    best = snap.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert best["normalized_score"] == pytest.approx(100.0)
    assert best["raw_score"] == pytest.approx(best["available_max"])


def test_aggregate_empty_detail():
    assert aggregate(pl.DataFrame()).is_empty()


def test_rating_thresholds():
    assert rating(90.0) == "优秀"
    assert rating(85.0) == "优秀"
    assert rating(70.0) == "良好"
    assert rating(60.0) == "一般"
    assert rating(10.0) == "较差"


def test_score_history_rolls_dates():
    """滚动打分：公告前无数据、公告后有数据 —— 同一批票在两个观察日结果不同。"""
    panel, ic, _ = make_panel()
    hist = score_history(panel, ic, [BEFORE, AFTER])
    assert set(hist["asof_date"].unique()) == {AFTER}          # BEFORE 无可见数据
    hist2 = score_history(panel, ic, [BEFORE])
    assert hist2.is_empty()


def test_metrics_module_weights_are_self_consistent():
    """回归 FinancialTool 的 95≠100 缺陷：子项满分之和必须等于模块权重。"""
    validate_modules()
    assert total_weight() == pytest.approx(100.0)
    for mod, items in metrics_by_module().items():
        assert sum(m.max_score for m in items) == pytest.approx(MODULE_WEIGHTS[mod])


def test_validate_modules_rejects_inconsistent_catalogue(monkeypatch):
    bad = (RatioMetric("x", "x", "profitability", 999.0),)
    monkeypatch.setattr("lquant.fundamental.metrics.METRICS", bad)
    with pytest.raises(ValueError, match="子项满分之和"):
        validate_modules()


def test_validate_modules_rejects_unknown_module(monkeypatch):
    bad = (RatioMetric("x", "x", "nope", 1.0),)
    monkeypatch.setattr("lquant.fundamental.metrics.METRICS", bad)
    with pytest.raises(ValueError, match="未声明的模块"):
        validate_modules()


def test_metric_direction_label():
    up = RatioMetric("a", "A", "profitability", 1.0, higher_better=True)
    down = RatioMetric("b", "B", "profitability", 1.0, higher_better=False)
    assert up.direction == "越高越好" and down.direction == "越低越好"


def test_score_universe_matches_scalar_banding():
    """向量化打分必须与标量 :func:`score_by_percentile` 逐行一致。

    ``score_universe`` 为了性能把档位判断内联成了 Polars 表达式，
    而 ``score_by_percentile`` 仍是公开 API（也是档位口径的文档）。
    两份实现各写一遍阈值迟早会漂移 —— 这条测试把它们钉在一起：
    只要有人改了其中一边的阈值，就会在这里红。
    """
    from lquant.fundamental.percentile import score_by_percentile

    panel, ic, _ = make_panel(items=FIXTURE_MIXED, n_per_industry=8)
    detail = score_universe(panel, ic, AFTER)
    assert detail.height > 0

    by_item = {m.item: m for m in METRICS if m.item in set(FIXTURE_MIXED)}
    assert by_item, "fixture 的指标必须真实存在于目录里"

    for r in detail.iter_rows(named=True):
        band = PercentileBand(r["p25"], r["p50"], r["p75"], r["n"])
        expect = score_by_percentile(r["value"], band,
                                     higher_better=by_item[r["item"]].higher_better)
        assert r["ratio"] == pytest.approx(expect), (
            f"{r['symbol']} {r['item']} value={r['value']} "
            f"band=({r['p25']},{r['p50']},{r['p75']}) "
            f"向量化={r['ratio']} 标量={expect}")


def test_band_constants_are_shared_not_duplicated():
    """档位阈值只应存在一份定义，且顺序单调。"""
    from lquant.fundamental import percentile as p

    assert p.BAND_RATIO_TOP > p.BAND_RATIO_MID > p.BAND_RATIO_LOW > p.BELOW_FLOOR
    assert (p.BAND_RATIO_TOP, p.BAND_RATIO_MID, p.BAND_RATIO_LOW, p.BELOW_FLOOR) \
        == (1.0, 0.8, 0.5, 0.2)


def test_long_view_returns_none_when_no_metric_column_present():
    """面板里一个目录指标都没有 → 走 resolve_pit 的空帧分支。"""
    panel, ic, _ = make_panel(items=("unknown.a", "unknown.b"))
    assert percentile_table(panel, ic, AFTER).is_empty()
    assert score_universe(panel, ic, AFTER).is_empty()


def test_long_view_returns_none_when_industry_join_yields_nothing():
    """有财务数据、但行业分类一只都对不上 → 视为无可用数据。

    注意这与上一条是**不同分支**：上一条在 ``resolve_pit`` 就空了，
    这条是宽表非空、join 行业后为空。不区分就会写出「绿在错误分支上」的测试。
    """
    panel, _, _ = make_panel()                       # 指标都是真实目录键
    other = pl.DataFrame({
        "symbol": ["999999.SH"], "std": ["sw"], "code": ["银行"],
        "name": ["银行"], "std_date": [date(2024, 1, 1)], "source": ["test"],
    })
    assert resolve_pit(panel, AFTER).height > 0       # 宽表确实非空
    assert percentile_table(panel, other, AFTER).is_empty()
    assert score_universe(panel, other, AFTER).is_empty()


def test_bands_empty_when_all_values_null():
    """全为 null 时降级为空分位表，而不是让 quantile 产出垃圾。"""
    panel, ic, _ = make_panel()
    panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias("value"))
    tbl = percentile_table(panel, ic, AFTER)
    assert tbl.is_empty()
    assert set(tbl.columns) >= {"industry", "item", "p25", "p50", "p75", "n"}


def test_score_universe_empty_catalogue_and_unreachable_min_samples():
    """两条提前返回：目录为空、以及分位样本阈值高到没有行业达标。"""
    panel, ic, _ = make_panel()
    assert score_universe(panel, ic, AFTER, ()).is_empty()          # 空目录
    assert score_universe(panel, ic, AFTER, min_samples=100).is_empty()  # 无行业达标
