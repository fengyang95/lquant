"""报告链路的**失败与降级分支**：算炸了要留痕、缺数据要降级，而不是抛或静默。

对应 pre-push 的改动行覆盖率门禁：这些分支平时跑不到，但正是「静默删节」
（评审 R7）最容易复发的地方 —— 覆盖它们等于把「失败可见」钉在测试里。
"""
from __future__ import annotations

import math
import os
from datetime import date, timedelta

import polars as pl
import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")

from lquant.factors.evaluate import capacity as cap  # noqa: E402
from lquant.factors.evaluate import extras as ex  # noqa: E402


def _panel(n_days: int = 30, n_sym: int = 12, *, flat: bool = False,
           null_factor: bool = False, size_col: bool = True) -> pl.DataFrame:
    rows = []
    base = date(2026, 1, 5)
    for i in range(n_days):
        d = base + timedelta(days=i)
        for j in range(n_sym):
            f = 1.0 if flat else 0.02 * j + 0.3 * math.sin(i * 0.7 + j * 1.3)
            rows.append({
                "trade_date": d,
                "symbol": f"{600000 + j:06d}.SH",
                "close": 10.0 + 0.1 * j,
                "amount": 1e7 + 1e6 * j,
                "f": None if null_factor else f,
                "_factor": None if null_factor else f,
                "fwd_ret_1": 0.001 * (j + 1),
            })
    df = pl.DataFrame(rows)
    if null_factor:
        df = df.with_columns(pl.col("f").cast(pl.Float64),
                             pl.col("_factor").cast(pl.Float64))
    if size_col:
        df = df.with_columns(pl.col("amount").log1p().alias("cov_market_cap"))
    return df


# --------------------------------------------------------------------------- #
# capacity：不可估算时记 NaN，而不是编一个数
# --------------------------------------------------------------------------- #
def test_portfolio_adv_returns_nan_when_no_legs() -> None:
    """因子全 null → 分不出多空两端 → ADV 不可算。"""
    assert math.isnan(cap.portfolio_adv(_panel(null_factor=True), "f", 5))


def test_capacity_summary_untradable_marks_everything_nan() -> None:
    """换手为 0（常数因子）→ 容量无法评估：全部记 NaN，viable=False。"""
    out = cap.capacity_summary(_panel(flat=True), "f", "fwd_ret_1", n_groups=5,
                               aum_list=[1e8])
    assert math.isnan(out["capacity_aum"])
    row = out["rows"][0]
    assert math.isnan(row["participation"]) and math.isnan(row["impact_bps"])
    assert math.isnan(row["net_annual"]) and row["viable"] is False


# --------------------------------------------------------------------------- #
# extras：分段失败留痕
# --------------------------------------------------------------------------- #
def test_r_rounding_guards() -> None:
    assert ex._r(None) is None
    assert ex._r("not-a-number") is None          # TypeError/ValueError → None
    assert ex._r(float("nan")) is None
    assert ex._r(1.23456, 3) == 1.235


def test_neutral_ladder_covariate_failure_is_recorded(monkeypatch) -> None:
    """协变量建不出来 → 阶梯为空 + 记错误键（不抛）。"""
    import lquant.factors.covariates as cov_mod

    def boom(*_a, **_k):
        raise RuntimeError("covariates down")

    monkeypatch.setattr(cov_mod, "build_covariates", boom)
    errors: dict[str, str] = {}
    assert ex.neutral_ladder(_panel(), "f", "fwd_ret_1", errors=errors) == []
    assert "neutral_ladder:covariates" in errors
    assert "covariates down" in errors["neutral_ladder:covariates"]


def test_neutral_views_failure_is_recorded(monkeypatch) -> None:
    import lquant.factors.evaluate.neutral_views as nv_mod

    def boom(*_a, **_k):
        raise RuntimeError("views down")

    monkeypatch.setattr(nv_mod, "neutral_views", boom)
    errors: dict[str, str] = {}
    out = ex.neutral_views_for(_panel(), "fwd_ret_1", factor="f", errors=errors)
    assert out["view"] == "error" and "views down" in out["error"]
    assert "neutral_views" in errors


def test_neutral_views_without_covariates_is_disclosed() -> None:
    out = ex.neutral_views_for(_panel(size_col=False), "fwd_ret_1", factor="f")
    assert out["view"].startswith("raw")


def test_size_group_ic_without_size_column() -> None:
    # 三个候选列（cov_market_cap / float_mv / amount）都没有 → 不分组、不报错
    df = _panel(size_col=False).drop("amount")
    assert ex.size_group_ic(df, "_factor", "fwd_ret_1") == {"size_col": None, "rows": []}


def test_industry_group_ic_empty_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr("lquant.factors.evaluate.group_ic.ic_by_group",
                        lambda *a, **k: pl.DataFrame())
    assert ex.industry_group_ic(_panel(), "_factor", "fwd_ret_1", "cov_market_cap") == []


