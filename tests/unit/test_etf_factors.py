"""ETF 因子：清单校验 / etf_meta 挂载 / 因子可算 / 协变量覆盖率。

全部离线：用自造的 ETF 日线帧 + 自造 etf_meta，不读湖、不读库。
共享夹具在 ``etf_helpers.py``（同目录普通模块，不走跨测试模块 import）。
"""
from __future__ import annotations

import math

import polars as pl
import pytest
from etf_helpers import ETF, TYPES, bars, etf_meta

_ETF = ETF
_TYPES = TYPES


# ---------------- 因子清单 ----------------

def test_load_etf_factors_all_pass_static_check():
    from lquant.factors.etf import load_etf_factors

    rows = load_etf_factors()
    assert len(rows) >= 12
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names))                 # 无重名
    for r in rows:
        assert r["source"] == "etf_yaml"
        assert r["expression"]
        assert isinstance(r["min_window"], int)


def test_etf_factors_cover_key_categories():
    """ETF 因子要覆盖动量/反转/波动/流动性/规模/费率等基本族。"""
    from lquant.factors.etf import load_etf_factors

    cats = {r["category"] for r in load_etf_factors()}
    for c in ("momentum", "reversal", "volatility", "liquidity", "size", "cost"):
        assert c in cats, c


def test_allowed_fields_includes_etf_meta_not_in_global():
    """ETF 字段白名单含 etf_meta 列，但不污染个股全局白名单。"""
    from lquant.factors.etf import ETF_EXTRA_FIELDS, etf_allowed_fields
    from lquant.factors.fields import daily_fields

    allowed = etf_allowed_fields()
    for f in ETF_EXTRA_FIELDS:
        assert f in allowed
        assert f not in daily_fields()        # 全局白名单不许出现 ETF 字段
    assert daily_fields() <= allowed          # 日线字段仍然可用


def test_bad_etf_field_rejected_with_hint(tmp_path):
    """引用不存在的 ETF 字段 → 静态期报错（不是算出全 null 静默污染）。"""
    from lquant.core.errors import FactorError
    from lquant.factors.etf import load_etf_factors

    p = tmp_path / "bad.yaml"
    p.write_text(
        "factors:\n  - name: bad\n    expr: \"$no_such_field\"\n    category: x\n",
        encoding="utf-8",
    )
    with pytest.raises(FactorError):
        load_etf_factors(p)


def test_preprocess_default_has_no_industry():
    """ETF 默认配方必须去掉 industry_sw1（ETF 无申万分类，留着就是假中性化）。"""
    from lquant.factors.etf import etf_preprocess_default

    steps = etf_preprocess_default()
    assert steps
    by = []
    for s in steps:
        raw = s.get("by") or s.get("factors") or []
        by += raw if isinstance(raw, list) else [raw]
    assert "industry_sw1" not in by
    assert "etf_fund_size" in by


# ---------------- etf_meta 挂载 ----------------

def test_attach_etf_meta_broadcasts_without_changing_rows():
    from lquant.factors.etf import attach_etf_meta

    df = bars()
    out = attach_etf_meta(df, etf_meta())
    assert out.height == df.height                    # 广播，不改行数
    for c in ("fund_type", "fund_size", "share_outstanding", "management_fee"):
        assert c in out.columns
    assert out["fund_type"].null_count() == 0


def test_attach_etf_meta_empty_meta_still_adds_columns():
    """etf_meta 空表也要补列 —— 否则下游字段解析/白名单会飘。"""
    from lquant.factors.etf import attach_etf_meta

    out = attach_etf_meta(bars(), pl.DataFrame(schema={"symbol": pl.Utf8}))
    for c in ("fund_type", "fund_size", "share_outstanding", "management_fee"):
        assert c in out.columns
    assert out["fund_size"].null_count() == out.height


