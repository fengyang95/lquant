"""``lq etf`` CLI：JSON 出口纪律（NaN/日期收口）+ 失败路径 → ClickException。

全部打桩：命令体只做「取数 → 归一 → 打印」，真正的计算在 factors/backtest 层。
这里验证的是**出口契约** —— stdout 必须是严格合法 JSON（裸 NaN/date 都会让
管道解析端炸），失败必须非 0 退出且提示可读。
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lquant.cli.commands.etf import _clean, etf


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _json_out(res) -> object:
    assert res.exit_code == 0, res.output
    return json.loads(res.output)          # 裸 NaN/date 在这里就会炸


# ---------------- _clean：NaN / 日期 / 未知对象 ----------------

def test_clean_collapses_nan_and_dates():
    class _Odd:
        def __str__(self) -> str:      # 未知对象 → str()，不静默丢字段
            return "odd"

    assert _clean(float("nan")) is None
    assert _clean(float("inf")) is None
    assert _clean(float("-inf")) is None
    assert _clean(1.5) == 1.5
    assert _clean(dt.date(2026, 1, 2)) == "2026-01-02"
    assert _clean(dt.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert _clean({"a": (float("nan"), 1), "b": [{"c": dt.date(2026, 1, 1)}]}) == {
        "a": [None, 1], "b": [{"c": "2026-01-01"}]}
    assert _clean("s") == "s"
    assert _clean(3) == 3
    assert _clean(True) is True
    assert _clean(None) is None
    assert _clean(_Odd()) == "odd"


# ---------------- universes / factors ----------------

def test_universes_prints_json(runner, monkeypatch):
    monkeypatch.setattr(
        "lquant.factors.etf.etf_universe_options",
        lambda: [("etf_all", None, "全部ETF"), ("etf_equity", "equity", "股票ETF")],
    )
    out = _json_out(runner.invoke(etf, ["universes"]))
    assert out == [
        {"key": "etf_all", "fund_type": None, "label": "全部ETF"},
        {"key": "etf_equity", "fund_type": "equity", "label": "股票ETF"},
    ]


def test_factors_prints_json(runner, monkeypatch):
    monkeypatch.setattr(
        "lquant.factors.etf.load_etf_factors",
        lambda: [{"name": "etf_mom_20", "expression": "Ts_Return($close,20)",
                  "category": "momentum", "description": "20日动量", "min_window": 20,
                  "source": "etf_yaml", "factor_id": None}],
    )
    out = _json_out(runner.invoke(etf, ["factors"]))
    assert out[0]["name"] == "etf_mom_20"
    assert out[0]["min_window"] == 20
    assert "source" not in out[0]          # 只出 CLI 承诺的字段


# ---------------- eval / audit ----------------

def test_eval_pops_report_and_prints_json(runner, monkeypatch):
    seen = {}

    def fake(expr, **kw):
        seen.update({"expr": expr, **kw})
        return {"rating": {"rating": "strong"}, "report": "<html>", "ic": float("nan")}

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", fake)
    out = _json_out(runner.invoke(etf, ["eval", "Ts_Return($close,20)"]))
    assert out["rating"]["rating"] == "strong"
    assert out["ic"] is None               # NaN → null，JSON 仍合法
    assert "report" not in out             # HTML 不进 CLI 出口
    assert seen["with_report"] is False
    assert "with_robustness" not in seen    # eval 不开 L3，audit 才开


def test_eval_value_error_is_click_exception(runner, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("ETF 面板为空（池=etf_all）")

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", boom)
    res = runner.invoke(etf, ["eval", "Ts_Return($close,20)"])
    assert res.exit_code == 1
    assert "ETF 面板为空" in res.output


def test_audit_value_error_is_click_exception(runner, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("ETF train 段为空 —— 数据太短或表达式全 null")

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", boom)
    res = runner.invoke(etf, ["audit", "Ts_Return($close,20)"])
    assert res.exit_code == 1
    assert "ETF train 段为空" in res.output


def test_audit_enables_robustness(runner, monkeypatch):
    seen = {}

    def fake(expr, **kw):
        seen.update(kw)
        return {"rating": {"rating": "weak"}, "report": "<html>"}

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", fake)
    out = _json_out(runner.invoke(
        etf, ["audit", "Ts_Return($close,20)", "--universe", "etf_qdii", "--n-groups", "3"]))
    assert out["rating"]["rating"] == "weak"
    assert seen["with_robustness"] is True
    assert seen["universe"] == "etf_qdii" and seen["n_groups"] == 3


# ---------------- report ----------------

def test_report_with_explicit_out(runner, monkeypatch, tmp_path):
    out_file = tmp_path / "r.html"
    seen = {}

    def fake(expr, **kw):
        seen.update(kw)
        return {"report_path": str(out_file), "rating": {"rating": "moderate"},
                "etf": {"label": "全部ETF"}, "report": "<html>"}

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", fake)
    out = _json_out(runner.invoke(etf, ["report", "Ts_Return($close,20)", "--out", str(out_file)]))
    assert out["report"] == str(out_file)
    assert out["rating"] == "moderate"
    assert out["etf"] == {"label": "全部ETF"}
    assert seen["with_report"] is True
    assert str(seen["out"]) == str(out_file)


def test_report_default_path_lands_under_reports_dir(runner, monkeypatch):
    seen = {}

    def fake(expr, **kw):
        seen.update(kw)
        return {"rating": {}, "etf": {}}

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", fake)
    out = _json_out(runner.invoke(etf, ["report", "Ts_Return($close,20)"]))
    path = str(seen["out"])
    assert path.endswith(".html") and "etf_factor_" in path
    assert out["report"] is None           # 打桩没给 report_path → null，而不是缺字段


def test_report_value_error_is_click_exception(runner, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("ETF 面板为空")

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", boom)
    res = runner.invoke(etf, ["report", "Ts_Return($close,20)"])
    assert res.exit_code == 1
    assert "ETF 面板为空" in res.output


# ---------------- backtest ----------------

def test_backtest_prints_summary(runner, monkeypatch):
    calls = {}

    def fake(formula, **kw):
        calls.update({"formula": formula, **kw})
        res = SimpleNamespace(metrics={"n_trades": 2, "sharpe": float("nan")}, nav=[1.0, 1.1])
        return res, {"n_symbols": 3}

    monkeypatch.setattr("lquant.backtest.etf.etf_factor_backtest", fake)
    out = _json_out(runner.invoke(etf, [
        "backtest", "--factor", "Ts_Return($close,20)", "--top-n", "3",
        "--rebalance", "weekly", "--exit-strategy", "simple"]))
    assert out["n_symbols"] == 3
    assert out["n_nav_points"] == 2
    assert out["n_trades"] == 2
    assert out["metrics"]["sharpe"] is None
    assert calls["exit_strategy"] == "simple"
    assert calls["top_n"] == 3 and calls["rebalance"] == "weekly"


def test_backtest_value_error_is_click_exception(runner, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("ETF 面板为空 —— 先同步 ETF 日线与元数据")

    monkeypatch.setattr("lquant.backtest.etf.etf_factor_backtest", boom)
    res = runner.invoke(etf, ["backtest", "--factor", "Ts_Return($close,20)"])
    assert res.exit_code == 1
    assert "ETF 面板为空" in res.output


def test_backtest_unknown_exit_strategy_is_click_exception(runner, monkeypatch):
    def boom(*_a, **_k):
        raise KeyError("nope")

    monkeypatch.setattr("lquant.backtest.etf.etf_factor_backtest", boom)
    res = runner.invoke(etf, ["backtest", "--factor", "Ts_Return($close,20)",
                              "--exit-strategy", "nope"])
    assert res.exit_code == 1
    assert "未注册的退出策略" in res.output


def test_etf_cli_group_is_registered():
    """``lq etf`` 挂在主 CLI 上，且子命令齐全。"""
    from lquant.cli.main import cli

    assert "etf" in cli.commands
    assert set(etf.commands) == {"universes", "factors", "eval", "audit", "report", "backtest"}
    assert "etf" in CliRunner().invoke(cli, ["--help"]).output
