"""ETF 面板构建与因子评价（离线）。

``read_daily`` / ``_read_etf_meta`` 全部打桩：断言的核心是
「ETF 池里不混入个股」与「协变量口径是 ETF 的那套，不是个股的」。
"""
from __future__ import annotations

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
