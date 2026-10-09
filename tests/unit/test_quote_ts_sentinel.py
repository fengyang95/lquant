"""``quote_ts`` 哨兵生产者的回归（让 B1 值级校验的三态真正可用）。

背景：``data/quality/integrity.py::partition_is_snapshot`` 靠 ``quote_ts``
区分「盘中实时落盘的分区」与「盘后权威历史」。此前 lquant 没有任何写入方
产出该列，对现有湖一律返回 UNKNOWN（有意的 fail-loudly）。本文件锁住补上
生产者之后的契约：

- 盘后批量路径（``ingest/daily.py::_stamp`` + ``store/parquet.py::write_daily``
  兜底）显式写 ``quote_ts=NULL`` → 判定 AUTHORITATIVE；
- 数据层显式写入的采集时刻：当日盘后 → AUTHORITATIVE；收盘前 → SNAPSHOT；
  落在别的日期 → 可疑（按 integrity 既有口径也算 SNAPSHOT）；
- 老 parquet 缺该列 → 读取不报错，判定 UNKNOWN（**不是** AUTHORITATIVE）；
- 写入 → 读回 round-trip，``quote_ts`` 值不变（含 NULL）。

全部走 tmp 湖（``LQ_ROOT`` 隔离），不触共享数据湖。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from lquant.data.quality import integrity as ig
from lquant.data.store import parquet as pq

CN = timezone(timedelta(hours=8))
DAY = date(2026, 9, 10)
OTHER_DAY = date(2026, 9, 11)


def _ms(hour: int, minute: int = 0, *, day: date = DAY) -> int:
    """上海墙钟 → epoch 毫秒（哨兵列口径）。"""
    return int(
        datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN).timestamp()
        * 1000
    )


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """LQ_ROOT + cwd + duckdb 隔离（``_stamp`` 会写血缘/issue 库）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "duckdb" / "lquant.duckdb"))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _daily_frame(
    symbols: list[str],
    *,
    day: date = DAY,
    quote_ts: list[int | None] | None = None,
    with_sentinel: bool = True,
    volume: float = 1_000_000.0,
) -> pl.DataFrame:
    """构造一批日线行（列子集足够过 gate_daily 的价格/量断言）。"""
    n = len(symbols)
    data: dict[str, list] = {
        "symbol": list(symbols),
        "trade_date": [day] * n,
        "open": [10.0] * n,
        "high": [10.5] * n,
        "low": [9.9] * n,
        "close": [10.2] * n,
        "pre_close": [10.0] * n,
        "volume": [volume] * n,
        "amount": [volume * 10.0] * n,
    }
    if with_sentinel:
        data["quote_ts"] = [None] * n if quote_ts is None else quote_ts
    return pl.DataFrame(data)


def _daily_dir() -> Path:
    return pq._root() / "daily"


def _year_file() -> Path:
    return _daily_dir() / "year=2026" / "part-0.parquet"


# ---------------------------------------------------------------------------
# 1) 盘后批量 = 显式 NULL = 权威
# ---------------------------------------------------------------------------
def test_batch_stamp_writes_explicit_null_and_is_authoritative(lake) -> None:
    """走真实批量盖章入口 ``_stamp``：``quote_ts`` 必为显式 Int64 NULL。"""
    from lquant.data.ingest.daily import _stamp

    stamped = _stamp(
        _daily_frame(["600000.SH", "000001.SZ"], with_sentinel=False), "test"
    )
    assert "quote_ts" in stamped.columns, "批量盖章必须显式产出哨兵列"
    assert stamped.schema["quote_ts"] == pl.Int64
    assert stamped["quote_ts"].null_count() == stamped.height

    pq.write_daily(stamped)
    on_disk = pl.read_parquet(_year_file())
    assert on_disk["quote_ts"].null_count() == on_disk.height

    verdict = ig.partition_is_snapshot(_daily_dir(), DAY)
    assert verdict.state == ig.AUTHORITATIVE
    assert not verdict.is_snapshot
    assert verdict.batch_rows == on_disk.height


