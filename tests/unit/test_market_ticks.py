"""market.ticks 覆盖补齐：注入后端、校验收口与真实后端兜底链（全打桩）。"""

from __future__ import annotations

import math

import polars as pl
import pytest

from lquant.market import ticks
from lquant.market.ticks import TicksError, fetch_quotes


def test_empty_symbols_returns_empty() -> None:
    assert fetch_quotes([], backend=lambda s, timeout: []) == []


def test_backend_ok_rows_validated() -> None:
    rows = [
        {"symbol": "600519.SH", "name": "贵州茅台", "price": 1700.0,
         "change_pct": 1.2, "ts": "2026-09-17T10:00:00"},
        {"symbol": "", "price": 10.0},                       # 空符号 → 丢
        {"symbol": "X", "price": "nan"},                     # 非数值 → 丢
        {"symbol": "Y", "price": float("nan")},              # 非有限数 → 丢
        {"price": 5.0},                                      # 缺符号 → 丢
        {"symbol": "Z", "price": True},                      # bool 也是 int，保留
    ]
    out = fetch_quotes(rows, backend=lambda s, timeout: rows)
    assert [r["symbol"] for r in out] == ["600519.SH", "Z"]
    assert out[0]["price"] == 1700.0
    assert out[1]["price"] == 1.0  # bool 按 float 处理


def test_backend_exception_wrapped_in_ticks_error() -> None:
    def boom(symbols, timeout):
        raise ConnectionError("断网")

    with pytest.raises(TicksError) as ei:
        fetch_quotes(["600519.SH"], backend=boom)
    assert "ConnectionError" in ei.value.detail
    assert str(ei.value) == "ConnectionError: 断网"


def test_all_rows_invalid_raises() -> None:
    rows = [{"symbol": "A", "price": float("inf")}]
    with pytest.raises(TicksError, match="解析为空"):
        fetch_quotes(rows, backend=lambda s, timeout: rows)


def test_ticks_error_keeps_detail() -> None:
    e = TicksError("x")
    assert e.detail == "x"
    assert str(e) == "x"


def test_default_backend_routes_through_provider(monkeypatch) -> None:
    df = pl.DataFrame([
        {"symbol": "600519.SH", "name": "贵州茅台", "price": 1.0,
         "change_pct": 0.0},
    ])

    class _Prov:
        def realtime(self, symbols):
            return df

    monkeypatch.setattr(
        "lquant.data.providers.get_provider", lambda: _Prov())
    out = ticks._realtime_backend(["600519.SH"])
    assert out[0]["symbol"] == "600519.SH"
    assert out[0]["ts"]  # 补齐采集时间戳


def test_validate_rows_mixed_finiteness() -> None:
    rows = [{"symbol": "A", "price": 1.0},
            {"symbol": "B", "price": math.inf},
            {"symbol": "C", "price": -0.0}]
    out = ticks._validate_rows(rows)
    assert len(out) == 2
