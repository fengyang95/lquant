"""parquet 湖读写补测：写入/覆盖合并/删除/分钟线/因子/异常降级路径。

全部走 tmp 湖（fake_settings 隔离 LQ_ROOT），不触共享数据湖。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from lquant.data.store import parquet as pq


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """LQ_ROOT + cwd 隔离（同 test_backfill_pool 模式，避免 patch 模块属性泄漏）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def lake(fake_settings):
    return pq


def _daily(rows):
    base = pl.DataFrame(schema=pq.SCHEMAS["daily_bar"]) if False else None
    data = {
        "symbol": [r[0] for r in rows],
        "trade_date": [r[1] for r in rows],
        "open": [1.0] * len(rows),
        "high": [1.0] * len(rows),
        "low": [1.0] * len(rows),
        "close": [2.0] * len(rows),
        "pre_close": [1.0] * len(rows),
        "volume": [100.0] * len(rows),
        "amount": [200.0] * len(rows),
        "quality_flags": [0] * len(rows),
    }
    return pl.DataFrame(data)


def test_write_read_daily_roundtrip(lake) -> None:
    df = _daily([("600000.SH", date(2024, 1, 2)), ("600000.SH", date(2025, 6, 10))])
    paths = lake.write_daily(df)
    assert len(paths) == 2
    out = lake.read_daily(symbols=["600000.SH"], start="2024-01-01",
                          end="2025-12-31").collect()
    assert out.height == 2
    assert lake.daily_range() == (date(2024, 1, 2), date(2025, 6, 10))
    assert lake.latest_trade_date() == date(2025, 6, 10)
    assert lake.latest_top_by_amount(1) == ["600000.SH"]


def test_write_daily_empty(lake) -> None:
    assert lake.write_daily(pl.DataFrame()) == []
    assert lake.write_daily_basic(pl.DataFrame()) == []
    assert lake.write_minute(pl.DataFrame()) == []


def test_write_daily_basic_roundtrip(lake) -> None:
    df = pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2024, 1, 2)],
        "turn": [0.8], "pe_ttm": [5.0], "pb_mrq": [1.0],
        "ps_ttm": [1.0], "pcf_ncf_ttm": [2.0],
        "total_mv": [1e8], "float_mv": [8e7],
    })
    paths = lake.write_daily_basic(df)
    assert len(paths) == 1
    # 再写一次同键 → 覆盖
    df2 = df.with_columns(pe_ttm=pl.lit(6.0))
    lake.write_daily_basic(df2)
    out = lake.read_daily_basic("2024-01-01", "2024-12-31")
    assert out["pe_ttm"][0] == 6.0
    assert out.height == 1
    # 空湖读 → 带 schema 空帧（换新 tmp 目录验证）— 这里只验证过滤
    assert lake.read_daily_basic(start="2030-01-01").is_empty()


def test_lake_is_empty_and_glob(lake) -> None:
    assert lake.lake_is_empty("daily")
    assert lake.lake_is_empty("minute/freq=60min")
    g = lake.lake_glob("daily")
    assert g.endswith("*.parquet") and "daily" in g
    lake.write_daily(_daily([("600000.SH", date(2024, 1, 2))]))
    assert not lake.lake_is_empty("daily")


def test_overlay_empty_old(lake) -> None:
    new = _daily([("600000.SH", date(2024, 1, 2))])
    out = pq._overlay(pl.DataFrame(), new, ["symbol", "trade_date"])
    assert out.height == 1


def test_merge_quality_flags_keeps_lake_flags(lake) -> None:
    old = _daily([("600000.SH", date(2024, 1, 2))]).with_columns(quality_flags=pl.lit(4))
    new = _daily([("600000.SH", date(2024, 1, 2))]).with_columns(quality_flags=pl.lit(0))
    merged = pq._merge_quality_flags(old, new)
    assert merged["quality_flags"][0] == 4        # 湖内标记不被再同步抹掉
    # 无 quality_flags 列 → 原样返回 new
    assert pq._merge_quality_flags(old.drop("quality_flags"),
                                   new.drop("quality_flags")).height == 1


def test_write_daily_overwrite_merges(lake) -> None:
    lake.write_daily(_daily([("600000.SH", date(2024, 1, 2))]))
    df2 = _daily([("600000.SH", date(2024, 1, 2)),
                  ("600000.SH", date(2024, 1, 3))]).with_columns(close=pl.lit(3.0))
    lake.write_daily(df2)
    out = lake.read_daily().collect().sort("trade_date")
    assert out.height == 2
    assert out["close"].to_list() == [3.0, 3.0]