def test_build_report_extras_records_industry_ic_failure(monkeypatch) -> None:
    """行业分组 IC 抛错 → 记错误键、其余内容块照常。"""
    def boom(*_a, **_k):
        raise RuntimeError("group ic down")

    monkeypatch.setattr("lquant.factors.evaluate.group_ic.ic_by_group", boom)
    errors: dict[str, str] = {}
    out = ex.build_report_extras(_panel(), "_factor", "fwd_ret_1", n_groups=5,
                                 group_col="cov_market_cap", pre_recipe_df=_panel(),
                                 errors=errors)
    assert out["excess"]["dates"]
    assert "group_ic" in errors and "group ic down" in errors["group_ic"]


# --------------------------------------------------------------------------- #
# meta：元信息缺失绝不阻断评价
# --------------------------------------------------------------------------- #
def test_factor_description_guards(monkeypatch) -> None:
    from lquant.factors import meta

    assert meta.factor_description(None) == ""
    assert meta.factor_description("") == ""

    class _Con:
        def execute(self, *_a, **_k):
            raise RuntimeError("db down")

    class _Ctx:
        def __enter__(self):
            return _Con()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr("lquant.core.db.reader", lambda: _Ctx())
    assert meta.factor_description("mom20") == ""


# --------------------------------------------------------------------------- #
# API：样本过滤 / 容量失败 / 合成降级
# --------------------------------------------------------------------------- #
def test_evaluate_sample_filter_paths(monkeypatch) -> None:
    from fastapi import HTTPException

    from lquant.server.api.factors import EvaluateIn, _evaluate_full

    # 真正剔除 ST（走 apply_sample_filters 分支）
    metrics, _series = _evaluate_full(EvaluateIn(
        factor="errpaths_st", formula="pct_change_5", start="2026-04-01",
        exclude_st=True))
    assert metrics["n_samples"] > 0

    # 剔除后为空 → 422 而不是生成一份空报告
    import lquant.factors.evaluate.sample as sample_mod

    monkeypatch.setattr(sample_mod, "apply_sample_filters",
                        lambda df, **_k: df.head(0))
    with pytest.raises(HTTPException) as ei:
        _evaluate_full(EvaluateIn(factor="errpaths_empty", formula="pct_change_5",
                                  start="2026-04-01", exclude_st=True))
    assert ei.value.status_code == 422


def test_evaluate_capacity_failure_is_recorded(monkeypatch) -> None:
    import lquant.factors.evaluate.capacity as cap_mod

    def boom(*_a, **_k):
        raise RuntimeError("capacity down")

    monkeypatch.setattr(cap_mod, "capacity_summary", boom)
    from lquant.server.api.factors import EvaluateIn, _evaluate_full

    metrics, _series = _evaluate_full(EvaluateIn(
        factor="errpaths_cap", formula="pct_change_5", start="2026-04-01"))
    assert metrics["capacity"] is None
    assert "capacity" in metrics["errors"]
    assert "capacity down" in metrics["errors"]["capacity"]


def _synthesize(monkeypatch, *, break_industry=False, break_cov=False,
                break_capacity=False):
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    if break_industry:
        import lquant.server.api.factors as api

        class _Boom:
            def execute(self, *_a, **_k):
                raise RuntimeError("industry read down")

        class _Ctx:
            def __enter__(self):
                return _Boom()

            def __exit__(self, *_a):
                return False

        monkeypatch.setattr(api, "reader", lambda: _Ctx())
    if break_cov:
        import lquant.factors.covariates as cov_mod

        def _boom(*_a, **_k):
            raise RuntimeError("cov down")

        monkeypatch.setattr(cov_mod, "build_covariates", _boom)
    if break_capacity:
        import lquant.factors.evaluate.capacity as cap_mod

        def _boom(*_a, **_k):
            raise RuntimeError("cap down")

        monkeypatch.setattr(cap_mod, "capacity_summary", _boom)

    with TestClient(create_app()) as c:
        return c.post("/api/factors/synthesize", json={
            "formulas": ["pct_change_5", "pct_change_20"], "method": "equal",
            "n_groups": 10, "start": "2026-04-01"})


def test_synthesize_degrades_when_industry_read_fails(monkeypatch) -> None:
    r = _synthesize(monkeypatch, break_industry=True)
    assert r.status_code == 200, r.text
    # 行业列缺失 → 归因/分组 IC 不出现，但报告照常生成
    from lquant.server.api import factors as api

    html = (api.REPORT_DIR / "syn_2f_eq.html").read_text(encoding="utf-8")
    assert "核心指标" in html


def test_synthesize_degrades_when_covariates_fail(monkeypatch) -> None:
    r = _synthesize(monkeypatch, break_cov=True)
    assert r.status_code == 200, r.text


def test_synthesize_records_capacity_failure(monkeypatch) -> None:
    r = _synthesize(monkeypatch, break_capacity=True)
    assert r.status_code == 200, r.text
    from lquant.server.api import factors as api

    html = (api.REPORT_DIR / "syn_2f_eq.html").read_text(encoding="utf-8")
    assert "本节生成失败" in html and "cap down" in html
