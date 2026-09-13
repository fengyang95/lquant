"""Parquet 湖读写。

日频按年单文件（不要按 symbol 分文件 —— 小文件灾难）。
因子值一因子一目录，便于增量重算与缓存失效。
"""
from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

import polars as pl

from lquant.core.config import get_settings
from lquant.data.schema import SCHEMAS

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


def lake_glob(kind: str = "daily") -> str:
    """某类数据的 parquet glob（**绝对路径**）。

    SQL 侧（`read_parquet`）必须用这个，不要在 SQL 里硬编码
    `'data/parquet/...'` —— 那是相对 CWD 的路径，服务进程与 CLI 的 CWD
    不同就会读到空集，且失败是静默的（曾经 DuckDB `daily_bar` 幽灵表
    就是这么来的：表在 DDL 里，却从没有任何写入路径）。
    """
    return str(_root() / kind / "**" / "*.parquet")


def _has_parquet(root: Path) -> bool:
    """目录下是否至少有一个 parquet 文件。

    `rglob` 是生成器，`any()` 命中首个文件即短路 —— 有数据时几乎零成本，
    只有真·空目录才会走完整个树。
    """
    return root.is_dir() and any(root.rglob("*.parquet"))


def lake_is_empty(subdir: str = "daily") -> bool:
    """湖的某个分区是否**一个 parquet 文件都没有**（`subdir` 相对湖根，
    如 `daily` / `minute/freq=60min`）。

    存在的意义是区分两种语义不同的「空」：
    - 湖为空：全新 checkout / 从未同步 → 消费方该提示「先同步数据」；
    - 湖有数据但没有目标标的或区间 → 正常的空结果。

    读函数返回的是**有 schema 的空帧**（空库读出空结果，不是抛异常），
    所以「帧是空的」无法再区分上面两种情形，必须显式问这个。
    """
    return not _has_parquet(_root() / subdir)


def _empty_frame(name: str) -> pl.LazyFrame:
    """空湖返回「有 schema 的空帧」而不是 0 列空帧。

    0 列空帧一旦被下游 `.select(["symbol", "trade_date"])` 命中就是
    ColumnNotFoundError —— 空库应当读出空结果，不是炸读取。
    """
    return pl.DataFrame(schema=SCHEMAS[name]).lazy()


