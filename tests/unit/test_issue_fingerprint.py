"""issue 内容指纹测试：detail 内嵌计数变化不得生成新 issue_id。"""
from __future__ import annotations

from lquant.data.quality.issues import Issue


def test_fingerprint_ignores_detail_counts():
    """同规则同标的，detail 计数从 3 变 5 → 同一 issue_id（覆盖而非膨胀）。"""
    a = Issue(rule="ZOMBIE", severity="warn", detail="3 行零成交", count=3)
    b = Issue(rule="ZOMBIE", severity="warn", detail="5 行零成交", count=5)
    assert a.to_row(None)["issue_id"] == b.to_row(None)["issue_id"]


def test_fingerprint_distinct_symbols_stay_distinct():
    """不同标的仍是不同 issue。"""
    a = Issue(rule="ZOMBIE", severity="warn", detail="3 行零成交",
              symbol="600000.SH")
    b = Issue(rule="ZOMBIE", severity="warn", detail="5 行零成交",
              symbol="000001.SZ")
    assert a.to_row(None)["issue_id"] != b.to_row(None)["issue_id"]


def test_fingerprint_field_in_extra_distinguishes():
    """extra 字段名（如 crosscheck 的 field）参与区分：同键不同 field 不合并。"""
    a = Issue(rule="CROSS_SRC_DIFF", severity="error", detail="偏差过大",
              symbol="600000.SH", extra={"field": "close"})
    b = Issue(rule="CROSS_SRC_DIFF", severity="error", detail="偏差过大",
              symbol="600000.SH", extra={"field": "open"})
    assert a.to_row(None)["issue_id"] != b.to_row(None)["issue_id"]


def test_fingerprint_counts_in_extra_masked():
    """extra 里的数值变化不产生新 issue_id（如 dates 截断后的计数差异）。"""
    a = Issue(rule="COVERAGE_GAP", severity="error", detail="缺失 3 天",
              symbol="600000.SH", extra={"dates": ["2026-01-01", "2026-01-02", "2026-01-03"]})
    b = Issue(rule="COVERAGE_GAP", severity="error", detail="缺失 5 天",
              symbol="600000.SH",
              extra={"dates": ["2026-01-01", "2026-01-02", "2026-01-03",
                               "2026-01-06", "2026-01-07"]})
    assert a.to_row(None)["issue_id"] == b.to_row(None)["issue_id"]