def test_batch_frame_missing_sentinel_gets_explicit_null(lake) -> None:
    """写入层兜底：批量帧漏带该列也要落成显式 NULL（而非缺列）。"""
    pq.write_daily(_daily_frame(["600000.SH"], with_sentinel=False))
    on_disk = pl.read_parquet(_year_file())
    assert "quote_ts" in on_disk.columns, "缺列会被判 UNKNOWN，必须兜底成显式 NULL"
    assert on_disk["quote_ts"].null_count() == on_disk.height
    assert ig.partition_is_snapshot(_daily_dir(), DAY).state == ig.AUTHORITATIVE


# ---------------------------------------------------------------------------
# 2) 显式时间戳：盘中 → 快照；异日 → 可疑；当日盘后 → 权威
# ---------------------------------------------------------------------------
def test_preclose_sentinel_is_snapshot(lake) -> None:
    """收盘前（14:30）采集的行 → 盘中快照。"""
    pq.write_daily(_daily_frame(["600000.SH"], quote_ts=[_ms(14, 30)]))
    verdict = ig.partition_is_snapshot(_daily_dir(), DAY)
    assert verdict.state == ig.SNAPSHOT
    assert verdict.suspicious_rows == 1


def test_other_day_sentinel_is_suspicious(lake) -> None:
    """哨兵落在别的日期（陈旧报价）→ 同样可疑，不算权威。"""
    pq.write_daily(_daily_frame(["600000.SH"], quote_ts=[_ms(15, 30, day=OTHER_DAY)]))
    verdict = ig.partition_is_snapshot(_daily_dir(), DAY)
    assert verdict.state == ig.SNAPSHOT
    assert verdict.suspicious_rows == 1


def test_post_close_sentinel_is_authoritative(lake) -> None:
    """当日盘后（15:05）显式写入 → 按 integrity 口径仍是权威。"""
    pq.write_daily(_daily_frame(["600000.SH"], quote_ts=[_ms(15, 5)]))
    assert ig.partition_is_snapshot(_daily_dir(), DAY).state == ig.AUTHORITATIVE


# ---------------------------------------------------------------------------
# 3) 老 parquet 无该列 → 读取不报错 + UNKNOWN（绝不是 AUTHORITATIVE）
# ---------------------------------------------------------------------------
def test_legacy_parquet_without_sentinel_is_unknown(lake) -> None:
    _year_file().parent.mkdir(parents=True, exist_ok=True)
    _daily_frame(["600000.SH"], with_sentinel=False).write_parquet(_year_file())

    out = pq.read_daily(symbols=["600000.SH"], start=DAY, end=DAY).collect()
    assert out.height == 1  # 缺列不炸读取

    verdict = ig.partition_is_snapshot(_daily_dir(), DAY)
    assert verdict.state == ig.UNKNOWN
    assert verdict.state != ig.AUTHORITATIVE
    assert verdict.is_unknown


# ---------------------------------------------------------------------------
# 4) round-trip：值（含 NULL）不变
# ---------------------------------------------------------------------------
def test_quote_ts_roundtrip_preserves_values_and_null(lake) -> None:
    ts = _ms(15, 5)
    pq.write_daily(_daily_frame(["600000.SH", "000001.SZ"], quote_ts=[None, ts]))

    back = pl.read_parquet(_year_file())
    assert back.schema["quote_ts"] == pl.Int64
    got = dict(zip(back["symbol"].to_list(), back["quote_ts"].to_list(), strict=True))
    assert got == {"600000.SH": None, "000001.SZ": ts}

    lazy = pq.read_daily(start=DAY, end=DAY).collect()
    assert lazy.schema["quote_ts"] == pl.Int64
    assert sorted(map(str, lazy["quote_ts"].to_list())) == sorted(
        ["None", str(ts)]
    )