def _overlay(old: pl.DataFrame, new: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """把 new 覆盖到 old 上（同主键替换语义）。

    三步：① 主键 dtype 对齐到湖内既有 schema（老文件不被新批改写列类型）；
    ② anti-join 去掉 old 里与 new 同键的行；③ diagonal 合并（容忍跨版本加列）。

    为什么不用 `struct(keys).is_in(new_keys)`：dtype 漂移（旧年文件
    trade_date=Date / 新批 Datetime）时 is_in **静默返回全 False** ——
    去重失效、重复行悄悄累积且读取侧不可见；join 会显式报错。
    """
    if not old.height:
        return new
    if all(k in old.columns and k in new.columns for k in keys):
        new = new.with_columns(
            pl.col(k).cast(old.schema[k], strict=False) for k in keys
        )
        old = old.join(new.select(keys).unique(), on=keys, how="anti")
    return pl.concat([old, new], how="diagonal")


def _daily_path(year: int) -> Path:
    return _root() / "daily" / f"year={year}" / "part-0.parquet"


def _daily_basic_path(year: int) -> Path:
    return _root() / "daily_basic" / f"year={year}" / "part-0.parquet"


def write_daily_basic(df: pl.DataFrame) -> list[Path]:
    """daily_basic 湖按年单文件，覆盖语义与 write_daily 一致。"""
    if not len(df):
        return []
    out: list[Path] = []
    for year, g in df.with_columns(pl.col("trade_date").dt.year().alias("y")).group_by("y"):
        y = year[0]
        p = _daily_basic_path(y)
        p.parent.mkdir(parents=True, exist_ok=True)
        g = g.drop("y")
        with _file_lock(p):
            if p.exists():
                g = _overlay(pl.read_parquet(p), g, ["symbol", "trade_date"])
            g = g.sort(["symbol", "trade_date"])
            _atomic_write_parquet(g, p)
        out.append(p)
    return out


def read_daily_basic(start=None, end=None) -> pl.DataFrame:
    """读 daily_basic 湖（区间过滤，空湖返回带 schema 的空帧）。"""
    from datetime import date as _date

    root = _root() / "daily_basic"
    if not _has_parquet(root):
        return _empty_frame("daily_basic")
    if isinstance(start, str):
        start = _date.fromisoformat(start)
    if isinstance(end, str):
        end = _date.fromisoformat(end)
    df = (
        pl.scan_parquet(
            str(root / "**" / "*.parquet"),
            missing_columns="insert",
            extra_columns="ignore",
        ).collect()
    )
    if start is not None:
        df = df.filter(pl.col("trade_date") >= start)
    if end is not None:
        df = df.filter(pl.col("trade_date") <= end)
    return df


def write_daily(df: pl.DataFrame) -> list[Path]:
    if not len(df):
        return []
    out: list[Path] = []
    for year, g in df.with_columns(pl.col("trade_date").dt.year().alias("y")).group_by("y"):
        y = year[0]
        p = _daily_path(y)
        p.parent.mkdir(parents=True, exist_ok=True)
        g = g.drop("y")
        # 同key覆盖：读旧 → 覆盖合并 → 原子写（锁按文件粒度，不串行全年份）
        with _file_lock(p):
            if p.exists():
                g = _overlay(pl.read_parquet(p), g, ["symbol", "trade_date"])
            g = g.sort(["symbol", "trade_date"])
            _atomic_write_parquet(g, p)
        out.append(p)
    return out


def read_daily(symbols: list[str] | None = None, start=None, end=None) -> pl.LazyFrame:
    from datetime import date as _date

    root = _root() / "daily"
    # data 目录 gitignore，全新 checkout 下根目录不存在 → rglob 会抛
    # FileNotFoundError。用 is_dir 短路：无库即空帧，而不是炸读取。
    if not _has_parquet(root):
        return _empty_frame("daily_bar")
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


def delete_daily(
    symbols: list[str] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    dry_run: bool = False,
) -> dict:
    """从日线湖删除数据（按标的 / 日期区间过滤后重写文件）。

    - dry_run=True 只统计将删行数，不落盘（用于确认前预览）；
    - 读 → 过滤 → 写回/unlink 整体在 _file_lock 内（与写入路径互斥）；
      注意：锁只保护**同进程**线程并发，跨进程不安全 —— 调用方（HTTP
      purge 端点 / CLI）负责保证不与其他进程并发删同一份湖；
    - 过滤后为空的文件直接删除，不留空 parquet；
    - 返回 {rows_matched, files_scanned, files: [{file, rows_matched}]}。

    删除不可逆，调用方负责互斥锁（/data/purge 与 /data/check 共用
    _lake_check_lock）与审计日志。
    """
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    root = _root() / "daily"
    files = sorted(root.rglob("*.parquet")) if _has_parquet(root) else []
    total = 0
    touched: list[dict] = []
    for p in files:
        with _file_lock(p):
            df = pl.read_parquet(p)
            mask = pl.lit(True)
            if symbols:
                mask = pl.col("symbol").is_in(symbols)
            if start is not None:
                mask = mask & (pl.col("trade_date") >= start)
            if end is not None:
                mask = mask & (pl.col("trade_date") <= end)
            matched = int(df.select(mask.sum().fill_null(0)).item())
            if not matched:
                continue
            total += matched
            from loguru import logger

            logger.info(f"purge {'(dry_run) ' if dry_run else ''}"
                        f"{p.name}: {matched} 行{'（预览，未落盘）' if dry_run else ''}")
            touched.append({"file": str(p), "rows_matched": matched})
            if dry_run:
                continue
            kept = df.filter(~mask)
            if kept.height:
                _atomic_write_parquet(kept, p)
            else:
                p.unlink()  # 整文件清空 → 删文件，留空 parquet 徒增扫描成本
    return {"rows_matched": total, "files_scanned": len(files), "files": touched}


def daily_range() -> tuple[date | None, date | None]:
    """湖内日线的最小 / 最大交易日（空湖 → (None, None)）。

    给「该拉哪一段」类逻辑用（跨源对拍窗口、覆盖度报告）。
    走 parquet 统计做 min/max 下推，不物化数据。
    """
    root = _root() / "daily"
    if not _has_parquet(root):
        return None, None
    try:
        row = (
            pl.scan_parquet(str(root / "**" / "*.parquet"))
            .select(pl.col("trade_date").min().alias("lo"),
                    pl.col("trade_date").max().alias("hi"))
            .collect()
            .row(0)
        )
    except Exception:  # noqa: BLE001 - 读湖失败不阻断调用方的降级路径
        return None, None
    return row[0], row[1]


def latest_trade_date() -> date | None:
    return daily_range()[1]


def latest_top_by_amount(limit: int = 200) -> list[str]:
    """最近交易日成交额 top N 的标的（新闻/看板活跃池）。空湖 → []。"""
    lo, hi = daily_range()
    if hi is None:
        return []
    df = (
        read_daily(start=hi, end=hi)
        .select("symbol", "amount")
        .collect()
    )
    if not len(df):
        return []
    return (
        df.sort("amount", descending=True, nulls_last=True)
        .head(limit)["symbol"].to_list()
    )


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
        # 同key覆盖：读旧 → 覆盖合并 → 原子写（锁按文件粒度）
        with _file_lock(p):
            if p.exists():
                g = _overlay(pl.read_parquet(p), g, ["symbol", "ts"])
            g = g.sort(["symbol", "ts"]) if "ts" in g.columns else g
            _atomic_write_parquet(g, p)
        out.append(p)
    return out


def read_minute(symbols: list[str] | None = None, freq: str = "60min",
                start=None, end=None) -> pl.LazyFrame:
    root = _root() / "minute" / f"freq={freq}"
    if not _has_parquet(root):
        return _empty_frame("minute_bar")
    lf = pl.scan_parquet(str(root / "**" / "*.parquet"))
    if symbols:
        lf = lf.filter(pl.col("symbol").is_in(symbols))
    if start:
        lf = lf.filter(pl.col("ts") >= start)
    if end:
        lf = lf.filter(pl.col("ts") <= end)
    return lf
