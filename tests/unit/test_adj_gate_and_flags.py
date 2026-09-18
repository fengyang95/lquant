"""adj 刷新与再同步的写入安全：门禁、因子不被 1.0 覆盖、flags 不被抹。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest


def _lake_frame(flags: int | None = None) -> pl.DataFrame:
    row = {
        "symbol": ["600000.SH"],
        "trade_date": [date(2026, 8, 3)],
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "pre_close": [9.9], "volume": [1_000_000.0], "amount": [1.0e7],
        "adj_factor": [2.5],
    }
    if flags is not None:
        row["quality_flags"] = [flags]
    return pl.DataFrame(row).with_columns(
        pl.col("adj_factor").cast(pl.Float64),
        *(pl.col("quality_flags").cast(pl.Int32),)
        if flags is not None else (),
    )


def _prep(tmp_path, monkeypatch, request, lake: pl.DataFrame) -> None:
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    # teardown 也清缓存：否则缓存的 tmp LQ_ROOT 泄漏给后续测试
    # （test_api 的 ruleset 加载会 FileNotFoundError）
    request.addfinalizer(get_settings.cache_clear)
    from lquant.data.store.parquet import write_daily

    write_daily(lake)


class _FakeAdjProvider:
    name = "fake"

    def adj_factors(self, syms, start_d, end_d):
        return pl.DataFrame({
            "symbol": syms,
            "trade_date": [date(2026, 8, 3)] * len(syms),
            "factor": [2.0],
        })


def test_adj_no_1dot0_fill_for_uncovered_dates(
        tmp_path, monkeypatch, request) -> None:
    """provider 未覆盖的日期不得被写成 adj_factor=1.0（保留湖中原值）。"""
    _prep(tmp_path, monkeypatch, request, _lake_frame())
    from lquant.data.ingest.adj import refresh_adj_factors

    n = refresh_adj_factors(days=60, provider=_FakeAdjProvider(),
                            start="2026-08-01", end="2026-08-05")
    assert n == 1
    from lquant.data.store.parquet import read_daily

    row = read_daily().collect().filter(pl.col("symbol") == "600000.SH")
    assert row["adj_factor"][0] == 2.0  # provider 覆盖日被更新


def test_adj_preserves_lake_factor_when_provider_gaps(
        tmp_path, monkeypatch, request) -> None:
    """湖中有 5 天，provider 只覆盖 1 天：其余天 adj_factor 保留湖内原值。"""
    lake = pl.concat([
        _lake_frame().with_columns(
            trade_date=pl.lit(date(2026, 8, d)).cast(pl.Date))
        for d in (3, 4, 5, 6, 7)
    ])
    _prep(tmp_path, monkeypatch, request, lake)
    from lquant.data.ingest.adj import refresh_adj_factors

    refresh_adj_factors(days=60, provider=_FakeAdjProvider(),
                        start="2026-08-01", end="2026-08-07")
    from lquant.data.store.parquet import read_daily

    rows = read_daily().collect().sort("trade_date")
    got = rows["adj_factor"].to_list()
    assert got == [2.0, 2.5, 2.5, 2.5, 2.5], \
        f"仅 provider 覆盖日更新，其余保留湖内值，实际 {got}"


def test_adj_rejects_negative_factor(tmp_path, monkeypatch, request) -> None:
    """复权因子为负/零 → DataQualityError，不入湖。"""
    _prep(tmp_path, monkeypatch, request, _lake_frame())
    from lquant.core.errors import DataQualityError
    from lquant.data.ingest.adj import refresh_adj_factors

    class _BadProvider(_FakeAdjProvider):
        def adj_factors(self, syms, start_d, end_d):
            return pl.DataFrame({
                "symbol": syms,
                "trade_date": [date(2026, 8, 3)] * len(syms),
                "factor": [-1.0],
            })

    with pytest.raises(DataQualityError):
        refresh_adj_factors(days=60, provider=_BadProvider(),
                            start="2026-08-01", end="2026-08-05")
    from lquant.data.store.parquet import read_daily

    row = read_daily().collect().filter(pl.col("symbol") == "600000.SH")
    assert row["adj_factor"][0] == 2.5  # 湖内值未被破坏


def test_adj_rejects_duplicate_keys(tmp_path, monkeypatch, request) -> None:
    """同键重复行 → DataQualityError。"""
    _prep(tmp_path, monkeypatch, request, _lake_frame())
    from lquant.core.errors import DataQualityError
    from lquant.data.ingest.adj import refresh_adj_factors

    class _DupProvider(_FakeAdjProvider):
        def adj_factors(self, syms, start_d, end_d):
            return pl.concat([
                pl.DataFrame({"symbol": syms, "trade_date":
                              [date(2026, 8, 3)] * len(syms), "factor": [2.0]}),
                pl.DataFrame({"symbol": syms, "trade_date":
                              [date(2026, 8, 3)] * len(syms), "factor": [3.0]}),
            ])

    with pytest.raises(DataQualityError):
        refresh_adj_factors(days=60, provider=_DupProvider(),
                            start="2026-08-01", end="2026-08-05")


def test_resync_preserves_crosscheck_flags(tmp_path, monkeypatch, request) -> None:
    """跨源对拍打的 quality_flags 在同键再同步后不得被整行覆盖抹掉。"""
    _prep(tmp_path, monkeypatch, request, _lake_frame(flags=0b10000))
    from lquant.data.store.parquet import write_daily

    # 再同步同键批次（干净的 flags=0）
    new = _lake_frame().with_columns(
        source=pl.lit("baostock"),
        quality_flags=pl.lit(0, dtype=pl.Int32),
        ingested_at=pl.lit(None, dtype=pl.Datetime),
        data_version=pl.lit("v2"),
    )
    write_daily(new)
    from lquant.data.store.parquet import read_daily

    row = read_daily().collect().filter(pl.col("symbol") == "600000.SH")
    assert row.height == 1, "同键覆盖不应产生重复行"
    assert row["quality_flags"][0] & 0b10000, "对拍标记不得被抹掉"


def test_resync_fresh_key_flags_not_polluted(tmp_path, monkeypatch, request) -> None:
    """不同键的再同步不受影响（flags 合并只作用于同键行）。"""
    _prep(tmp_path, monkeypatch, request, _lake_frame(flags=0b10000))
    from lquant.data.store.parquet import write_daily

    new = _lake_frame().with_columns(
        symbol=pl.lit("000001.SZ"),
        source=pl.lit("baostock"),
        quality_flags=pl.lit(0, dtype=pl.Int32),
        ingested_at=pl.lit(None, dtype=pl.Datetime),
        data_version=pl.lit("v2"),
    )
    write_daily(new)
    from lquant.data.store.parquet import read_daily

    row = read_daily().collect().filter(pl.col("symbol") == "000001.SZ")
    assert row["quality_flags"][0] == 0
