"""Parquet 湖读写。

日频按年单文件（不要按 symbol 分文件 —— 小文件灾难）。
因子值一因子一目录，便于增量重算与缓存失效。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import polars as pl

from lquant.core.config import get_settings

# 读-改-写按 (路径) 加锁：不同 year / (freq, ym) 文件互不阻塞。
# 锁保护同进程并发（sync-worker 线程与本地任务线程同进程），
# 并保证「读旧 → 合并 → 写临时文件 → os.replace」对读侧原子可见。
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


def _atomic_write_parquet(df: pl.DataFrame, p: Path) -> None:
    """先写同目录临时文件再 os.replace：读侧不会看到半截文件。"""
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd")
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _root() -> Path:
    p = Path(get_settings().parquet_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _daily_path(year: int) -> Path:
    return _root() / "daily" / f"year={year}" / "part-0.parquet"


def write_daily(df: pl.DataFrame) -> list[Path]:
    if not len(df):
        return []
    out: list[Path] = []
    for year, g in df.with_columns(pl.col("trade_date").dt.year().alias("y")).group_by("y"):
        y = year[0]
        p = _daily_path(y)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 同key覆盖：读旧 → 去旧 → 合并 → 原子写（锁按文件粒度，不串行全年份）
        with _file_lock(p):
            if p.exists():
                old = pl.read_parquet(p)
                old = old.filter(
                    ~pl.struct(["symbol", "trade_date"]).is_in(
                        g.select(pl.struct(["symbol", "trade_date"])).to_series()
                    )
                )
                g = pl.concat([old, g], how="diagonal")
            g = g.drop("y").sort(["symbol", "trade_date"])
            _atomic_write_parquet(g, p)
        out.append(p)
    return out


def read_daily(symbols: list[str] | None = None, start=None, end=None) -> pl.LazyFrame:
    from datetime import date as _date

    root = _root() / "daily"
    # data 目录 gitignore，全新 checkout 下根目录不存在 → rglob 会抛
    # FileNotFoundError。用 is_dir 短路：无库即空帧，而不是炸读取。
    if not root.is_dir() or not any(root.rglob("*.parquet")):
        return pl.DataFrame().lazy()
    # 字符串日期显式转 Date，避免 filter 时类型比较失败
    if isinstance(start, str):
        start = _date.fromisoformat(start)
    if isinstance(end, str):
        end = _date.fromisoformat(end)
    lf = pl.scan_parquet(
        str(root / "**" / "*.parquet"),
        # 跨年文件 schema 漂移容错：增量回填（如 baostock 带 is_st）会改写
        # 单个年文件，老文件缺列/新文件多列都不该炸读取
        missing_columns="insert",
        extra_columns="ignore",
    )
    if symbols:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    if start:
        lf = lf.filter(pl.col("trade_date") >= start)
    if end:
        lf = lf.filter(pl.col("trade_date") <= end)
    return lf


def write_factor(factor: str, df: pl.DataFrame) -> Path:
    p = _root() / "factors" / f"name={factor}"
    p.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p / "part-0.parquet", compression="zstd")
    return p / "part-0.parquet"


def write_minute(df: pl.DataFrame, freq: str | None = None) -> list[Path]:
    """分钟线按 (freq, 年月) 分区。

    按 symbol 分文件会产生几十万小文件；按年月分，单文件几十 MB，读写都快。
    """
    if not len(df):
        return []
    freq = freq or (df["freq"][0] if "freq" in df.columns else "60min")
    out: list[Path] = []
    keyed = df.with_columns(
        pl.col("ts").dt.strftime("%Y-%m").alias("_ym")
    ) if "ts" in df.columns else df
    for ym, g in keyed.group_by("_ym"):
        period = ym[0]
        p = _root() / "minute" / f"freq={freq}" / f"year_month={period}" / "part-0.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        g = g.drop("_ym")
        # 同key覆盖：读旧 → 去旧 → 合并 → 原子写（锁按文件粒度）
        with _file_lock(p):
            if p.exists():
                old = pl.read_parquet(p)
                if "symbol" in old.columns and "ts" in old.columns:
                    old = old.filter(
                        ~pl.struct(["symbol", "ts"]).is_in(
                            g.select(pl.struct(["symbol", "ts"])).to_series()
                        )
                    )
                    g = pl.concat([old, g], how="diagonal")
            g = g.sort(["symbol", "ts"]) if "ts" in g.columns else g
            _atomic_write_parquet(g, p)
        out.append(p)
    return out


def read_minute(symbols: list[str] | None = None, freq: str = "60min",
                start=None, end=None) -> pl.LazyFrame:
    root = _root() / "minute" / f"freq={freq}"
    if not root.is_dir() or not any(root.rglob("*.parquet")):
        return pl.DataFrame().lazy()
    lf = pl.scan_parquet(str(root / "**" / "*.parquet"))
    if symbols:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    if start:
        lf = lf.filter(pl.col("ts") >= start)
    if end:
        lf = lf.filter(pl.col("ts") <= end)
    return lf
