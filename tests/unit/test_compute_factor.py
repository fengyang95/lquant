"""缺陷 #1：内置因子探测改白名单查询，缺列错误不被吞成 422。"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import HTTPException


def _panel() -> pl.DataFrame:
    rows = []
    for sym in ("A", "B"):
        px = 10.0
        for i in range(30):
            rows.append({"symbol": sym, "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "open": px, "high": px * 1.01, "low": px * 0.99,
                         "close": px, "volume": 1e6, "amount": 1e7})
            px *= 1.001
    return pl.DataFrame(rows)


def test_has_factor_whitelist():
    from lquant.factors.qlib_alpha import has_factor

    assert has_factor("KMID")
    assert has_factor("ma20")
    assert not has_factor("pct_change_5")
    assert not has_factor("NOPE123")


def test_unknown_formula_raises_422_with_hint():
    from lquant.server.api.factors import _compute_factor

    with pytest.raises(HTTPException) as ei:
        _compute_factor(_panel(), "pct_change_abc")
    assert ei.value.status_code == 422
    assert "builtin" in ei.value.detail or "内置" in ei.value.detail


def test_missing_column_error_not_swallowed():
    """qlib 内部缺列（如 KMID 需要 open）必须抛原始错误，不能被 422 吞掉。"""
    from lquant.server.api.factors import _compute_factor

    bad = _panel().drop("open")
    with pytest.raises(Exception) as ei:
        _compute_factor(bad, "KMID")
    assert not (isinstance(ei.value, HTTPException) and ei.value.status_code == 422), \
        f"缺列错误被误报为 422: {ei.value}"


def test_dsl_error_raises_422_not_500():
    """缺陷：DSL 公式错误（解析失败/未注册算子/未知字段）直接炸 500，
    必须转成 422 并带原始错误信息。"""
    from lquant.server.api.factors import _compute_factor

    for bad in ("$bad + ", "Mean($close, 0)", "Rank($no_such_col)"):
        with pytest.raises(HTTPException) as ei:
            _compute_factor(_panel(), bad)
        assert ei.value.status_code == 422, f"{bad!r} 应为 422，实际 {ei.value.status_code}"
        assert str(ei.value.detail), "detail 应带原始错误信息"


def test_dsl_error_is_factor_error():
    """DSL 分支抛的应是 FactorError 家族（校验期就能识别），非裸异常。"""
    from lquant.core.errors import FactorError
    from lquant.factors.analysis import compute_factor_col

    with pytest.raises(FactorError):
        compute_factor_col(_panel(), "$bad + ", "_factor")


def test_pct_change_still_works():
    from lquant.server.api.factors import _compute_factor

    out = _compute_factor(_panel(), "pct_change_5")
    assert "_factor" in out.columns


def test_compute_factor_col_overwrites_existing_f_column():
    """robust 链路：train 帧已带上一轮 prepare_segment 算出的 f 列。

    多步表达式 compute(name='f') 曾因 rename 到已存在列名抛
    DuplicateError: column 'f' is duplicate —— 而单步路径 with_columns
    是覆盖语义。修复后两条路径一致：同名列 = 覆盖重算。
    """
    import numpy as np

    rng = np.random.default_rng(3)
    rows = []
    for sym in ("A", "B", "C"):
        px = 10.0
        for i in range(30):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": sym,
                         "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "volume": float(1e5 * (1 + rng.normal(0, 0.3)))})
    df = pl.DataFrame(rows)
    # 预置同名 f 列（旧值显然与重算结果不同）
    df = df.with_columns(pl.col("close").pct_change().over("symbol").alias("f"))

    from lquant.factors.analysis import compute_factor_col

    out = compute_factor_col(df, "Rank(-Ts_Corr(Rank(close), Rank(volume), 10))", "f")
    assert "f" in out.columns and len(out) == len(df)
    # 覆盖语义：非空行不应再是旧 pct_change 值
    assert out["f"].null_count() < len(out)
