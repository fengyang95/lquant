"""三表勾稽：口径正确性、缺失值安全、报告语义。

回归点：FinancialTool 原实现在 ``NI is None`` 时直接除法会抛 ``TypeError``；
本实现对任何缺失输入都返回 ``None``（记为「未检查」），绝不炸也不静默给分。
"""
from __future__ import annotations

import pytest

from lquant.fundamental import (
    DEFAULT_FULL_SCORES,
    DEFAULT_TIERS,
    PASS_TIER,
    banded_score,
    cash_change_gap,
    earnings_quality_gap,
    ocf_to_ni_ratio,
    reconcile,
    retained_earnings_gap,
)

# ---------- 单项口径 ----------

def test_retained_earnings_gap_math():
    # NI=100, OCI=0, ΔRE=102 → 差额 2 → 2%
    assert retained_earnings_gap(100.0, 0.0, 102.0) == pytest.approx(0.02)
    # OCI 计入
    assert retained_earnings_gap(100.0, 5.0, 105.0) == pytest.approx(0.0)
    # 亏损公司用 |NI| 作分母
    assert retained_earnings_gap(-100.0, 0.0, -110.0) == pytest.approx(0.10)


def test_retained_earnings_gap_missing_inputs():
    assert retained_earnings_gap(None, 0.0, 100.0) is None
    assert retained_earnings_gap(100.0, 0.0, None) is None
    assert retained_earnings_gap(0.0, 0.0, 100.0) is None      # 分母为 0 不炸


def test_cash_change_gap_math():
    assert cash_change_gap(50.0, 50.0) == pytest.approx(0.0)
    assert cash_change_gap(100.0, 80.0) == pytest.approx(20.0 / 100.0)
    assert cash_change_gap(0.0, 0.0) == pytest.approx(0.0)     # 两边都 0 → 视为一致
    assert cash_change_gap(None, 1.0) is None


def test_earnings_quality_gap_math():
    assert earnings_quality_gap(100.0, 100.0) == pytest.approx(0.0)
    assert earnings_quality_gap(100.0, 90.0) == pytest.approx(0.10)
    assert earnings_quality_gap(None, 90.0) is None
    assert earnings_quality_gap(0.0, 90.0) is None


def test_ocf_to_ni_ratio():
    assert ocf_to_ni_ratio(120.0, 100.0) == pytest.approx(1.2)
    assert ocf_to_ni_ratio(50.0, -10.0) is None                # 亏损时无解释力
    assert ocf_to_ni_ratio(None, 100.0) is None


# ---------- 分档给分 ----------

def test_banded_score_tiers():
    full = 5.0
    assert banded_score(0.01, DEFAULT_TIERS, full) == pytest.approx(5.0)
    assert banded_score(0.05, DEFAULT_TIERS, full) == pytest.approx(4.0)   # 边界归入下一档
    assert banded_score(0.10, DEFAULT_TIERS, full) == pytest.approx(4.0)
    assert banded_score(0.20, DEFAULT_TIERS, full) == pytest.approx(2.0)
    assert banded_score(0.99, DEFAULT_TIERS, full) == pytest.approx(0.0)


def test_banded_score_uses_absolute_value_and_handles_none():
    assert banded_score(-0.01, DEFAULT_TIERS, 5.0) == pytest.approx(5.0)
    assert banded_score(None, DEFAULT_TIERS, 5.0) == 0.0


# ---------- 报告 ----------

def test_reconcile_all_pass():
    r = reconcile("600519.SH", net_income=100.0, other_comprehensive=0.0,
                  delta_retained=102.0, cashflow_net_change=50.0,
                  balance_cash_change=50.5, deducted_net_income=97.0)
    assert r.passed is True
    assert r.failed == ()
    assert len(r.checked_items) == 3
    assert r.score == pytest.approx(sum(DEFAULT_FULL_SCORES.values()))
    assert r.available_score == pytest.approx(sum(DEFAULT_FULL_SCORES.values()))


def test_reconcile_detects_failures():
    r = reconcile("600519.SH", net_income=100.0, delta_retained=190.0,
                  cashflow_net_change=50.0, balance_cash_change=50.0,
                  deducted_net_income=40.0)
    names = {i.name for i in r.failed}
    assert names == {"retained_earnings", "earnings_quality"}
    assert r.passed is False
    assert r.score == pytest.approx(DEFAULT_FULL_SCORES["cash_change"])


def test_reconcile_no_data_is_not_pass():
    """一项都没查到不能算通过 —— 否则数据缺失会被当成质量优秀。"""
    r = reconcile("600519.SH")
    assert r.passed is False
    assert r.checked_items == ()
    assert r.failed == ()
    assert r.score == 0.0 and r.available_score == 0.0


def test_reconcile_partial_data_excludes_unchecked_from_available():
    r = reconcile("600519.SH", net_income=100.0, delta_retained=101.0)
    assert len(r.checked_items) == 1
    assert r.available_score == pytest.approx(DEFAULT_FULL_SCORES["retained_earnings"])
    assert r.passed is True


def test_reconcile_to_dict_shape():
    d = reconcile("600519.SH", net_income=100.0, delta_retained=190.0).to_dict()
    assert d["symbol"] == "600519.SH" and d["passed"] is False
    assert d["failed"] == ["retained_earnings"]
    assert len(d["items"]) == 3
    unchecked = [i for i in d["items"] if i["passed"] is None]
    assert len(unchecked) == 2                                 # 未检查项标 None 而非 False


def test_pass_tier_constant_is_within_tiers_range():
    assert any(upper == PASS_TIER for upper, _ in DEFAULT_TIERS)