def test_attach_etf_meta_unmatched_symbol_is_null():
    from lquant.factors.etf import attach_etf_meta

    df = bars(symbols=["510300.SH"])
    out = attach_etf_meta(df, etf_meta(symbols=["159915.SZ"], types=["equity"]))
    assert out["fund_size"].null_count() == out.height


# ---------------- 因子可算 ----------------

@pytest.mark.parametrize("name_idx", range(12))
def test_every_factor_computes_non_null(name_idx):
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.etf import attach_etf_meta, load_etf_factors

    factors = load_etf_factors()
    f = factors[name_idx]
    d = attach_etf_meta(bars(), etf_meta())
    out = compute_factor_col(d, f["expression"], "f")
    assert out["f"].is_not_null().sum() > 0, f"{f['name']} 全为 null"
    # 稳定性：末值必须是有限数（NaN/Inf 会污染 IC 且不报错）
    last = out["f"].to_list()[-1]
    assert last is not None and math.isfinite(last), f"{f['name']} 末值非有限: {last}"


def test_etf_size_factor_negative_log():
    """etf_size = -Log(fund_size)：规模越小因子值越大。"""
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.etf import attach_etf_meta

    d = attach_etf_meta(bars(symbols=["510300.SH", "518880.SH"]),
                        etf_meta(symbols=["510300.SH", "518880.SH"],
                                 types=["equity", "commodity"]))
    # 把 fund_size 改成一大一小，验证方向
    d = d.with_columns(pl.when(pl.col("symbol") == "510300.SH")
                       .then(pl.lit(300.0)).otherwise(pl.lit(5.0)).alias("fund_size"))
    out = compute_factor_col(d, "-Log($fund_size)", "f")
    last = (out.group_by("symbol").agg(pl.col("f").last())
            .sort("symbol").to_dicts())
    small = next(r["f"] for r in last if r["symbol"] == "518880.SH")
    big = next(r["f"] for r in last if r["symbol"] == "510300.SH")
    assert small > big


# ---------------- 协变量 ----------------

def test_build_covariates_etf_defaults():
    from lquant.factors.covariates import build_covariates
    from lquant.factors.etf import ETF_DEFAULT_COVS, attach_etf_meta

    d = attach_etf_meta(bars(), etf_meta())
    _, report = build_covariates(d, list(ETF_DEFAULT_COVS))
    cov = {r["covariate"]: r["coverage"] for r in report}
    assert cov.get("etf_fund_size", 0) > 0.9      # 有 fund_size → 覆盖率接近 1
    assert cov.get("turnover_1m", 0) > 0.9
    # 绝不出现个股那套（ETF 无 float_mv / 无行业分类）
    assert "market_cap" not in cov
    assert "industry_sw1" not in cov


def test_etf_fund_size_provider_unavailable_without_column():
    """没挂 etf_meta 的面板 → 覆盖率 0 并显式上报，绝不静默去均值。"""
    from lquant.factors.covariates import build_covariates

    plain = bars().drop("amount")     # 无 fund_size 列
    _, report = build_covariates(plain, ["etf_fund_size"])
    assert report[0]["coverage"] == 0.0
    assert report[0]["note"]          # 有原因说明，不是空串


def test_etf_fund_size_ignores_negative_values():
    """坏数据里负规模若直接 log1p 会得到 NaN 且 null_count 数不到（覆盖率虚高）。"""
    from lquant.factors.covariates import build_covariates
    from lquant.factors.etf import attach_etf_meta

    d = attach_etf_meta(bars(), etf_meta())
    d = d.with_columns(pl.when(pl.col("symbol") == "510300.SH")
                       .then(pl.lit(-5.0)).otherwise(pl.col("fund_size")).alias("fund_size"))
    out, report = build_covariates(d, ["etf_fund_size"])
    assert report[0]["coverage"] > 0.9                       # 夹到 0 后仍有值
    assert out["cov_etf_fund_size"].is_nan().sum() == 0      # 没有 NaN 混入
    assert out.filter(pl.col("symbol") == "510300.SH")["cov_etf_fund_size"][0] == 0.0