def test_read_daily_empty_lake(lake) -> None:
    lf = lake.read_daily(symbols=["600000.SH"])
    assert lf.collect().is_empty()
    assert lf.collect_schema().names() == list(pq.SCHEMAS["daily_bar"])


def test_delete_daily_dry_run_and_real(lake) -> None:
    lake.write_daily(_daily([("600000.SH", date(2024, 1, 2)),
                             ("000001.SZ", date(2024, 1, 2))]))
    r = lake.delete_daily(symbols=["600000.SH"], start="2024-01-01",
                          end="2024-12-31", dry_run=True)
    assert r["rows_matched"] == 1 and r["files_scanned"] == 1
    assert r["files"][0]["rows_matched"] == 1
    r2 = lake.delete_daily(symbols=["600000.SH"], start=date(2024, 1, 1),
                           end=date(2024, 12, 31))
    assert r2["rows_matched"] == 1
    # 剩余行仍在
    assert lake.read_daily().collect()["symbol"].unique().to_list() == ["000001.SZ"]
    # 全删 → 文件被 unlink
    r3 = lake.delete_daily(start="2024-01-01", end="2024-12-31")
    assert r3["rows_matched"] == 1
    assert lake.lake_is_empty("daily")


def test_delete_daily_no_files(lake) -> None:
    r = lake.delete_daily(symbols=["600000.SH"])
    assert r == {"rows_matched": 0, "files_scanned": 0, "files": []}


def test_daily_range_read_error_degrades(lake, monkeypatch) -> None:
    lake.write_daily(_daily([("600000.SH", date(2024, 1, 2))]))
    import polars as plx

    orig = plx.scan_parquet

    def boom(*a, **k):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(plx, "scan_parquet", boom)
    assert pq.daily_range() == (None, None)
    assert orig is not None


def test_write_factor_and_read(lake) -> None:
    df = pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2024, 1, 2)], "value": [0.5],
    })
    p = lake.write_factor("mom20", df)
    assert p.exists()
    assert pl.read_parquet(p)["value"].to_list() == [0.5]


def test_write_read_minute(lake) -> None:
    ts = datetime(2024, 1, 2, 10, 30)
    df = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "ts": [ts, ts.replace(hour=14)],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.1, 1.2], "volume": [10.0, 10.0], "amount": [100.0, 100.0],
        "freq": ["60min", "60min"],
    })
    paths = lake.write_minute(df)
    assert len(paths) == 1
    out = lake.read_minute(symbols=["600000.SH"], freq="60min",
                           start=ts, end=ts.replace(hour=14)).collect()
    assert out.height == 2
    # 同键覆盖
    lake.write_minute(df.with_columns(close=pl.lit(9.9)))
    out2 = lake.read_minute(freq="60min").collect()
    assert out2.height == 2 and out2["close"].unique().to_list() == [9.9]


def test_write_minute_no_ts_column(lake) -> None:
    """无 ts 列的 df 不再炸 group_by("_ym")：单文件覆盖写。"""
    df = pl.DataFrame({"symbol": ["600000.SH"], "close": [1.0]})
    out = lake.write_minute(df)
    assert len(out) == 1 and out[0].exists()


def test_read_minute_empty(lake) -> None:
    lf = lake.read_minute(symbols=["600000.SH"])
    assert lf.collect().is_empty()


def test_atomic_write_cleanup_on_failure(lake, monkeypatch, tmp_path) -> None:
    df = _daily([("600000.SH", date(2024, 1, 2))])

    def boom(self, *a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", boom)
    with pytest.raises(RuntimeError):
        pq._atomic_write_parquet(df, tmp_path / "x.parquet")
    assert list(tmp_path.glob("*.tmp")) == []


def test_file_lock_degrades_on_oserror(lake, monkeypatch, tmp_path) -> None:
    import fcntl

    calls = {"n": 0}
    real_flock = fcntl.flock

    def flaky(fd, op):
        calls["n"] += 1
        raise OSError("read-only fs")

    monkeypatch.setattr(pq.fcntl, "flock", flaky)
    p = tmp_path / "a.parquet"
    with pq._file_lock(p):
        pass                                    # 锁降级但不抛
    assert calls["n"] >= 1
    monkeypatch.setattr(fcntl, "flock", real_flock)
