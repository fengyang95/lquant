"""factors/preprocess/orthogonalize.py 单测：对称正交 / 逐步回归 / PCA / none。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from lquant.factors.preprocess.orthogonalize import gram_schmidt, none, pca, symmetric

SYMS = [f"S{i:02d}" for i in range(10)]


def make_panel(n_days: int = 8, seed: int = 9) -> pl.DataFrame:
    """10 只 × n_days，三个强相关因子 + 一点噪声。

    注意：必须按 trade_date 排序 —— orthogonalize 的各实现用 cursor 位置回写，
    未排序输入会被静默错位（源码缺陷，见测试类报告）。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for s in SYMS:
        base = rng.normal(size=n_days)
        rows.append(pl.DataFrame({
            "trade_date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n_days)],
            "symbol": s,
            "f1": base,
            "f2": 0.9 * base + rng.normal(scale=0.1, size=n_days),
            "f3": 0.8 * base + rng.normal(scale=0.2, size=n_days),
        }))
    return pl.concat(rows).sort("trade_date")


# ---------- none / 单列直通 ----------

def test_none_passthrough_and_single_col():
    df = make_panel()
    out = none(df, ["f1", "f2"])
    assert out is df
    # 因子数 < 2 → 原样返回
    assert symmetric(df, ["f1"]) is df
    assert gram_schmidt(df, ["f1"]) is df
    assert pca(df, ["f1"]) is df


# ---------- symmetric ----------

def test_symmetric_produces_uncorrelated_columns():
    df = make_panel(n_days=10)
    out = symmetric(df, ["f1", "f2", "f3"])
    assert out.height == df.height
    sub = out.filter(pl.col("trade_date") == date(2024, 1, 2))
    X = np.column_stack([sub[c].to_numpy() for c in ["f1", "f2", "f3"]])
    Xc = X - X.mean(axis=0)
    c = np.corrcoef(Xc, rowvar=False)
    off = c - np.eye(3)
    assert np.abs(off).max() < 1e-8       # 当日内列间正交
    # 数值不等于原始值（确实做了变换）
    orig = df.filter(pl.col("trade_date") == date(2024, 1, 2))["f1"].to_numpy()
    assert not np.allclose(X[:, 0], orig)


def test_symmetric_small_cross_section_becomes_nan():
    """当日样本数 ≤ 因子数 → 该日输出 NaN。"""
    df = make_panel(n_days=6).filter(
        ~((pl.col("trade_date") == date(2024, 1, 1))
          & pl.col("symbol").is_in(SYMS[3:])))
    assert df.filter(pl.col("trade_date") == date(2024, 1, 1)).height == 3
    out = symmetric(df, ["f1", "f2", "f3"])
    d1 = out.filter(pl.col("trade_date") == date(2024, 1, 1))
    assert d1["f1"].is_nan().sum() == len(d1)


def test_symmetric_with_nan_rows_excluded():
    """缺因子值的行不参与变换，输出 NaN。"""
    df = make_panel(n_days=6)
    df = df.with_columns(
        pl.when((pl.col("trade_date") == date(2024, 1, 2))
                & (pl.col("symbol") == "S00"))
        .then(None).otherwise(pl.col("f1")).alias("f1"))
    out = symmetric(df, ["f1", "f2", "f3"])
    bad = out.filter((pl.col("trade_date") == date(2024, 1, 2))
                     & (pl.col("symbol") == "S00"))
    assert bad["f1"].is_nan().sum() == 1
    good = out.filter((pl.col("trade_date") == date(2024, 1, 2))
                      & (pl.col("symbol") == "S01"))
    assert not bool(good["f1"].is_nan()[0])


def test_symmetric_linalg_error_fallback():
    """eigh 失败 → 退回标准化矩阵 Z 本身。"""
    df = make_panel(n_days=8)
    calls = {"n": 0}
    real_eigh = np.linalg.eigh

    def flaky_eigh(a, *a2, **kw):
        calls["n"] += 1
        raise np.linalg.LinAlgError("boom")

    np.linalg.eigh = flaky_eigh
    try:
        out = symmetric(df, ["f1", "f2", "f3"])
    finally:
        np.linalg.eigh = real_eigh
    assert calls["n"] >= 1
    # 退回 Z：标准化后列间不再强相关但不保证正交；仅验证有限值输出
    assert out["f1"].is_finite().all() if hasattr(out["f1"], "is_finite") \
        else bool(np.isfinite(out["f1"].to_numpy()).all())


# ---------- gram_schmidt ----------

