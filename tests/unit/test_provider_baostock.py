"""BaoStockProvider 迁移到 MappingProvider 引擎后的回归测试。

全部通过 _raw 直传 / monkeypatch watchdog，不触网。
使用仓库真实 config/schema/*.yaml（迁移交付物本身）。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

import lquant.data.watchdog as wd
from lquant.data.mapping import load_table_mapping
from lquant.data.providers.baostock import BaoStockProvider
from lquant.data.schema import SCHEMAS

P = BaoStockProvider()

DAILY_ROWS = [
    # date, code, open, high, low, close, preclose, volume, amount(千元), turn, tradestatus, isST
    ["2024-01-02", "sh.600000", "10.0", "10.5", "9.8", "10.2", "10.1",
     "1000", "3000", "1.5", "1", "0"],
    ["2024-01-03", "sh.600000", "10.2", "10.6", "10.0", "10.4", "10.2",
     "1100", "3300", "1.6", "1", "1"],
    ["2024-01-04", "sh.600000", "10.4", "10.8", "10.2", "10.6", "10.4",
     "0", "0", "0", "0", "0"],  # 停牌行，应被过滤
]


def _daily_raw() -> pl.DataFrame:
    return pl.DataFrame(
        DAILY_ROWS,
        schema=["date", "code", "open", "high", "low", "close",
                "preclose", "volume", "amount", "turn", "tradestatus", "isST"],
        orient="row",
    )


def test_daily_mapping_via_engine() -> None:
    raw = (
        _daily_raw()
        .filter(pl.col("tradestatus") != "0")
        .drop(["tradestatus", "isST"])
        .with_columns(
            pl.col("date").str.to_date("%Y-%m-%d"),
            pl.col(["open", "high", "low", "close", "preclose", "volume",
                    "amount"]).cast(pl.Float64),
            pl.col("turn").cast(pl.Float64, strict=False),
        )
    )
    p = BaoStockProvider()
    p._pending_is_st = pl.Series("is_st", [False, True])
    out = p.request("daily_bar", _raw=raw)
    assert out.columns[:16] == list(SCHEMAS["daily_bar"])
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["trade_date"].dtype == pl.Date
    assert out["amount"].to_list() == [300000.0, 330000.0]  # 千元 → 元
    assert out["pre_close"].to_list() == [10.1, 10.2]
    assert out["turnover_rate"].to_list() == [1.5, 1.6]
    assert out["sec_type"].to_list() == ["stock", "stock"]
    assert out["source"].to_list() == ["baostock", "baostock"]
    assert out["quality_flags"].to_list() == [0, 0]
    assert out["adj_factor"].to_list() == [1.0, 1.0]
    assert out["is_st"].to_list() == [False, True]
    assert out["ingested_at"].is_null().all()
    assert out["data_version"].is_null().all()


def test_daily_fetch_filters_suspended_and_converts_is_st(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_daily：停牌行过滤 + isST→bool，watchdog 打桩不触网。"""
    captured: dict[str, object] = {}

    def fake_query(fn: object, *args: object, **kw: object) -> list[list[str]]:
        captured["code"] = args[0]
        return DAILY_ROWS

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    raw = p._fetch_daily(["600000.SH"], date(2024, 1, 1), date(2024, 1, 4))
    assert captured["code"] == "sh.600000"
    assert len(raw) == 2  # 停牌行被过滤
    assert p._pending_is_st.to_list() == [False, True]
    assert "tradestatus" not in raw.columns and "isST" not in raw.columns


def test_minute_mapping_via_engine() -> None:
    raw = pl.DataFrame({
        "code": ["sh.600000", "sh.600000", "sh.600000"],
        "ts": [
            datetime(2024, 1, 2, 10, 30),
            datetime(2024, 1, 2, 11, 30),
            datetime(2024, 1, 2, 14, 0),
        ],
        "open": [10.0, 10.2, 10.4],
        "high": [10.5, 10.6, 10.8],
        "low": [9.8, 10.0, 10.2],
        "close": [10.2, 10.4, 10.6],
        "volume": [1000.0, 1100.0, 1200.0],
        "amount": [10000.0, 11000.0, 12000.0],  # 已是元，不换算
    })
    out = P.request("minute_bar", _raw=raw, freq="60min")
    assert out.columns[:12] == list(SCHEMAS["minute_bar"])
    assert out["symbol"].to_list() == ["600000.SH"] * 3
    assert out["freq"].to_list() == ["60min"] * 3  # params 覆盖 yaml fill 的 5min
    assert out["amount"].to_list() == [10000.0, 11000.0, 12000.0]
    assert out["adj_factor"].to_list() == [1.0, 1.0, 1.0]
    assert out["source"].to_list() == ["baostock"] * 3
    assert out["ingested_at"].dtype == pl.Datetime
    assert not out["ingested_at"].is_null().any()
    # 60min 边界归一：11:30 → 11:00，14:00 保持
    assert out["ts"].dt.hour().to_list() == [10, 11, 14]
    assert out["ts"].dt.minute().to_list() == [30, 0, 0]


def test_minute_5min_no_boundary_shift() -> None:
    """非 60min 的 freq 不做边界归一。"""
    raw = pl.DataFrame({
        "code": ["sh.600000"],
        "ts": [datetime(2024, 1, 2, 11, 30)],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
        "volume": [1000.0], "amount": [10000.0],
    })
    out = P.request("minute_bar", _raw=raw, freq="5min")
    assert out["freq"].to_list() == ["5min"]
    assert out["ts"].dt.hour().to_list() == [11]
    assert out["ts"].dt.minute().to_list() == [30]


def test_minute_fetch_parses_time_and_slices_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_minute：time(YYYYmmddHHMMSSmmm) → ts，按年切片。"""
    calls: list[str] = []

    def fake_query(fn: object, *args: object) -> list[list[str]]:
        code, fields, start, end, freq, adj = args
        calls.append(f"{code}|{start}~{end}|{freq}")
        if start.startswith("2023"):
            return []
        return [
            ["2024-01-02", "20240102113000000", code, "10.0", "10.5", "9.8",
             "10.2", "1000", "10000", "3"],
            ["2024-01-02", "20240102140000000", code, "10.2", "10.6", "10.0",
             "10.4", "1100", "11000", "3"],
        ]

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    raw = p._fetch_minute(["600000.SH"], date(2023, 11, 1), date(2024, 2, 1), "60min")
    assert calls == ["sh.600000|2023-11-01~2023-12-31|60min",
                     "sh.600000|2024-01-01~2024-02-01|60min"]
    assert raw["ts"].dt.hour().to_list() == [11, 14]
    assert raw["ts"].dt.minute().to_list() == [30, 0]
    assert raw["close"].dtype == pl.Float64
    assert "time" not in raw.columns and "date" not in raw.columns


def test_minute_bad_freq_raises_before_network() -> None:
    with pytest.raises(ValueError, match="60min|不支持"):
        P._fetch_minute(["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), "7min")


def test_real_yaml_loads() -> None:
    """交付的两份 yaml 与 SCHEMAS 校验兼容（fail-fast 不炸）。"""
    dm = load_table_mapping("daily_bar", "baostock")
    assert dm.rename == {"date": "trade_date", "code": "symbol",
                         "preclose": "pre_close", "turn": "turnover_rate"}
    assert "amount" in dm.derive
    assert dm.fill["source"] == "baostock"
    mm = load_table_mapping("minute_bar", "baostock")
    assert mm.fill == {"freq": "5min", "source": "baostock", "adj_factor": 1.0}
