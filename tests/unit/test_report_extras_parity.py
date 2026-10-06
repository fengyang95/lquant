"""内容块（extras）的入口一致性：三个入口必须产出**同一套**报告内容。

评审 R12 的原话是「同一个生成器，三个入口产出三种不同的报告」。修法分两步：
编排抽到 ``evaluate/extras.py``，再由三个入口共用。本文件守住第二半 ——
抽出来了但没接线，等于没修。
"""
from __future__ import annotations

import math
import os
import re
from datetime import date, timedelta

import polars as pl
import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")

from lquant.factors.evaluate import extras as ex  # noqa: E402

#: 三个入口都必须出现的小节（条件小节的触发条件在 extras 已齐备时必然成立）。
REQUIRED_SECTIONS = (
    "结论",
    "样本与口径",
    "核心指标",
    "累计 IC",
    "滚动窗口",
    "分层收益",
    "超额收益",
    "Top-N 持仓收缩",
    "事件式分层收益",
    "IC 衰减",
    "分年度 IC",
    "IC 归因阶梯",
    "中性化视图",
    "归因分解",
    "分组 IC",
    "风格相关性体检",
    "换手率",
    "成本敏感性",
    "容量与流动性",
)


def _panel(n_days: int = 90, n_sym: int = 30) -> pl.DataFrame:
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
                "cov_market_cap": math.log1p(1e9 + 1e8 * j),
                "cov_industry_sw1": f"I{j % 3}",
                "cov_turnover_1m": 0.5 + 0.01 * j,
                "_factor": 0.02 * j + 0.3 * math.sin(i * 0.7 + j * 1.3),
                "fwd_ret_1": 0.001 * (j + 1) + 0.0005 * i,
                "fwd_ret_5": 0.002 * (j + 1),
            })
    return pl.DataFrame(rows)


def _sections(html: str) -> set[str]:
    return {re.sub(r"（.*", "", h).split(" · ")[0].strip()
            for h in re.findall(r"<h2>(.*?)</h2>", html)}


# --------------------------------------------------------------------------- #
# 编排本身
# --------------------------------------------------------------------------- #
def test_build_report_extras_returns_all_report_blocks() -> None:
    errors: dict[str, str] = {}
    out = ex.build_report_extras(_panel(), "_factor", "fwd_ret_1", n_groups=5,
                                 top_ns=[10, 20], pre_recipe_df=_panel(),
                                 cov_report={"market_cap": 1.0, "industry_sw1": 1.0,
                                             "turnover_1m": 1.0},
                                 errors=errors)
    for key in ("excess", "top_n", "style_corr", "neutral_ladder",
                "neutral_views", "group_ic_size", "group_ic"):
        assert key in out, key
    # 归因阶梯四级：raw → +market_cap → +industry → +turnover
    assert [r["label"] for r in out["neutral_ladder"]] == [
        "raw", "+market_cap", "+industry", "+turnover"]
    assert out["excess"]["dates"] and out["excess"]["curves"]
    assert out["group_ic_size"]["size_col"] == "cov_market_cap"
    assert errors == {}, errors


def test_build_report_extras_records_failures_instead_of_raising(monkeypatch) -> None:
    """任何一段炸了都要留痕（键名与前端故障标签表对应），不许静默、也不许整批失败。"""
    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr("lquant.factors.evaluate.top_n.top_n_summary", boom)
    monkeypatch.setattr("lquant.factors.evaluate.style_corr.style_correlation", boom)
    errors: dict[str, str] = {}
    out = ex.build_report_extras(_panel(), "_factor", "fwd_ret_1", n_groups=5,
                                 pre_recipe_df=None, errors=errors)
    assert out["top_n"] == []
    assert out["style_corr"] == {}
    assert "top_n" in errors and "down" in errors["top_n"]
    assert "style_corr" in errors and "down" in errors["style_corr"]
    # 其它段照常产出（一段炸不拖垮整份报告）
    assert out["excess"]["dates"]


