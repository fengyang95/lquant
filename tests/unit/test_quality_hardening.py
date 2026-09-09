"""质量模块加固测试：跨源对拍零值/空帧、门禁主键去重、golden 只读防御。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest


def _good_bars(n_days: int = 5) -> pl.DataFrame:
    """与 test_quality._good_bars 同款干净数据（tests 不是包，就地复制）。"""
    from datetime import timedelta

    d0 = date(2026, 1, 5)
    dates = [d0 + timedelta(days=i) for i in range(n_days)]
    closes = [10.0 + 0.1 * i for i in range(n_days)]
    rows = {
        "symbol": ["000001.SZ"] * n_days,
        "trade_date": dates,
        "open": closes,
        "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes],
        "close": closes,
        "pre_close": [10.0] + closes[:-1],
        "volume": [1e6] * n_days,
        "amount": [c * 1e6 for c in closes],
    }
    return pl.DataFrame(rows)


# ---------- 跨源对拍 ----------

@pytest.fixture
def q_env(tmp_path_factory):
    """与 test_quality.q_env 同款隔离 tmp duckdb（tests 不是包，就地复制）。"""
    import os

    base = tmp_path_factory.mktemp("quality_hardening")
    old_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield base
    os.chdir(old_cwd)
    get_settings.cache_clear()


def test_crosscheck_zero_vs_nonzero_is_L3():
    """零值 vs 非零是数量级硬伤（L3 降级候选），不能因除零回 None 被放行成 L1。"""
    from lquant.data.quality import crosscheck

    key = {"trade_date": date(2026, 1, 6)}
    primary = pl.DataFrame({"symbol": ["000001.SZ"], **key,
                            "volume": [0.0], "close": [10.0]})
    peer = pl.DataFrame({"symbol": ["000001.SZ"], **key,
                         "volume": [1e6], "close": [10.0]})
    diffs = crosscheck.classify_divergence(primary, peer)
    vol = diffs.filter(pl.col("field") == "volume")
    assert vol["level"].to_list() == [crosscheck.L3]
    # 双零 → rel_diff=0（确定一致），不进告警档
    diffs2 = crosscheck.classify_divergence(primary.with_columns(volume=0.0),
                                            peer.with_columns(volume=0.0))
    vol2 = diffs2.filter(pl.col("field") == "volume")
    assert vol2["rel_diff"].to_list() == [0.0]
    assert vol2["level"].to_list() == [crosscheck.L1]


def test_crosscheck_empty_frames_do_not_panic():
    """空帧/无可比字段 → 带 schema 的空结果；flag/summarize 不因缺列 panic。"""
    from lquant.data.quality import crosscheck

    empty = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date,
                                 "close": pl.Float64})
    diffs = crosscheck.classify_divergence(empty, empty)
    assert len(diffs) == 0
    assert "level" in diffs.columns
    flagged = crosscheck.flag_cross_source(_good_bars(2), diffs)
    assert "quality_flags" in flagged.columns
    assert flagged["quality_flags"].to_list() == [0, 0]
    assert crosscheck.summarize(diffs) == {"L0": 0, "L1": 0, "L2": 0, "L3": 0,
                                           "checked": 0}
    # 无可比字段（两帧列完全不相交）同样走空结果而非报错
    a = pl.DataFrame({"symbol": ["x"], "trade_date": [date(2026, 1, 6)],
                      "foo": [1.0]})
    b = pl.DataFrame({"symbol": ["x"], "trade_date": [date(2026, 1, 6)],
                      "bar": [2.0]})
    assert len(crosscheck.classify_divergence(a, b)) == 0


# ---------- 门禁主键去重 ----------

def test_gate_daily_rejects_duplicate_keys(q_env):
    """重复 (symbol, trade_date) 入湖前必须拦住 —— 且 issue 先落库留证据。"""
    from lquant.core.errors import DataQualityError
    from lquant.data.quality.issues import latest_issues
    from lquant.data.quality.pipeline import gate_daily

    dup = pl.concat([_good_bars(3), _good_bars(3)])
    assert len(dup) == 6
    with pytest.raises(DataQualityError):
        gate_daily(dup, data_version="20260109.1")
    rules = {r["rule_code"] for r in latest_issues()}
    assert "DUP_KEY" in rules


# ---------- golden 只读防御 ----------

def test_golden_rejects_mutable_sql(q_env):
    from lquant.core.db import writer
    from lquant.data.quality.golden import GoldenCase, freeze, list_cases, run_all

    with writer() as w:
        w.register("_gb", _good_bars())
        w.execute("CREATE TABLE golden_src AS SELECT * FROM _gb")

    # 非 SELECT / 多语句 / 注释藏分号，一律拒绝
    for sql in ("DELETE FROM golden_src",
                "SELECT 1; SELECT 2",
                "SELECT 1 /* ; */; DROP TABLE golden_src"):
        with pytest.raises(ValueError):
            freeze([GoldenCase(name="evil", kind="structural", sql=sql)])

    # 合法 case（含注释）正常冻结
    ok = [GoldenCase(name="bar_count", kind="structural",
                     sql="-- count\nSELECT count(*) FROM golden_src")]
    assert freeze(ok) == 1

    # 纵深防御：冻结后 SQL 被篡改，运行时同样拦截
    with writer() as w:
        w.execute("UPDATE golden_expected SET sql = 'DELETE FROM golden_src' "
                  "WHERE name = 'bar_count'")
    with pytest.raises(ValueError):
        run_all()
    assert list_cases()[0].name == "bar_count"
