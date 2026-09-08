"""测试夹具。"""
from __future__ import annotations

import polars as pl
import pytest


@pytest.fixture
def daily_bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * 5 + ["600000.SH"] * 5,
            "trade_date": [__import__("datetime").date(2026, 1, d) for d in range(5, 10)] * 2,
            "open": [10.0, 10.1, 10.2, 10.3, 10.4] * 2,
            "high": [10.5] * 10,
            "low": [9.5] * 10,
            "close": [10.0, 10.1, 10.2, 10.3, 10.4] * 2,
            "pre_close": [10.0, 10.0, 10.1, 10.2, 10.3] * 2,
            "volume": [1e6] * 10,
            "amount": [1e7] * 10,
        }
    )