def test_size_group_ic_records_error_when_grouping_fails(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("size down")

    monkeypatch.setattr("lquant.factors.evaluate.group_ic.size_group", boom)
    errors: dict[str, str] = {}
    out = ex.size_group_ic(_panel(), "_factor", "fwd_ret_1", errors=errors)
    assert out["rows"] == []
    assert "group_ic" in errors and "size down" in errors["group_ic"]


def test_neutral_views_for_uses_given_factor_column() -> None:
    """CLI 的因子列叫 f、API 叫 _factor —— 写死一个名字就会在另一个入口炸。"""
    errors: dict[str, str] = {}
    d = _panel(n_days=40, n_sym=20).rename({"_factor": "f"})
    out = ex.neutral_views_for(d, "fwd_ret_1", factor="f", n_groups=5, errors=errors)
    assert "error" not in out or out.get("view") != "error", out
    assert errors == {}, errors


# --------------------------------------------------------------------------- #
# 端到端：API 评价 vs 合成
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entry", ["evaluate", "synthesize"])
def test_each_entry_report_has_all_sections(entry) -> None:
    from fastapi.testclient import TestClient

    from lquant.server.api.factors import EvaluateIn, _evaluate_full
    from lquant.server.main import create_app

    if entry == "evaluate":
        _metrics, _series = _evaluate_full(EvaluateIn(
            factor="sec_eval", formula="pct_change_20", start="2026-04-01"))
        from lquant.server.api import factors as api

        html = (api.REPORT_DIR / "sec_eval.html").read_text(encoding="utf-8")
    else:
        with TestClient(create_app()) as c:
            r = c.post("/api/factors/synthesize", json={
                "formulas": ["pct_change_5", "pct_change_20"], "method": "equal",
                "n_groups": 10, "start": "2026-04-01"})
            assert r.status_code == 200, r.text
        from lquant.server.api import factors as api

        html = (api.REPORT_DIR / "syn_2f_eq.html").read_text(encoding="utf-8")

    got = _sections(html)
    missing = [s for s in REQUIRED_SECTIONS if not any(s in g for g in got)]
    assert not missing, f"{entry} 报告缺小节: {missing}（实得: {sorted(got)}）"
    # 调用方的错误通道接到了报告里（有失败必须可见）
    assert "本节生成失败" in html or "errors" not in html


# --------------------------------------------------------------------------- #
# CLI 接线（打桩，快）
# --------------------------------------------------------------------------- #
def test_cli_build_report_forwards_extras_and_errors(monkeypatch, tmp_path) -> None:
    """CLI 必须把 extras 与 errors 都交给 factor_report —— 否则报告又变薄。"""
    import lquant.factors.evaluate as ev_pkg
    import lquant.factors.evaluate.capacity as cap_mod
    import lquant.factors.evaluate.extras as extras_mod
    import lquant.factors.evaluate.rating as rating_mod
    import lquant.factors.evaluate.report as report_mod
    from lquant.cli.commands import factor as cli_factor

    frame = pl.DataFrame({
        "trade_date": [date(2026, 1, 5)] * 3,
        "symbol": ["a", "b", "c"],
        "f": [1.0, 2.0, 3.0],
        "fwd_ret_1": [0.01, 0.02, 0.03],
        "cov_market_cap": [1.0, 2.0, 3.0],
    })
    monkeypatch.setattr(cli_factor, "_load_segments",
                        lambda start, expr, **kw: ({"train": frame}, ["cov_market_cap"],
                                                   {}, frame))
    sentinel_extras = {"excess": {"dates": []}, "top_n": [], "style_corr": {},
                       "neutral_ladder": [], "neutral_views": {}, "group_ic_size": {},
                       "group_ic": {}}
    captured: dict = {}

    def _fake_extras(df, factor, ret_col, **kw):
        captured["extras_kw"] = kw
        return dict(sentinel_extras)

    monkeypatch.setattr(extras_mod, "build_report_extras", _fake_extras)
    monkeypatch.setattr(rating_mod, "factor_rating",
                        lambda ic, qs: {"rating": "weak"})
    monkeypatch.setattr(cap_mod, "capacity_summary",
                        lambda *a, **k: {"capacity_aum": 1.0})

    def _fake_report(df, factor, ret_col="fwd_ret_1", **kw):
        captured["report_kw"] = kw
        return "<html>stub</html>"

    monkeypatch.setattr(report_mod, "factor_report", _fake_report)
    monkeypatch.setattr(ev_pkg, "factor_report", _fake_report)

    out = tmp_path / "r.html"
    info = cli_factor._build_report("Ts_Mean($close,5)", out=str(out))
    assert info["sections"] == sorted(sentinel_extras)
    # extras 全量传入（+ capacity）
    assert captured["report_kw"]["extras"]["excess"] == sentinel_extras["excess"]
    assert "capacity" in captured["report_kw"]["extras"]
    # errors 通道也传了
    assert "errors" in captured["report_kw"]
    # 归因阶梯拿到了「中性化之前」的帧
    assert captured["extras_kw"]["pre_recipe_df"] is frame


def _stub_cli_report(monkeypatch, frame, *, rating=None, capacity=None,
                     rating_raises=False, capacity_raises=False,
                     apply_filters=None, settings=None):
    """把 `_build_report` 的依赖全部打桩，返回 (cli_factor, captured)。"""
    import lquant.factors.evaluate as ev_pkg
    import lquant.factors.evaluate.capacity as cap_mod
    import lquant.factors.evaluate.extras as extras_mod
    import lquant.factors.evaluate.rating as rating_mod
    import lquant.factors.evaluate.report as report_mod
    import lquant.factors.evaluate.sample as sample_mod
    from lquant.cli.commands import factor as cli_factor

    monkeypatch.setattr(cli_factor, "_load_segments",
                        lambda start, expr, **kw: ({"train": frame}, ["cov_market_cap"],
                                                   {}, frame))
    monkeypatch.setattr(extras_mod, "build_report_extras",
                        lambda df, factor, ret_col, **kw: {"excess": {"dates": []}})
    if rating_raises:
        def _rboom(*_a, **_k):
            raise RuntimeError("rating down")
        monkeypatch.setattr(rating_mod, "factor_rating", _rboom)
    else:
        monkeypatch.setattr(rating_mod, "factor_rating",
                            lambda ic, qs: rating or {"rating": "weak"})
    if capacity_raises:
        def _cboom(*_a, **_k):
            raise RuntimeError("capacity down")
        monkeypatch.setattr(cap_mod, "capacity_summary", _cboom)
    else:
        monkeypatch.setattr(cap_mod, "capacity_summary",
                            lambda *a, **k: capacity or {"capacity_aum": 1.0})
    if apply_filters is not None:
        monkeypatch.setattr(sample_mod, "apply_sample_filters", apply_filters)
    if settings is not None:
        monkeypatch.setattr("lquant.core.config.get_settings", lambda: settings)

    captured: dict = {}

    def _fake_report(df, factor, ret_col="fwd_ret_1", **kw):
        captured["report_kw"] = kw
        return "<html>stub</html>"

    monkeypatch.setattr(report_mod, "factor_report", _fake_report)
    monkeypatch.setattr(ev_pkg, "factor_report", _fake_report)
    return cli_factor, captured


def _frame(**extra):
    base = {
        "trade_date": [date(2026, 1, 5)] * 3,
        "symbol": ["a", "b", "c"],
        "f": [1.0, 2.0, 3.0],
        "fwd_ret_1": [0.01, 0.02, 0.03],
        "cov_market_cap": [1.0, 2.0, 3.0],
    }
    base.update(extra)
    return pl.DataFrame(base)


def test_cli_report_applies_sample_filter(monkeypatch, tmp_path) -> None:
    """--exclude-st 走真正的过滤路径，并把开关状态写进报告披露。"""
    calls = []

    def _fake_filter(df, *, exclude_st=False, exclude_suspended=False):
        calls.append((exclude_st, exclude_suspended))
        return df

    cli_factor, captured = _stub_cli_report(
        monkeypatch, _frame(is_st=[False, True, False]),
        apply_filters=_fake_filter)
    info = cli_factor._build_report("Ts_Mean($close,5)", out=str(tmp_path / "r.html"),
                                    exclude_st=True)
    assert calls == [(True, False)]
    assert info["exclude_st"] is True
    assert captured["report_kw"]["sample_filters"]  # 披露行存在


def test_cli_report_rejects_empty_sample(monkeypatch, tmp_path) -> None:
    """过滤后一行不剩 → ClickException，而不是生成一份空报告。"""
    import click

    cli_factor, _ = _stub_cli_report(
        monkeypatch, _frame(is_st=[True, True, True]),
        apply_filters=lambda df, **_k: df.head(0))
    with pytest.raises(click.ClickException) as ei:
        cli_factor._build_report("Ts(M)", out=str(tmp_path / "r.html"), exclude_st=True)
    assert "样本过滤后没有剩余数据" in str(ei.value)


def test_cli_report_records_rating_and_capacity_failures(monkeypatch, tmp_path) -> None:
    cli_factor, captured = _stub_cli_report(
        monkeypatch, _frame(), rating_raises=True, capacity_raises=True)
    info = cli_factor._build_report("Ts_Mean($close,5)", out=str(tmp_path / "r.html"))
    assert info["rating"] is None and info["capacity_aum"] is None
    errs = captured["report_kw"]["errors"]
    assert "rating down" in errs["rating"] and "capacity down" in errs["capacity"]


def test_cli_report_default_output_path(monkeypatch, tmp_path) -> None:
    """不传 --out：落到 Settings.reports_dir（相对路径锚定仓库根）。"""
    from types import SimpleNamespace

    s = SimpleNamespace(root=tmp_path, reports_dir="./data/reports")
    cli_factor, _ = _stub_cli_report(monkeypatch, _frame(), settings=s)
    info = cli_factor._build_report("Ts_Mean($close,5)")
    assert info["report"].startswith(str(tmp_path / "data" / "reports"))
    assert info["report"].endswith(".html")
    assert info["factor_id"] in info["report"]
