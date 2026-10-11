"""ETF 面板构建与因子评价（离线）。

``read_daily`` / ``_read_etf_meta`` 全部打桩：断言的核心是
「ETF 池里不混入个股」与「协变量口径是 ETF 的那套，不是个股的」。
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from etf_helpers import ETF, bars, etf_meta


@pytest.fixture()
def patched(monkeypatch):
    """把湖读 + etf_meta 读都换掉：湖里故意混入个股，验证会被过滤掉。"""
    frame = pl.concat([
        bars(n_days=380, symbols=ETF),
        bars(n_days=380, symbols=["600000.SH"]),    # 个股混进湖里
    ])
    monkeypatch.setattr(
        "lquant.data.store.parquet.read_daily",
        lambda **kw: frame.lazy(),
    )
    monkeypatch.setattr("lquant.factors.etf._read_etf_meta", lambda: etf_meta())
    return frame


# ---------------- 面板 ----------------

def test_build_etf_panel_excludes_stocks(patched):
    from lquant.factors.etf import build_etf_panel

    df, cov_cols, info = build_etf_panel(universe="etf_all")
    syms = set(df["symbol"].unique().to_list())
    assert "600000.SH" not in syms                     # 个股必须被剔除
    assert syms == set(ETF)
    assert info["n_symbols"] == len(ETF)
    assert info["note"] is None


def test_build_etf_panel_attaches_fund_type_and_size(patched):
    from lquant.factors.etf import build_etf_panel

    df, _, _ = build_etf_panel(universe="etf_all")
    assert "fund_type" in df.columns and "fund_size" in df.columns
    assert df["fund_type"].null_count() == 0


def test_build_etf_panel_covs_are_etf_flavored(patched):
    from lquant.factors.etf import build_etf_panel

    _, cov_cols, info = build_etf_panel(universe="etf_all")
    assert "cov_etf_fund_size" in cov_cols
    assert "cov_market_cap" not in cov_cols            # ETF 无 float_mv，绝不挂
    assert "cov_industry_sw1" not in cov_cols
    assert info["covariates"]["etf_fund_size"] > 0.9


def test_attach_etf_meta_reads_repo_when_meta_omitted(monkeypatch):
    """不传 meta → 自己读 etf_meta（调用方不必先解析池）。"""
    from lquant.factors.etf import attach_etf_meta

    monkeypatch.setattr("lquant.factors.etf._read_etf_meta", lambda: etf_meta())
    out = attach_etf_meta(bars(n_days=5, symbols=["510300.SH"]))
    assert out["fund_type"].to_list() == ["equity"] * 5
    assert out["fund_size"][0] == pytest.approx(300.0)


def test_build_etf_panel_by_fund_type(patched):
    from lquant.factors.etf import build_etf_panel

    df, _, info = build_etf_panel(universe="etf_qdii")
    assert set(df["symbol"].unique().to_list()) == {"513050.SH", "513100.SH"}
    assert info["note"] is None


def test_build_etf_panel_empty_lake(monkeypatch):
    empty = pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date,
                                 "close": pl.Float64})
    monkeypatch.setattr("lquant.data.store.parquet.read_daily", lambda **kw: empty.lazy())
    monkeypatch.setattr("lquant.factors.etf._read_etf_meta", lambda: etf_meta())
    from lquant.factors.etf import build_etf_panel

    df, cov_cols, info = build_etf_panel(universe="etf_all")
    assert len(df) == 0
    assert cov_cols == []
    assert info["n_symbols"] == 0


# ---------------- 评价 ----------------

def test_evaluate_etf_runs_and_reports_etf_caliber(patched):
    from lquant.factors.etf import evaluate_etf

    res = evaluate_etf("Ts_Return($close, 20)", universe="etf_all", n_groups=5)
    assert res["rating"]["rating"] in ("weak", "moderate", "strong")
    # ETF 口径披露：池标签 + 无降级 + 协变量覆盖率
    assert res["etf"]["label"] == "全部ETF"
    assert res["etf"]["note"] is None
    assert "etf_fund_size" in res["etf"]["covariates"]
    assert "industry_sw1" not in res["etf"]["covariates"]
    # 归因/分组走 fund_type：暴露表的列名就是 cat_col（面板里根本没有行业列，
    # 列名叫 fund_type 才证明用的是基金类型而不是行业兜底）。
    attr = res["attribution"]
    assert attr.get("industry_exposure"), "分组暴露不应为空（应按 fund_type 分组）"
    assert "fund_type" in attr["industry_exposure"][0]
    # 结果必须可直接 JSON 序列化（CLI/API 共用）
    import json

    json.dumps(res, ensure_ascii=False)


def test_evaluate_etf_uses_etf_rating_thresholds(patched):
    """ETF 评级阈值来自 rating_etf.yaml（ic_moderate=0.015），不是个股的 0.02。"""
    from lquant.factors.etf import _etf_rating_thresholds

    th = _etf_rating_thresholds()
    assert th.ic_moderate == 0.015
    assert th.ic_strong == 0.025


def test_evaluate_etf_empty_panel_raises(monkeypatch):
    empty = pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date,
                                 "close": pl.Float64})
    monkeypatch.setattr("lquant.data.store.parquet.read_daily", lambda **kw: empty.lazy())
    monkeypatch.setattr("lquant.factors.etf._read_etf_meta", lambda: etf_meta())
    from lquant.factors.etf import evaluate_etf

    with pytest.raises(ValueError, match="ETF 面板为空"):
        evaluate_etf("Ts_Return($close, 20)", universe="etf_all")


def test_evaluate_etf_bad_expression_raises(patched):
    from lquant.core.errors import FactorError
    from lquant.factors.etf import evaluate_etf

    with pytest.raises((FactorError, ValueError)):
        evaluate_etf("$no_such_field", universe="etf_all")


# ---------------- ETF 份额协变量 ----------------

def test_etf_share_covariate_is_log1p(patched):
    """``etf_share`` 与 ``etf_fund_size`` 同一套实现（clip 下界 0 + log1p）。"""
    from lquant.factors.covariates import build_covariates

    frame = bars(n_days=60, symbols=ETF).join(etf_meta(), on="symbol", how="left")
    out, report = build_covariates(frame, ["etf_share"])
    assert "cov_etf_share" in out.columns
    assert report == [{"covariate": "etf_share", "coverage": 1.0, "note": ""}]
    assert out["cov_etf_share"].null_count() == 0


def test_etf_covariate_without_meta_column_is_declared():
    """没挂 etf_meta 列 → coverage=0 且写明原因，绝不静默当成中性化成功。"""
    from lquant.factors.covariates import build_covariates

    out, report = build_covariates(bars(n_days=30, symbols=["510300.SH"]), ["etf_share"])
    assert "cov_etf_share" not in out.columns
    assert report[0]["coverage"] == 0.0
    assert "share_outstanding" in report[0]["note"]


# ---------------- 出口归一（CLI / API 共用） ----------------

def test_jsonable_normalizes_polars_dates_and_unknown_objects():
    from lquant.factors.etf import _jsonable

    assert _jsonable(pl.DataFrame({"h": [1, 2]})) == [{"h": 1}, {"h": 2}]
    assert _jsonable(pl.Series("s", [1.5, 2.5])) == [1.5, 2.5]
    assert _jsonable(dt.date(2026, 1, 2)) == "2026-01-02"
    assert _jsonable(dt.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert _jsonable({"a": [float("nan"), 2]}) == {"a": [None, 2]}
    assert _jsonable((1, 2)) == [1, 2]
    assert _jsonable(True) is True
    assert _jsonable(None) is None
    assert _jsonable("x") == "x" and _jsonable(3) == 3
    # 不可转 float 的对象退化为 str：宁可给个可读字符串，也不让出口炸
    odd = object()
    assert _jsonable(odd) == str(odd)


def test_etf_rating_thresholds_explicit_path():
    """显式 path 分支：不再走 get_settings().config_dir 的默认解析。"""
    from pathlib import Path

    from lquant.factors.etf import _etf_rating_thresholds

    path = Path(__file__).resolve().parents[2] / "config" / "factors" / "rating_etf.yaml"
    th = _etf_rating_thresholds(path)
    assert th.ic_moderate == 0.015 and th.ic_strong == 0.025


# ---------------- train 段为空 / HTML 报告落盘 ----------------

def test_evaluate_etf_empty_train_segment_raises(monkeypatch):
    """样本太短（或表达式全 null）→ 明确报错，而不是生成一份空报告。"""
    from lquant.factors.etf import evaluate_etf

    panel = pl.DataFrame({"symbol": ["510300.SH"], "trade_date": [dt.date(2026, 1, 5)],
                          "close": [1.0]})
    monkeypatch.setattr("lquant.factors.etf.build_etf_panel",
                        lambda **kw: (panel, [], {"note": None, "n_symbols": 1,
                                                  "covariates": {}}))
    monkeypatch.setattr("lquant.factors.mining.runner.split_dates",
                        lambda dates, **kw: ([], [], []))
    monkeypatch.setattr("lquant.factors.mining.submit.prepare_segment",
                        lambda *a, **k: pl.DataFrame())

    with pytest.raises(ValueError, match="ETF train 段为空"):
        evaluate_etf("Ts_Return($close, 20)", universe="etf_all")


def test_evaluate_etf_saves_html_report(monkeypatch, tmp_path):
    """with_report + out → 真落盘 HTML，且 report_path / report 都回传。"""
    from lquant.factors.etf import evaluate_etf

    panel = pl.DataFrame({
        "symbol": ["510300.SH"] * 3,
        "trade_date": [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)],
        "close": [1.0, 1.1, 1.2],
    })
    monkeypatch.setattr("lquant.factors.etf.build_etf_panel",
                        lambda **kw: (panel, ["cov_etf_fund_size"],
                                      {"note": None, "n_symbols": 1,
                                       "covariates": {"etf_fund_size": 1.0}}))
    monkeypatch.setattr("lquant.factors.mining.runner.split_dates",
                        lambda dates, **kw: (dates[:2], [], []))
    monkeypatch.setattr("lquant.factors.mining.submit.prepare_segment",
                        lambda *a, **k: panel)
    monkeypatch.setattr("lquant.factors.evaluate.evaluate",
                        lambda *a, **k: {"report": "<html>ETF 报告</html>",
                                         "rating": {"rating": "strong"},
                                         "decay": {"profile": pl.DataFrame({"h": [1]})}})

    out = tmp_path / "etf_report.html"
    res = evaluate_etf("Ts_Return($close, 20)", universe="etf_all",
                       with_report=True, out=out)

    assert out.read_text(encoding="utf-8") == "<html>ETF 报告</html>"
    assert res["report_path"] == str(out)
    assert res["report"] == "<html>ETF 报告</html>"       # HTML 原样，不进 _jsonable
    assert res["decay"]["profile"] == [{"h": 1}]          # 其余仍是 JSON 原生
    assert res["etf"]["preprocess"]                       # 预处理配方随报告披露
