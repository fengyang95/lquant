"""Task 2：质量门禁停牌豁免（is_suspended 行不参与断言，不影响入湖数据）。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality.asserts import run_record_checks
from lquant.data.quality.flags import PRICE_OUT_OF_RANGE, SUSPENSION_FILL, ZOMBIE


def test_suspended_rows_exempt_from_record_checks():
    """停牌行自带零量/零价不触发断言；正常行同样问题仍然命中。"""
    d0 = date(2026, 1, 5)
    df = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "trade_date": [d0, date(2026, 1, 6)],
        "open": [10.0, 0.0],           # 第二行是停牌行 0 价格，不豁免会 fatal
        "high": [10.05, 0.0],
        "low": [9.95, 0.0],
        "close": [10.0, 0.0],
        "pre_close": [10.0, 10.0],
        "volume": [1e6, 0.0],
        "amount": [1e6 * 10.0, 0.0],
        "is_suspended": [False, True],
    })
    out, issues = run_record_checks(df, raise_on_fatal=False)
    assert out["quality_flags"].to_list() == [0, 0]
    assert not any(i.severity == "fatal" for i in issues)

    # 同样的数据但非停牌 → 照常命中 PRICE_OUT_OF_RANGE（fatal）
    df_bad = df.with_columns(pl.lit(False).alias("is_suspended"))
    out2, issues2 = run_record_checks(df_bad, raise_on_fatal=False)
    assert out2["quality_flags"][1] & PRICE_OUT_OF_RANGE
    assert any(i.severity == "fatal" for i in issues2)


def test_suspended_exempt_covers_warn_checks():
    """零量零收益（zombie）与零量价动（suspension fill）对停牌行豁免。"""
    d0 = date(2026, 1, 5)
    df = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "trade_date": [d0, date(2026, 1, 6)],
        "open": [10.0, 10.0],
        "high": [10.05, 10.0],
        "low": [9.95, 10.0],
        "close": [10.0, 10.0],
        "pre_close": [10.0, 10.0],
        "volume": [1e6, 0.0],
        "amount": [1e6 * 10.0, 0.0],
        "is_suspended": [False, True],
    })
    out, _ = run_record_checks(df, raise_on_fatal=False)
    assert out["quality_flags"].to_list() == [0, 0]
    assert not (out["quality_flags"][1] & ZOMBIE)
    assert not (out["quality_flags"][1] & SUSPENSION_FILL)
