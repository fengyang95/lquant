"""portfolio/dedup.py 单测：相关矩阵、贪心去重、簇标记与概览。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.portfolio.dedup import (
    cluster_labels,
    cluster_summary,
    correlation_matrix,
    dedup,
)


def make_returns_df(n_days: int = 120, seed: int = 5) -> pl.DataFrame:
    """A、B 高相关（≈1）；C、D 独立低相关。"""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.01, n_days)
    return pl.DataFrame({
        "A": a,
        "B": a + rng.normal(0, 0.0005, n_days),
        "C": rng.normal(0, 0.01, n_days),
        "D": rng.normal(0, 0.01, n_days),
    })


# ---------- correlation_matrix ----------

def test_correlation_matrix_from_df():
    corr, syms = correlation_matrix(make_returns_df())
    assert syms == ["A", "B", "C", "D"]
    assert corr.shape == (4, 4)
    assert abs(corr[0, 1]) > 0.95          # A-B 高相关
    assert abs(corr[0, 2]) < 0.5           # A-C 低相关
    np.testing.assert_allclose(np.diag(corr), 1.0)


def test_correlation_matrix_excludes_date_cols():
    df = make_returns_df().with_columns(pl.Series("trade_date", range(120)))
    corr, syms = correlation_matrix(df)
    assert "trade_date" not in syms


def test_correlation_matrix_from_ndarray_default_names():
    rng = np.random.default_rng(1)
    M = rng.normal(size=(50, 3))
    corr, syms = correlation_matrix(M)
    assert syms == ["a0", "a1", "a2"]
    np.testing.assert_allclose(np.diag(corr), 1.0)


def test_correlation_matrix_short_sample_identity():
    # 少于 3 行退回单位阵
    corr, syms = correlation_matrix(np.array([[0.01, 0.02], [0.0, -0.01]]))
    np.testing.assert_allclose(corr, np.eye(2))
    assert syms == ["a0", "a1"]


def test_correlation_matrix_nan_and_constant_column():
    # 常数列（方差为 0）→ corrcoef 出 NaN → 应被填 0 后 clip
    M = np.column_stack([np.ones(10), np.arange(10.0)])
    corr, _ = correlation_matrix(M)
    assert np.isfinite(corr).all()
    assert corr[0, 1] == 0.0
    assert np.diag(corr) == pytest.approx(np.ones(2))


def test_correlation_matrix_df_symbols_explicit():
    df = make_returns_df()
    corr, syms = correlation_matrix(df, symbols=["A", "C"])
    assert syms == ["A", "C"] and corr.shape == (2, 2)


# ---------- dedup ----------

def test_dedup_removes_highly_correlated():
    kept = dedup(make_returns_df(), threshold=0.9)
    assert "A" in kept and "B" not in kept   # 同簇只留先到者
    assert {"C", "D"} <= set(kept)
    assert kept == [s for s in ["A", "B", "C", "D"] if s in kept]  # 保持输入序


def test_dedup_single_or_empty():
    assert dedup(np.zeros((5, 1)), ["A"]) == ["A"]
    assert dedup(np.zeros((0, 0)), []) == []


def test_dedup_priority_dict_reorders():
    """优先级 dict：B 优先 → 保留 B 丢 A。"""
    kept = dedup(make_returns_df(), threshold=0.9,
                 priority={"A": 1.0, "B": 10.0})
    assert "B" in kept and "A" not in kept


def test_dedup_priority_df_with_amount():
    df = make_returns_df()
    prio = pl.DataFrame({"symbol": ["A", "B", "C", "D"],
                         "amount": [100.0, 900.0, 500.0, 400.0]})
    kept = dedup(df, threshold=0.9, priority=prio, priority_col="amount")
    assert kept[0] == "B"
    assert "A" not in kept


def test_dedup_priority_df_all_null_rows():
    """priority 表 drop_nulls 后为空 → 退回输入顺序去重（B 与 A 同簇被丢）。"""
    prio = pl.DataFrame({"symbol": [None, None], "amount": [None, None]})
    kept = dedup(make_returns_df(), priority=prio)
    assert kept == ["A", "C", "D"]


def test_dedup_priority_symbols_not_in_table_get_zero():
    prio = pl.DataFrame({"symbol": ["A"], "amount": [5.0]})
    kept = dedup(make_returns_df(), threshold=0.9, priority=prio)
    # B/C/D 缺席按 0 分，A=5 分排最前
    assert kept[0] == "A"


def test_dedup_threshold_zero_keeps_first_only():
    # 阈值 0：任何非零相关都被去重，只剩第一个
    kept = dedup(make_returns_df(), threshold=0.0)
    assert len(kept) == 1


def test_dedup_negative_correlation_also_deduped():
    # 用 |corr| 判定：强负相关同样算同质
    rng = np.random.default_rng(2)
    a = rng.normal(0, 0.01, 100)
    df = pl.DataFrame({"A": a, "B": -a})
    kept = dedup(df, threshold=0.9)
    assert len(kept) == 1


# ---------- cluster_labels / cluster_summary ----------

def test_cluster_labels_groups_correlated():
    labels = cluster_labels(make_returns_df(), threshold=0.9)
    assert labels["A"] == labels["B"]
    assert labels["C"] != labels["D"]
    assert len(set(labels.values())) == 3   # AB / C / D 三簇


def test_cluster_summary_shape():
    out = cluster_summary(make_returns_df(), threshold=0.9)
    assert out["size"][0] == 2              # A、B 同簇排最前
    assert set(out["members"][0].split(", ")) == {"A", "B"}
    assert out.height == 3


def test_cluster_summary_empty_returns_empty_df():
    out = cluster_summary(np.zeros((0, 0)), [])
    assert isinstance(out, pl.DataFrame) and out.is_empty()


def test_cluster_labels_threshold_zero():
    labels = cluster_labels(make_returns_df(), threshold=0.0)
    # 阈值 0 → 全部归一个簇
    assert len(set(labels.values())) == 1
