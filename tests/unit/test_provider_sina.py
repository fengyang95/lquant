"""新浪复权因子适配器：JSONP 解析防御 + adj_factors 打桩。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.core.errors import CapabilityMissing, DataUnavailable, SourceSchemaChanged
from lquant.data.providers import sina as sina_mod
from lquant.data.providers.sina import SinaProvider, parse_hfq


def test_parse_hfq_data_key() -> None:
    payload = 'var _sh600519_hfq={"data":[["2026-01-05",1,2,3,30.5,100]]}'
    dates, closes = parse_hfq(payload)
    assert dates == ["2026-01-05"] and closes == [30.5]


def test_parse_hfq_year_grouped_unquoted_keys() -> None:
    payload = 'var x={2025:[["2025-01-02",1,2,3,10.0,5]],' \
              '2026:[["2026-01-05",1,2,3,11.0,5]]}'
    dates, closes = parse_hfq(payload)
    assert dates == ["2025-01-02", "2026-01-05"]
    assert closes == [10.0, 11.0]


def test_parse_hfq_no_json_raises() -> None:
    with pytest.raises(SourceSchemaChanged, match="找不到 JSON"):
        parse_hfq("garbage")


def test_parse_hfq_broken_json_recovers_and_raises_cleanly() -> None:
    # 补引号后仍解析失败 → SourceSchemaChanged
    with pytest.raises(SourceSchemaChanged, match="JSON 解析失败"):
        parse_hfq('var x={2026: not json}')


def test_parse_hfq_empty_rows_raises() -> None:
    with pytest.raises(SourceSchemaChanged, match="data 为空"):
        parse_hfq('var x={"data":[]}')


def test_parse_hfq_bad_row_shape_raises() -> None:
    with pytest.raises(SourceSchemaChanged, match="行结构异常"):
        parse_hfq('var x={"data":[["2026-01-05",1,2]]}')


def test_adj_factors_filters_window(monkeypatch) -> None:
    payload = ('var x={"data":[["2026-01-02",1,2,3,10.0,5],'
               '["2026-01-05",1,2,3,11.0,5],["2026-02-01",1,2,3,12.0,5]]}')
    monkeypatch.setattr(sina_mod, "_fetch_hfq", lambda code: payload)
    df = SinaProvider().adj_factors(
        ["600519.SH"], date(2026, 1, 1), date(2026, 1, 31))
    assert set(df["trade_date"].to_list()) == {
        date(2026, 1, 2), date(2026, 1, 5)}
    assert df["hfq_close"].to_list() == [10.0, 11.0]
    assert df.columns == ["symbol", "trade_date", "hfq_close"]


def test_adj_factors_no_data_raises_unavailable(monkeypatch) -> None:
    # 窗口过滤后为空 → frames 空 → DataUnavailable
    payload = 'var x={"data":[["2025-01-02",1,2,3,10.0,5]]}'
    monkeypatch.setattr(sina_mod, "parse_hfq",
                        lambda p: ([], []))
    monkeypatch.setattr(sina_mod, "_fetch_hfq", lambda code: payload)
    with pytest.raises(DataUnavailable):
        SinaProvider().adj_factors(
            ["600519.SH"], date(2026, 1, 1), date(2026, 1, 31))


def test_unsupported_methods_raise_capability_missing() -> None:
    p = SinaProvider()
    with pytest.raises(CapabilityMissing):
        p.daily_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(CapabilityMissing):
        p.minute_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2), "5")
    # freq 形如 "5min" 时 require 拼出 "minute_5min" —— Capability.parse 抛 ValueError
    with pytest.raises(ValueError, match="未知能力"):
        p.minute_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2), "5min")
    with pytest.raises(CapabilityMissing):
        p.financial_pit(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(CapabilityMissing):
        p.securities()
    with pytest.raises(CapabilityMissing):
        p.trade_calendar(date(2026, 1, 1), date(2026, 1, 2))
    assert isinstance(pl.DataFrame(), pl.DataFrame)  # noqa: F401 - 占位保持导入整洁

