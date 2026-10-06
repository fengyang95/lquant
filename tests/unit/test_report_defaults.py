"""报告口径契约：默认值单一来源 + 版本/陈旧标记 + 身份披露。

对应评审 R12（三入口默认值漂移）、R14（内部列名印在用户面）、
R15（报告无版本、旧口径无法分辨）、R20（缺契约文档）。

这些断言的作用是**防止再次漂移**：默认值不许在入口处再写字面量。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.factors.evaluate import defaults as dflt
from lquant.factors.evaluate import report as rep


def _panel(n_days: int = 60, n_sym: int = 12) -> pl.DataFrame:
    rows = []
    base = date(2026, 1, 5)
    for i in range(n_days):
        d = base + timedelta(days=i)
        for j in range(n_sym):
            rows.append({
                "trade_date": d,
                "symbol": f"{600000 + j:06d}.SH",
                "close": 10.0 + 0.1 * j + 0.01 * i,
                "amount": 1e7 + 1e6 * j,
                "market_cap": 1e9 + 1e8 * j,
                "industry_sw1": f"I{j % 3}",
                "test_factor": 0.02 * j + 0.3 * math.sin(i * 0.7 + j * 1.3),
                "fwd_ret_1": 0.001 * (j + 1) + 0.0005 * i,
                "fwd_ret_5": 0.002 * (j + 1),
            })
    return pl.DataFrame(rows)


# --------------------------------------------------------------------------- #
# R12：默认值单一来源
# --------------------------------------------------------------------------- #
def test_entry_defaults_come_from_one_place() -> None:
    """API（评价/合成）/ 报告 / 衰减引擎的默认值必须 === defaults.py 的常量。"""
    from lquant.server.api.factors import EvaluateIn, SynthesizeIn

    assert EvaluateIn().n_groups == dflt.DEFAULT_N_GROUPS
    assert EvaluateIn().horizons == list(dflt.DEFAULT_DECAY_HORIZONS)
    assert EvaluateIn().window == dflt.DEFAULT_WINDOW
    assert tuple(EvaluateIn().event_window) == dflt.DEFAULT_EVENT_WINDOW
    assert SynthesizeIn(formulas=["a", "b"]).n_groups == dflt.DEFAULT_N_GROUPS

    from inspect import signature

    for fn in (rep.factor_report,):
        p = signature(fn).parameters
        assert p["n_groups"].default == dflt.DEFAULT_N_GROUPS
        assert p["window"].default == dflt.DEFAULT_WINDOW
        assert p["event_window"].default == dflt.DEFAULT_EVENT_WINDOW


def test_decay_engine_uses_canonical_ladder() -> None:
    from lquant.factors.evaluate.decay import decay_profile

    prof = decay_profile(_panel(), "test_factor")
    assert prof["horizon"].to_list() == list(dflt.DEFAULT_DECAY_HORIZONS)


def test_cli_option_defaults_come_from_one_place() -> None:
    """CLI 的 --n-groups / --horizons / --bps 默认值同源。"""
    from lquant.cli.commands.factor import audit, report, robust

    def opt(cmd, name):
        return next(p for p in cmd.params if p.name == name).default

    assert opt(report, "n_groups") == dflt.DEFAULT_N_GROUPS
    assert opt(report, "bps") == ",".join(str(int(b)) for b in dflt.DEFAULT_BPS)
    assert opt(robust, "n_groups") == dflt.DEFAULT_N_GROUPS
    assert opt(audit, "horizons") == dflt.horizons_csv()


def test_decay_horizons_helper() -> None:
    assert dflt.decay_horizons(None) == list(dflt.DEFAULT_DECAY_HORIZONS)
    assert dflt.decay_horizons([]) == list(dflt.DEFAULT_DECAY_HORIZONS)
    # 显式传入：去重保序，不静默换成默认
    assert dflt.decay_horizons([5, 1, 5]) == [5, 1]


# --------------------------------------------------------------------------- #
# R14：内部标识符不上用户面
# --------------------------------------------------------------------------- #
def test_report_hides_internal_column_names() -> None:
    html = rep.factor_report(_panel(), "test_factor", "fwd_ret_1",
                             display_name="动量因子", expr="pct_change_20",
                             cat_col="industry_sw1", universe="all",
                             n_samples=720, steps=[
                                 {"op": "winsorize", "method": "mad"},
                                 {"op": "neutralize", "method": "ols"},
                             ],
                             covariates={"market_cap": 1.0})
    assert "申万一级行业" in html
    assert "归因分解 · industry_sw1" not in html
    assert "归因分解 · cov_industry_sw1" not in html
    assert "1 日前瞻收益" in html
    assert "全市场" in html
    assert "去极值" in html and "中性化" in html
    # 报告头部不再印内部列名
    assert "前瞻收益 fwd_ret_1" not in html


def test_report_discloses_decay_ladder() -> None:
    html = rep.factor_report(_panel(), "test_factor", display_name="f",
                             horizons=[1, 5, 20])
    assert "衰减阶梯" in html
    assert "1 日、5 日、20 日" in html


def test_report_uses_default_ladder_when_unspecified() -> None:
    html = rep.factor_report(_panel(), "test_factor", display_name="f")
    assert "60 日" in html


# --------------------------------------------------------------------------- #
# R15：版本 / 生成时间 / 陈旧标记
# --------------------------------------------------------------------------- #
def test_report_embeds_version_and_generated_at() -> None:
    html = rep.factor_report(_panel(), "test_factor", display_name="f",
                             generator_version="9.9",
                             generated_at="2026-02-01 09:30")
    assert '<meta name="lquant-report-generator" content="9.9">' in html
    assert '<meta name="lquant-report-generated-at" content="2026-02-01 09:30">' in html
    assert "报告版本 9.9" in html
    assert "2026-02-01 09:30" in html


def test_report_generated_at_defaults_to_now() -> None:
    from datetime import datetime

    html = rep.factor_report(_panel(), "test_factor", display_name="f")
    assert datetime.now().strftime("%Y-%m-%d") in html


def test_report_meta_roundtrip_and_stale_flag(tmp_path, monkeypatch) -> None:
    """报告中心读得到版本；旧产物（无版本号）必须被判为陈旧。"""
    from lquant.server.api import factors as api

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    cur = rep.factor_report(_panel(), "test_factor", display_name="f")
    (tmp_path / "current.html").write_text(cur, encoding="utf-8")
    # 模拟修复前的产物：结构一样，但没有版本 meta
    legacy = cur.replace(
        '<meta name="lquant-report-generator" content="2.0">\n', "")
    (tmp_path / "legacy.html").write_text(legacy, encoding="utf-8")

    rows = {r["name"]: r for r in api.list_reports()}
    assert set(rows) == {"current", "legacy"}
    assert rows["current"]["generator_version"] == rep.REPORT_GENERATOR_VERSION
    assert rows["current"]["stale"] is False
    assert rows["current"]["generated_at"]
    assert rows["current"]["current_version"] == rep.REPORT_GENERATOR_VERSION
    assert rows["legacy"]["generator_version"] is None
    assert rows["legacy"]["stale"] is True
    assert rows["legacy"]["size_kb"] >= 1


def test_list_reports_missing_dir(tmp_path, monkeypatch) -> None:
    from lquant.server.api import factors as api

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path / "nope")
    assert api.list_reports() == []


def test_get_factor_reports_exact_match_only(tmp_path, monkeypatch) -> None:
    """BETA10 的报告面板不许挂上 BETA10_copy（另一个因子）。"""
    from lquant.server.api import factors as api

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    html = rep.factor_report(_panel(), "test_factor", display_name="f")
    (tmp_path / "BETA10.html").write_text(html, encoding="utf-8")
    (tmp_path / "BETA10_copy.html").write_text(html, encoding="utf-8")

    class _Con:
        def execute(self, *_a, **_k):
            return self

        def fetchone(self):
            return ("BETA10", "pct_change_20", "", "2026-01-01", "manual", "")

    class _Ctx:
        def __enter__(self):
            return _Con()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(api, "reader", lambda: _Ctx())
    out = api.get_factor("BETA10")
    assert [r["name"] for r in out["reports"]] == ["BETA10"]
    assert out["reports"][0]["generator_version"] == rep.REPORT_GENERATOR_VERSION
    assert out["reports"][0]["stale"] is False


# --------------------------------------------------------------------------- #
# API 端到端：生成的报告含新小节（身份 / 结论 / 容量 / 样本过滤 / 衰减阶梯）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factor_name", ["covidentity"])
def test_evaluate_report_contains_v2_sections(tmp_path, monkeypatch,
                                              factor_name) -> None:
    from lquant.server.api import factors as api
    from lquant.server.api.factors import EvaluateIn, _evaluate_full

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    req = EvaluateIn(factor=factor_name, formula="pct_change_5",
                     start="2026-04-01")
    metrics, _series = _evaluate_full(req)
    p = tmp_path / f"{factor_name}.html"
    assert p.exists()
    html = p.read_text(encoding="utf-8")

    assert f"因子研究报告 · {factor_name}" in html
    assert "样本与口径" in html
    assert "衰减阶梯" in html
    assert "1 日前瞻收益" in html
    # 结论节（评级）+ 容量小节 + 样本过滤披露
    assert "结论" in html
    assert metrics["rating"]["rating"] in ("weak", "moderate", "strong")
    assert "容量" in html
    assert "样本过滤" in html
    # 报告头部有版本与生成时间（陈旧判定依据）
    assert f'content="{rep.REPORT_GENERATOR_VERSION}"' in html
    assert 'name="lquant-report-generated-at"' in html
    # 回归：不许再按个股归因
    assert "归因分解 · 个股" not in html
    assert "归因分解 · symbol" not in html


def test_evaluate_report_failure_is_disclosed_not_silent(tmp_path, monkeypatch) -> None:
    """报告生成抛错：指标照常返回，但必须留痕（不许静默给个没有报告的 200）。"""
    from lquant.server.api import factors as api
    from lquant.server.api.factors import EvaluateIn, _evaluate_full

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("报告模板炸了")

    monkeypatch.setattr(rep, "factor_report", boom)
    metrics, series = _evaluate_full(
        EvaluateIn(factor="noreport", formula="pct_change_5", start="2026-04-01"))
    assert metrics["report_url"] is None
    assert "report" in metrics["errors"]
    assert "报告模板炸了" in metrics["errors"]["report"]
    assert "report" in series["errors"]
    assert not (tmp_path / "noreport.html").exists()
    # 其余指标不受影响
    assert metrics["n_samples"] > 0

