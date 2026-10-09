"""扩展数据 parquet 存储与读取（PIT 安全的分区形状）。

分区布局（``<数据根>/ext/<table_id>/``）::

    config.json                       # 表定义（store.py）
    part.parquet                      # mode=snapshot：只有「最新值」
    date=2024-01-02/part.parquet      # mode=timeseries：按交易日分区

为什么 timeseries 必须按日分文件、而不是一张大表里放 date 列：
- **幂等回补**靠分区存在性判断（已有分区直接跳过），一张大表做不到；
- 写入是「读旧 → 合并去重 → 原子替换」，分区粒度让单日重写不碰其它日期。

日期列统一落 ``pl.Date``（名为 ``config.date_field``，默认 ``date``）：
PIT 对齐要跟日线面板的 ``trade_date``（Date）做等值 join，字符串日期会 join
不上；而 join 不上在 polars 里是**静默出 null**，看起来就像「这票当日没数据」。
"""

from __future__ import annotations

import os
from datetime import date as _date
from pathlib import Path

import polars as pl

from lquant.data.ext.models import ExtConfig, ExtConfigError
from lquant.data.ext.schema import normalize_symbol_column, polars_dtype
from lquant.data.ext.store import table_dir


def snapshot_path(config: ExtConfig, data_root: str | Path | None = None) -> Path:
    return table_dir(config.id, data_root) / "part.parquet"


def partition_dir(config: ExtConfig, day: _date, data_root: str | Path | None = None) -> Path:
    return table_dir(config.id, data_root) / f"date={day.isoformat()}"


def partition_path(config: ExtConfig, day: _date, data_root: str | Path | None = None) -> Path:
    return partition_dir(config, day, data_root) / "part.parquet"


def existing_dates(config: ExtConfig, data_root: str | Path | None = None) -> list[str]:
    """已落盘的 timeseries 分区日期（升序 ISO 字符串）。

    回补的幂等判据就是它：`date=...` 目录存在即视为已补，重复拉取不产生重复行。
    """
    root = table_dir(config.id, data_root)
    if not root.exists():
        return []
    out = [
        d.name[5:]
        for d in root.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").exists()
    ]
    return sorted(out)


