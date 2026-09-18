"""paper/alert 单元测试（对拍 NAV / 成交明细，纯内存）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.paper.alert import DeviationReport, compare_nav, compare_trades


def _nav(dates, values) -> pl.DataFrame:
    return pl.DataFrame({"trade_date": dates, "nav": values})


# ---------- DeviationReport ----------


def test_as_dict_rounds_and_corr_none_when_nonfinite():
    r = DeviationReport(0.1234567, 0.987654321, float("nan"), 3, "warning", "d")
    d = r.as_dict()
    assert d["max_nav_dev"] == 0.123457 and d["mean_nav_dev"] == 0.987654
    assert d["corr"] is None


# ---------- compare_nav ----------


def test_compare_nav_empty_side_is_critical():
    empty = pl.DataFrame(schema={"trade_date": pl.Date, "nav": pl.Float64})
    r = compare_nav(_nav([date(2026, 1, 5)], [100.0]), empty)
    assert r.verdict == "critical" and "空" in r.detail
    assert compare_nav(empty, empty).verdict == "critical"


def test_compare_nav_no_overlap_is_critical():
    a = _nav([date(2026, 1, 5), date(2026, 1, 6)], [100.0, 101.0])
    b = _nav([date(2026, 2, 1), date(2026, 2, 2)], [100.0, 101.0])
    r = compare_nav(a, b)
    assert r.verdict == "critical" and "日期无交集" in r.detail


def test_compare_nav_ok_within_tolerance():
    d = [date(2026, 1, i) for i in range(5, 10)]
    a = _nav(d, [100.0, 101.0, 102.0, 103.0, 104.0])
    b = _nav(d, [100.5, 101.5, 102.5, 103.5, 104.5])   # 偏差 ~0.5%
    r = compare_nav(a, b)
    assert r.verdict == "ok" and r.n_mismatch_days == 0
    assert 0 < r.max_nav_dev <= 0.01
    assert -1 <= r.corr <= 1


def test_compare_nav_warning_band():
    d = [date(2026, 1, i) for i in range(5, 10)]
    a = _nav(d, [100.0] * 5)
    b = _nav(d, [103.0, 100.0, 100.0, 100.0, 100.0])   # 最大 3% 偏差
    r = compare_nav(a, b, tol_daily=0.01)
    assert r.verdict == "warning" and r.n_mismatch_days == 1


def test_compare_nav_critical_beyond_5x():
    d = [date(2026, 1, i) for i in range(5, 10)]
    a = _nav(d, [100.0] * 5)
    b = _nav(d, [110.0, 100.0, 100.0, 100.0, 100.0])   # 10% > 5×1%
    r = compare_nav(a, b)
    assert r.verdict == "critical" and r.n_mismatch_days == 1


def test_compare_nav_two_rows_corr_is_nan():
    """仅 2 行重叠时相关性取 nan（as_dict 输出 None）。"""
    d = [date(2026, 1, 5), date(2026, 1, 6)]
    r = compare_nav(_nav(d, [100.0, 101.0]), _nav(d, [100.0, 102.0]))
    assert r.n_mismatch_days >= 0
    assert r.as_dict()["corr"] is None


# ---------- compare_trades ----------


def test_compare_trades_matched_and_only_sides():
    ts = "2026-01-05 09:31:00"
    a = pl.DataFrame({"ts": [ts], "symbol": ["600000.SH"], "side": ["buy"], "qty": [100]})
    b = pl.DataFrame({"ts": [ts], "symbol": ["600000.SH"], "side": ["buy"], "qty": [200]})
    res = compare_trades(a, b)
    assert res["matched"] == 1 and res["only_backtest"] == 0 and res["only_paper"] == 0
    assert res["match_rate"] == 1.0


def test_compare_trades_partial_match_and_qty_aggregation():
    ts1, ts2 = "2026-01-05 09:31:00", "2026-01-05 10:00:00"
    a = pl.DataFrame({"ts": [ts1, ts2], "symbol": ["A", "B"],
                      "side": ["buy", "buy"], "qty": [100, 50]})
    b = pl.DataFrame({"ts": [ts1], "symbol": ["A"], "side": ["buy"], "qty": [70]})
    res = compare_trades(a, b)
    assert res["bt_trades"] == 2 and res["paper_trades"] == 1
    assert res["matched"] == 1 and res["only_backtest"] == 1
    assert abs(res["match_rate"] - 0.5) < 1e-9


def test_compare_trades_empty_inputs():
    schema = {"ts": pl.Utf8, "symbol": pl.Utf8, "side": pl.Utf8, "qty": pl.Int64}
    res = compare_trades(pl.DataFrame(schema=schema), pl.DataFrame(schema=schema))
    assert res["bt_trades"] == 0 and res["paper_trades"] == 0
    assert res["matched"] == 0 and res["match_rate"] == 0.0


def test_compare_trades_date_slice_uses_ts_prefix():
    """同日不同时间戳的成交按日聚合。"""
    a = pl.DataFrame({"ts": ["2026-01-05 09:31:00", "2026-01-05 14:55:00"],
                      "symbol": ["A", "A"], "side": ["buy", "buy"],
                      "qty": [100, 50]})
    b = pl.DataFrame({"ts": ["2026-01-05 09:35:00"], "symbol": ["A"],
                      "side": ["buy"], "qty": [150]})
    res = compare_trades(a, b)
    assert res["matched"] == 1
