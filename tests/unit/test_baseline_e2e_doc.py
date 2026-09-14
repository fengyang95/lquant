"""E2E 文档写回不丢人工补注的回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_baseline_e2e.py"
_SPEC = importlib.util.spec_from_file_location("run_baseline_e2e", _SCRIPT)
_MOD = importlib.util.module_from_spec(_SPEC)
import sys  # noqa: E402

sys.modules["run_baseline_e2e"] = _MOD  # dataclass 解析注解需在 sys.modules 中
_SPEC.loader.exec_module(_MOD)

merge_manual_notes = _MOD.merge_manual_notes
extract_manual_notes = _MOD.extract_manual_notes


NEW_DOC = """# 运行记录

## Metrics

| a | b |
|---|---|
| 1 | 2 |
"""

OLD_NOTES = """## 人工补注

1. **数据缺口**:2021-2023 未回填。
2. **前 4 个月 0 持仓是预热伪影**。
"""

OLD_DOC = NEW_DOC + "\n" + OLD_NOTES


def test_extract_returns_notes_to_eof():
    assert extract_manual_notes(OLD_DOC).startswith("## 人工补注")
    assert "预热伪影" in extract_manual_notes(OLD_DOC)


def test_extract_missing_returns_empty():
    assert extract_manual_notes(NEW_DOC) == ""
    assert extract_manual_notes("") == ""


def test_merge_preserves_manual_notes_on_rerun():
    merged = merge_manual_notes(NEW_DOC, OLD_DOC)
    assert merged.startswith("# 运行记录")
    assert "预热伪影" in merged
    assert merged.count("## 人工补注") == 1


def test_merge_idempotent_and_noop():
    # 新文档已带补注段 → 原样返回
    assert merge_manual_notes(NEW_DOC + OLD_NOTES, OLD_DOC) == NEW_DOC + OLD_NOTES
    # 旧文档无补注段 → 原样返回
    assert merge_manual_notes(NEW_DOC, NEW_DOC) == NEW_DOC


# --- G3:默认预热区间 ---

import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import polars as pl  # noqa: E402

clip_warmup = _MOD.clip_warmup
resolve_warmup_start = _MOD.resolve_warmup_start


class _Fill:
    def __init__(self, d, qty=100, price=10.0, fee=1.0):
        self.trade_date = d
        self.qty = qty
        self.price = price
        self.fee = fee


def _fake_res(warm_nav, post_nav):
    return SimpleNamespace(
        nav=warm_nav + post_nav,
        trades=[_Fill("2023-12-01"), _Fill("2024-01-05")],
        rejected=[("2023-11-01", "000001.XSHE", "no_cash")],
        records={
            "n_positions": [("2023-12-15", 2), ("2024-01-05", 20)],
            "mv": [("2023-12-15", 1.0), ("2024-01-05", 2.0)],
        },
        metrics={"initial_cash": 10_000_000, "benchmark": "SH000300"},
    )


def test_clip_warmup_drops_pre_start_and_recomputes_metrics():
    res = _fake_res(
        [("2023-12-15", 10_000_000.0), ("2023-12-29", 10_100_000.0)],
        [("2024-01-02", 10_100_000.0), ("2024-01-03", 10_200_000.0)],
    )
    out = clip_warmup(res, "2024-01-01")
    assert [str(d) for d, _ in out.nav] == ["2024-01-02", "2024-01-03"]
    assert [str(t.trade_date) for t in out.trades] == ["2024-01-05"]
    assert out.rejected == []
    assert out.records["n_positions"] == [("2024-01-05", 20)]
    m = out.metrics
    assert m["n_trades"] == 1 and m["n_rejected"] == 0
    assert m["initial_cash"] == 10_000_000
    assert 0.0098 < m["total_return"] < 0.00991
    assert m["turnover"]["total_amount"] == 100 * 10.0


def test_clip_warmup_no_warmup_is_noop():
    res = _fake_res([], [("2024-01-01", 10_000_000.0), ("2024-01-02", 10_100_000.0)])
    out = clip_warmup(res, "2024-01-01")
    assert [str(d) for d, _ in out.nav] == ["2024-01-01", "2024-01-02"]
    assert out.metrics["n_trades"] == 1  # 2023-12-01 的成交被剔除
    assert out.metrics["total_return"] > 0


def test_resolve_warmup_zero_disables():
    assert resolve_warmup_start("2024-01-01", 0) == "2024-01-01"


def test_resolve_warmup_skips_when_lake_empty(monkeypatch):
    def fake_read_daily(*a, **kw):
        return pl.LazyFrame({"trade_date": []}, schema={"trade_date": pl.Date})

    monkeypatch.setattr(_MOD.store, "read_daily", fake_read_daily)
    assert resolve_warmup_start("2024-01-01", 120) == "2024-01-01"


def test_resolve_warmup_advances_when_lake_has_data(monkeypatch):
    def fake_read_daily(*a, **kw):
        return pl.LazyFrame(
            {"trade_date": [datetime.date(2023, 12, 1)]}, schema={"trade_date": pl.Date}
        )

    monkeypatch.setattr(_MOD.store, "read_daily", fake_read_daily)
    assert resolve_warmup_start("2024-01-01", 120) == "2023-09-03"