def _atomic_write_parquet(df: pl.DataFrame, path: Path) -> None:
    """临时文件 + os.replace：读方不会看到半截 parquet。

    半截文件比「没写入」更糟：下一次写入的「合并旧数据」会读到损坏文件，
    若把异常吞掉就变成静默丢弃历史。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _cast_to_schema(df: pl.DataFrame, config: ExtConfig) -> pl.DataFrame:
    """按字段类型强转。

    ``strict=True``（polars 默认）会把「字符串列里混了 'x'」这类脏值显式抛出来 ——
    这正是我们要的：一次显式失败远好过把它读成 null 后算出「看起来合理」的因子。
    """
    for f in config.fields:
        if f.name not in df.columns:
            raise ExtConfigError(
                f"扩展表 {config.id} 缺少声明字段 {f.name!r}（实际列: {df.columns}）"
            )
        if f.name == config.date_field:
            continue  # 日期列单独按 Date 口径处理（见 _attach_date）
        target = polars_dtype(f.dtype)
        try:
            df = df.with_columns(pl.col(f.name).cast(target))
        except Exception as e:  # noqa: BLE001 - 转成带字段名的显式错误
            raise ExtConfigError(
                f"扩展表 {config.id} 字段 {f.name!r} 无法转成 {f.dtype}: {e}"
            ) from e
    # 只保留声明的字段 + 标的 + 日期：上游接口常附带一堆无关列，落盘会让
    # schema 随接口版本漂移，也会把 noise 带进 rows 查询结果。
    keep = [f.name for f in config.fields]
    for extra in (config.symbol_field, config.date_field):
        if extra and extra not in keep:
            keep.append(extra)
    return df.select([c for c in keep if c in df.columns])


def _attach_date(df: pl.DataFrame, config: ExtConfig, day: _date) -> pl.DataFrame:
    """写入前固定日期列，并校验数据里自带的日期与目标分区一致。

    为什么 JSON/CSV 写入也要校验（不只 HTTP）：用户上传的 CSV 带日期列时，
    「文件里的日期」和「`--date` 指定的分区」不一致同样是污染 —— 把 6 月的
    数据写进 3 月分区，读侧完全看不出来。宁可拒绝，也不猜用户意图。
    """
    name = config.date_field
    target = pl.lit(day, dtype=pl.Date)
    if name in df.columns:
        # 只在列里真的有值且与目标不符时报错；全 null 视为「没带日期」。
        raw = df.select(pl.col(name).cast(pl.Utf8, strict=False)).to_series().drop_nulls()
        mismatch = sorted({v for v in map(_to_day_label, raw.to_list())
                           if v is not None and v != day.isoformat()})
        if mismatch:
            raise ExtConfigError(
                f"扩展表 {config.id} 数据里的 {name} 与目标分区 {day.isoformat()} 不一致"
                f"（示例 {mismatch[:3]}）—— 拒绝把非当日数据写进该分区"
            )
    return df.with_columns(target.alias(name))


def _to_day_label(value: object) -> str | None:
    """把各种日期写法归一成 ``YYYY-MM-DD``；认不出来返回 None（交给上游报类型错）。

    支持 ISO 日期/时间前缀与紧凑 ``YYYYMMDD``：行情软件两种都常见。
    """
    text = str(value).strip()
    if not text:
        return None
    digits = text[:8]
    if len(text) >= 8 and digits.isdigit() and "-" not in text[:10]:
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def _dedupe_key(config: ExtConfig, df: pl.DataFrame) -> list[str]:
    """分区内去重键。

    market_level 表每天一条：键就是日期 → 同一日重复写入是覆盖语义。
    """
    keys: list[str] = []
    if config.symbol_field and config.symbol_field in df.columns:
        keys.append(config.symbol_field)
    if config.date_field in df.columns:
        keys.append(config.date_field)
    return keys


def write_frame(
    df: pl.DataFrame,
    config: ExtConfig,
    *,
    day: _date | None = None,
    data_root: str | Path | None = None,
) -> int:
    """把一帧数据写入扩展表，返回写入后该分区/快照的行数。

    - snapshot：忽略 day，整表按标的覆盖合并；
    - timeseries：必须给 day（缺省报错，而不是偷偷用今天 —— 写错分区是污染）。
    """
    if df.is_empty():
        raise ExtConfigError(f"扩展表 {config.id}: 空数据不写入（无行可落盘）")

    if config.symbol_field:
        df = normalize_symbol_column(df, config.symbol_field)

    if config.mode == "snapshot":
        path = snapshot_path(config, data_root)
        df = _cast_to_schema(df, config)
        keep = [c for c in df.columns if c != config.date_field]
        df = df.select(keep)
        if path.exists():
            old = pl.read_parquet(path)
            df = pl.concat([old, df], how="diagonal_relaxed")
        keys = [config.symbol_field] if config.symbol_field in df.columns else None
        # 市场级快照没有去重键，只保留最后一行 = 「最新值」
        df = df.unique(subset=keys, keep="last") if keys else df.tail(1)
        _atomic_write_parquet(df, path)
    else:
        if day is None:
            raise ExtConfigError(
                f"扩展表 {config.id} 是 timeseries 模式，写入必须指定日期（--date）"
            )
        df = _attach_date(df, config, day)
        df = _cast_to_schema(df, config)
        path = partition_path(config, day, data_root)
        if path.exists():
            old = pl.read_parquet(path)
            df = pl.concat([old, df], how="diagonal_relaxed")
        keys = _dedupe_key(config, df)
        df = df.unique(subset=keys, keep="last") if keys else df.tail(1)
        df = df.sort(keys) if keys else df
        _atomic_write_parquet(df, path)

    from lquant.data.ext.store import invalidate_ext_caches

    invalidate_ext_caches(data_root)
    return df.height


def read_table(
    config: ExtConfig,
    *,
    day: _date | str | None = None,
    start_date: _date | str | None = None,
    end_date: _date | str | None = None,
    data_root: str | Path | None = None,
) -> tuple[pl.DataFrame, str | None]:
    """读扩展表，返回 ``(帧, 生效日期标签)``。

    - snapshot：整表，``active=None``。日期范围请求显式报错（快照没有历史）。
    - timeseries + day：单分区。
    - timeseries + start/end：区间内全部分区（diagonal 合并，容忍中途加字段）。
    - timeseries 缺省：最新分区（与 ``/rows`` 的默认语义一致）。
    """
    _day = _coerce_date(day, field="date")
    _start = _coerce_date(start_date, field="start_date")
    _end = _coerce_date(end_date, field="end_date")

    if config.mode == "snapshot":
        if _start or _end:
            raise ExtConfigError("snapshot 模式的表没有历史分区，不支持日期范围查询")
        path = snapshot_path(config, data_root)
        if not path.exists():
            return pl.DataFrame(), None
        return pl.read_parquet(path), None

    dates = existing_dates(config, data_root)
    if not dates:
        return pl.DataFrame(), None
    if _day is not None:
        label = _day.isoformat()
        path = partition_path(config, _day, data_root)
        if not path.exists():
            return pl.DataFrame(), label
        return _ensure_partition_date(pl.read_parquet(path), config, label), label

    if _start or _end:
        lo = _start.isoformat() if _start else None
        hi = _end.isoformat() if _end else None
        if lo and hi and lo > hi:
            raise ExtConfigError(f"start_date {lo} 晚于 end_date {hi}")
        selected = [d for d in dates if (not lo or d >= lo) and (not hi or d <= hi)]
        if not selected:
            return pl.DataFrame(), None
        frames = []
        for d in selected:
            f = pl.read_parquet(partition_path(config, _d(d), data_root))
            frames.append(_ensure_partition_date(f, config, d))
        df = pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]
        return df, f"{selected[0]}..{selected[-1]}"

    latest = dates[-1]
    f = pl.read_parquet(partition_path(config, _d(latest), data_root))
    return _ensure_partition_date(f, config, latest), latest


def delete_table_data(config: ExtConfig, data_root: str | Path | None = None) -> None:
    """删除一张表的数据（保留 config.json）。"""
    import shutil

    root = table_dir(config.id, data_root)
    if not root.exists():
        return
    for child in root.iterdir():
        if child.name == "config.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    from lquant.data.ext.store import invalidate_ext_caches

    invalidate_ext_caches(data_root)


def _ensure_partition_date(df: pl.DataFrame, config: ExtConfig, label: str) -> pl.DataFrame:
    """老/外部文件缺日期列时，用分区目录名补上。

    分区目录名本身就是权威日期（写入时就固定了），从它恢复比让下游
    join 静默出 null 更安全。
    """
    if config.date_field in df.columns:
        return df
    return df.with_columns(pl.lit(label).str.to_date().alias(config.date_field))


def _coerce_date(value: _date | str | None, *, field: str) -> _date | None:
    if value is None or isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(str(value).strip())
    except ValueError as e:
        # 这个值会拼进 `date=<value>` 目录名，非法值不只是格式问题：
        # `date=../../daily` 会让读取路径离开扩展数据区域。
        raise ExtConfigError(f"{field} 不是合法日期: {value!r}") from e


def _d(iso: str) -> _date:
    return _date.fromisoformat(iso)