def test_gram_schmidt_sequential_residuals():
    df = make_panel(n_days=10)
    out = gram_schmidt(df, ["f1", "f2", "f3"])
    sub = out.filter(pl.col("trade_date") == date(2024, 1, 2))
    x1 = sub["f1"].to_numpy()
    x2 = sub["f2"].to_numpy()
    x3 = sub["f3"].to_numpy()
    # 第 2/3 个因子对已正交因子残差化 → 与前者正交
    for a, b in [(x1, x2), (x1, x3), (x2, x3)]:
        a = a - a.mean()
        b = b - b.mean()
        assert abs(np.dot(a, b)) < 1e-8
    # 首因子保持原值
    orig = df.filter(pl.col("trade_date") == date(2024, 1, 2))["f1"].to_numpy()
    np.testing.assert_allclose(x1, orig)


def test_gram_schmidt_small_cross_section_becomes_nan():
    df = make_panel(n_days=6).filter(
        ~((pl.col("trade_date") == date(2024, 1, 1))
          & pl.col("symbol").is_in(SYMS[3:])))
    out = gram_schmidt(df, ["f1", "f2", "f3"])
    d1 = out.filter(pl.col("trade_date") == date(2024, 1, 1))
    assert d1["f2"].is_nan().sum() == len(d1)


# ---------- pca ----------

def test_pca_components_orthogonal_and_ordered():
    df = make_panel(n_days=10)
    out = pca(df, ["f1", "f2", "f3"])
    new_cols = [c for c in out.columns if c.startswith("pc_")]
    assert new_cols == ["pc_0", "pc_1", "pc_2"]
    sub = out.filter(pl.col("trade_date") == date(2024, 1, 2))
    P = np.column_stack([sub[c].to_numpy() for c in new_cols])
    P = P[~np.isnan(P).any(axis=1)]
    P = P - P.mean(axis=0)
    gram = P.T @ P
    off = gram - np.diag(np.diag(gram))
    assert np.abs(off).max() < 1e-7      # 严格正交
    # 方差递减
    assert np.diag(gram)[0] >= np.diag(gram)[1] >= np.diag(gram)[2]


def test_pca_n_components():
    df = make_panel(n_days=8)
    out = pca(df, ["f3", "f1"], n_components=2, prefix="q")
    assert [c for c in out.columns if c.startswith("q_")] == ["q_0", "q_1"]


def test_pca_svd_failure_fallback():
    df = make_panel(n_days=8)
    real_svd = np.linalg.svd

    def flaky_svd(*a, **kw):
        raise np.linalg.LinAlgError("boom")

    np.linalg.svd = flaky_svd
    try:
        out = pca(df, ["f1", "f2", "f3"])
    finally:
        np.linalg.svd = real_svd
    # 退回 Z[:, :k]
    assert out.height == df.height
    assert np.isfinite(out["pc_0"].to_numpy()).all()


def test_pca_small_cross_section_becomes_nan():
    df = make_panel(n_days=6).filter(
        ~((pl.col("trade_date") == date(2024, 1, 1))
          & pl.col("symbol").is_in(SYMS[3:])))
    out = pca(df, ["f1", "f2", "f3"])
    d1 = out.filter(pl.col("trade_date") == date(2024, 1, 1))
    assert d1["pc_0"].is_nan().sum() == len(d1)


def test_unsorted_input_rows_aligned():
    """未按日期排序（symbol-major）的输入：结果必须按原始行对位，不得错位。

    修复前 cursor 按分区顺序回写，symbol-major 输入的输出值全部张冠李戴。
    """
    rows = []
    for sym in ("A", "B", "C"):
        for i, d in enumerate((date(2024, 1, 1), date(2024, 1, 2))):
            rows.append({"trade_date": d, "symbol": sym,
                         "f1": float(i + (sym == "B")), "f2": float(sym != "A")})
    df = pl.DataFrame(rows)
    out = symmetric(df, ["f1", "f2"])
    out_sorted = symmetric(df.sort("trade_date"), ["f1", "f2"])
    assert out["f1"].is_not_null().all() and out.height == 6
    # 按行键对齐比较：行序不同但每个 (symbol, date) 的值必须一致
    a = dict(zip(zip(out["symbol"], out["trade_date"], strict=True), out["f1"], strict=True))
    b = dict(zip(zip(out_sorted["symbol"], out_sorted["trade_date"], strict=True), out_sorted["f1"], strict=True))
    assert a.keys() == b.keys()
    for k, _ in a.items():
        assert abs(a[k] - b[k]) < 1e-9, f"{k}: {a[k]} != {b[k]}"
