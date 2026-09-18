"""factors.evaluate.report 缺口分支覆盖：SVG 辅件 + factor_report 分支（全打桩）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.factors.evaluate import report as rep


@pytest.fixture(autouse=True)
def _stub_event_study(monkeypatch):
    """真实 event_study_summary 对合成小 panel 会算出 null spread（TypeError），
    统一打桩；需要真实实现的用例自行覆盖。"""
    monkeypatch.setattr(
        rep, "event_study_summary",
        lambda *a, **k: {"rel_periods": [-1, 0, 1],
                         "curve": {"Q1": [0.1, None, 0.3],
                                   "Q2": [0.2, 0.2, 0.4]},
                         "look_ahead_ratio": 0.5})


def _panel(n_days=30, n_sym=4, seed=3):
    """确定性因子 panel。"""
    rows = []
    base = date(2026, 1, 5)
    for i in range(n_days):
        d = base + timedelta(days=i)
        for j in range(n_sym):
            f = j * 1.0 + (i % 3) * 0.1
            rows.append({
                "trade_date": d, "symbol": f"{600000 + j * 10}.SH",
                "close": 10.0 + j, "f": f, "fwd_ret_1": 0.001 * (j + 1),
                "grp": "g1" if j < 5 else "g2",
            })
    return pl.DataFrame(rows)


def test_fmt_variants() -> None:
    assert rep._fmt(None) == "n/a"
    assert rep._fmt("<b>") == "&lt;b&gt;"
    assert rep._fmt(True) == "True"
    assert rep._fmt(float("nan")) == "n/a"
    assert rep._fmt(0.1234) == "0.1234"
    assert rep._fmt(0.1234, pct=True) == "12.34%"
    assert rep._fmt(date(2026, 1, 5)) == "2026-01-05"


def test_cls_variants() -> None:
    assert rep._cls(0.5) == "pos"
    assert rep._cls(-0.5) == "neg"
    assert rep._cls(0.0) == ""
    assert rep._cls(None) == ""
    assert rep._cls("x") == ""


def test_svg_line_empty_and_mismatch() -> None:
    assert "数据不足" in rep._svg_line([], [], label="L")
    assert "数据不足" in rep._svg_line([1, 2], [1], label="L")


def test_svg_line_all_nonfinite() -> None:
    assert "数据不足" in rep._svg_line([1, 2], [float("nan")] * 2, label="L")


def test_svg_line_render() -> None:
    svg = rep._svg_line([1, 2], [0.5, -0.5])
    assert "<polyline" in svg and "viewBox" in svg


def test_svg_bars_empty() -> None:
    assert "数据不足" in rep._svg_bars([], [])


def test_svg_bars_render() -> None:
    svg = rep._svg_bars(["a", "b"], [1.0, -1.0])
    assert "<rect" in svg
    assert rep.UP in svg and rep.DOWN in svg


def test_palette_single() -> None:
    assert rep._palette(1) == [rep.LINE]


def test_palette_gradient() -> None:
    colors = rep._palette(3)
    assert len(colors) == 3 and colors[0].startswith("hsl(")


def test_svg_multi_empty() -> None:
    assert "数据不足" in rep._svg_multi([], {}, label="M")
    assert "数据不足" in rep._svg_multi([1, 2], {"a": [float("nan")] * 2},
                                        label="M")


def test_svg_multi_degenerate_span() -> None:
    svg = rep._svg_multi([1, 2], {"a": [1.0, 1.0]})
    assert "<polyline" in svg


def test_svg_multi_seg_break_on_nonfinite() -> None:
    svg = rep._svg_multi([1, 2, 3], {"a": [1.0, None, 3.0]})
    assert svg.count("<polyline") == 2  # None 处断段


def test_svg_multi_mark_x_and_label() -> None:
    svg = rep._svg_multi([1, 2], {"a": [1.0, 2.0]}, mark_x=100.0,
                         mark_label="虚线")
    assert "stroke-dasharray" in svg and "虚线" in svg


def test_table_empty() -> None:
    assert "无数据" in rep._table(None)
    assert "无数据" in rep._table(pl.DataFrame())


def test_table_renders_rows_and_pct() -> None:
    df = pl.DataFrame({"a": [0.1, 0.2], "b": ["x", "y"]})
    html = rep._table(df, pct_cols=("a",))
    assert "<td>10.00%</td>" in html and "<td>x</td>" in html


def test_factor_report_missing_factor_column() -> None:
    with pytest.raises(KeyError, match="因子列不存在"):
        rep.factor_report(_panel(), "nope")


def test_factor_report_basic_html(monkeypatch) -> None:
    html = rep.factor_report(_panel(), "f", universe="沪深300")
    assert "因子研究报告" in html
    assert "沪深300" in html
    assert "<html" in html


def test_factor_report_filter_zscore_path() -> None:
    html = rep.factor_report(_panel(n_days=80), "f", filter_zscore=3.0)
    assert "样本过滤" in html


def test_factor_report_outlier_stats_only() -> None:
    html = rep.factor_report(
        _panel(), "f",
        outlier_stats={"threshold": 3.0, "n_dropped": 2, "n_in": 100,
                       "dropped_rate": 0.02})
    assert "样本过滤" in html


def test_factor_report_optional_sections() -> None:
    df = _panel(n_days=80, n_sym=10)
    html = rep.factor_report(
        df, "f", cat_col="symbol", group_col="grp", bps_list=[5.0, 10.0])
    assert "归因分解" in html
    assert "分组 IC" in html
    assert "成本敏感性" in html


def test_factor_report_turnover_section() -> None:
    html = rep.factor_report(_panel(n_days=80), "f")
    assert "换手率" in html


_ES_CURVE = {"rel_periods": [-1, 0, 1], "curve": {"Q1": [0.1, None, 0.3],
                                                  "Q2": [0.2, 0.2, 0.4]},
             "look_ahead_ratio": 0.5}


def test_factor_report_event_study(monkeypatch) -> None:
    monkeypatch.setattr(rep, "event_study_summary",
                        lambda *a, **k: _ES_CURVE)
    html = rep.factor_report(_panel(), "f")
    assert "事件式分层收益" in html
    assert "事前/事后发散度比" in html


def test_factor_report_event_study_exception_swallowed(monkeypatch) -> None:
    def boom(*a, **k):
        raise ValueError("bad window")

    monkeypatch.setattr(rep, "event_study_summary", boom)
    html = rep.factor_report(_panel(), "f")
    assert "事件式分层收益" not in html


def test_factor_report_optional_sections_exception_swallowed(
        monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("源数据异常")

    monkeypatch.setattr(rep, "attribution_summary", boom)
    monkeypatch.setattr(rep, "ic_by_group", boom)
    monkeypatch.setattr(rep, "factor_turnover", boom)
    monkeypatch.setattr(rep, "cost_matrix", boom)
    html = rep.factor_report(_panel(), "f", cat_col="symbol",
                             group_col="symbol", bps_list=[5.0])
    assert "归因分解" not in html
    assert "分组 IC" not in html
    assert "换手率" not in html
    assert "成本敏感性" not in html
