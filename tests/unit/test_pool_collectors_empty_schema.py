"""涨跌停/板块采集器空结果路径：schema 构造不得抛错（Datetime 误用回归）。

真实故障：pl.Datetime("Asia/Shanghai") 把时区当 time_unit 传，
空结果日（如节假日后无涨跌停数据）采集器直接炸，连锁拖垮 sentiment。
"""
from __future__ import annotations


def test_broken_pool_empty_result_schema(monkeypatch) -> None:
    from lquant.market.collectors import limit_up

    monkeypatch.setattr(limit_up, "_fetch_pool", lambda *a, **k: [])
    df = limit_up.fetch_broken_pool(trade_date="2026-09-17", demo=False)
    assert df["collected_at"].dtype == __import__("polars").Datetime("us")


def test_limit_down_pool_empty_result_schema(monkeypatch) -> None:
    from lquant.market.collectors import limit_up

    monkeypatch.setattr(limit_up, "_fetch_pool", lambda *a, **k: [])
    df = limit_up.fetch_limit_down_pool(trade_date="2026-09-17", demo=False)
    assert df["collected_at"].dtype == __import__("polars").Datetime("us")


def test_sector_empty_schema_ok() -> None:
    from lquant.market.collectors.sector import _empty

    df = _empty()
    assert df["collected_at"].dtype == __import__("polars").Datetime("us")
