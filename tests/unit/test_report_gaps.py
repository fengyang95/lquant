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
        df, "f", cat_col="grp", group_col="grp", bps_list=[5.0, 10.0])
    assert "归因分解" in html
    assert "分组 IC" in html
    assert "成本敏感性" in html


def test_factor_report_refuses_symbol_attribution() -> None:
    """不变量：归因维度必须是分类维度。

    按个股算「行业暴露」是上一轮的静默错误（写着「越接近 0 说明中性化越干净」，
    算的却是个股维度）。调用方传错也不能默默产出无意义输出 —— 本节跳过并留痕。
    """
    html = rep.factor_report(_panel(n_days=80), "f", cat_col="symbol")
    assert "<h2>归因分解" not in html
    assert "归因维度 &#x27;symbol&#x27; 是个股维度" in html
    assert "本节生成失败" in html


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


def test_factor_report_event_study_failure_is_disclosed(monkeypatch) -> None:
    """事件式算炸了 → 小节不出现，但必须留下失败横幅（不再静默吞掉）。"""
    def boom(*a, **k):
        raise ValueError("bad window")

    monkeypatch.setattr(rep, "event_study_summary", boom)
    html = rep.factor_report(_panel(), "f")
    assert "事件式分层收益" not in html
    assert "本节生成失败" in html
    assert "event_study" in html


def test_factor_report_optional_sections_failure_is_disclosed(
        monkeypatch) -> None:
    """可选小节算炸了 → 内容缺失，但故障必须可见（这是上一轮的静默缺陷）。"""
    def boom(*a, **k):
        raise RuntimeError("源数据异常")

    monkeypatch.setattr(rep, "attribution_summary", boom)
    monkeypatch.setattr(rep, "ic_by_group", boom)
    monkeypatch.setattr(rep, "factor_turnover", boom)
    monkeypatch.setattr(rep, "cost_matrix", boom)
    html = rep.factor_report(_panel(), "f", cat_col="grp",
                             group_col="grp", bps_list=[5.0])
    assert "归因分解" not in html
    assert "分组 IC" not in html
    assert "换手率" not in html
    assert "成本敏感性" not in html
    # 关键：不能无声消失
    assert "本节生成失败" in html
    for key in ("attribution", "group_ic", "turnover", "cost_matrix"):
        assert key in html


# --------------------------------------------------------------------------- #
# 内部辅助的边界分支（平时跑不到，但决定「显示成什么」）
# --------------------------------------------------------------------------- #
def test_ret_label_branches() -> None:
    assert rep._ret_label("fwd_ret_5") == "5 日前瞻收益"
    assert rep._ret_label("fwd_ret_x") == "fwd_ret_x"   # 不认识的持有期原样返回
    assert rep._ret_label("custom_ret") == "custom_ret"


def test_fmt_int_branches() -> None:
    assert rep._fmt_int(None) == "n/a"
    assert rep._fmt_int(True) == "True"
    assert rep._fmt_int(float("nan")) == "n/a"
    assert rep._fmt_int(3.9) == "3"
    assert rep._fmt_int("x") == "x"


def test_rows_table_empty_returns_empty() -> None:
    assert rep._rows_table([]) == ""
    assert rep._rows_table(None or []) == ""


def test_empty_blocks_are_skipped_not_rendered_as_blank() -> None:
    assert rep._views_html(None) == ""
    # 既没有可渲染的行、也没有口径说明 → 整节省略（不留一个空标题）
    assert rep._views_html({}) == ""
    assert rep._size_ic_html(None) == ""
    assert rep._size_ic_html({"rows": []}) == ""
    assert rep._style_html({}) == ""
    assert rep._style_html({"max_abs": None}) == ""


def test_table_survives_schema_introspection_failure() -> None:
    """取不到 schema 时只放弃「整数列优化」，不许把整张表打挂。"""
    class _BadSchema:
        def __getitem__(self, _k):
            raise RuntimeError("no schema")

    class _FakeDF:
        columns = ["a"]
        schema = _BadSchema()

        def __len__(self):
            return 1

        def iter_rows(self, named=False):
            yield {"a": 1.5}

    html = rep._table(_FakeDF(), limit=None)
    assert "<table>" in html and "1.5000" in html


def test_provenance_range_variants() -> None:
    kw = dict(display_name="f", expr="x", universe="all", n_samples=10,
              ret_col="fwd_ret_1", steps=None, covariates=None,
              sample_filters=None, window=60, n_groups=10,
              generator_version="2.0")
    assert "2026-01-01 起" in rep._provenance_html(data_start="2026-01-01",
                                                  data_end=None, **kw)
    assert "至 2026-02-01" in rep._provenance_html(data_start=None,
                                                   data_end="2026-02-01", **kw)
    assert "未提供" in rep._provenance_html(data_start=None, data_end=None, **kw)


def test_conclusion_section_absent_without_rating() -> None:
    assert rep._conclusion_html(None, None, "随意的一句话") == ""
