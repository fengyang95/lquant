"""lquant parquet 日线湖 → qlib 二进制数据 导出器。

产出 qlib 标准目录结构（pyqlib `qlib.init(provider_uri=...)` 直接可用）：

    <out>/calendars/day.txt            交易日历（每行一个 YYYY-MM-DD）
    <out>/instruments/all.txt          标的清单：SYM\tstart\tend
    <out>/features/<SYM>/<field>.day.bin
    <out>/qlib_export_meta.json        导出清单（参数与统计）

bin 格式（与 qlib scripts/dump_bin.py 一致）：float32 小端一维数组，
首元素是该标的**首个有效日在日历中的下标**，其余按日历顺序对齐的值，
中间缺失日为 NaN。

价格口径（重要）：
- 湖内 OHLC 是不复权价；导出时按 adj_factor 归一化到「最新一天 = 不复权」
  （即前复权到最新），open/high/low/close/vwap 均为复权后序列；
- `$factor` = adj_factor / adj_factor_latest（不复权价 = 复权价 / factor）；
- `$vwap` = amount / volume（复权同样乘 f），volume<=0（停牌）置 NaN；
  qlib Alpha158 依赖 $vwap，导出默认包含。
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from lquant.data.store.parquet import read_daily

DEFAULT_FIELDS: tuple[str, ...] = (
    "open", "high", "low", "close", "volume", "amount", "vwap", "factor",
)
#: 湖里可额外导出的列（直接取值，不做复权处理）
OPTIONAL_FIELDS: tuple[str, ...] = (
    "turnover_rate", "total_mv", "float_mv", "pe_ttm", "pb_mrq", "ps_ttm", "pct_chg",
)
ALL_FIELDS = DEFAULT_FIELDS + OPTIONAL_FIELDS

_SUFFIX_TO_PREFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def to_qlib_symbol(symbol: str) -> str:
    """`600000.SH` → `SH600000`（qlib 惯例命名，也用作 features 目录名）。"""
    code, _, suffix = symbol.partition(".")
    sfx = suffix.upper()
    if not code or sfx not in _SUFFIX_TO_PREFIX:
        raise ValueError(f"无法映射到 qlib symbol：{symbol!r}（期望 <code>.SH/.SZ/.BJ）")
    return f"{sfx}{code}"


def from_qlib_symbol(qlib_symbol: str) -> str:
    """`SH600000` → `600000.SH`。"""
    sfx, code = qlib_symbol[:2].upper(), qlib_symbol[2:]
    if sfx not in _SUFFIX_TO_PREFIX or not code:
        raise ValueError(f"无法还原 qlib symbol：{qlib_symbol!r}")
    return f"{code}.{sfx}"


def _adjusted(df: pl.DataFrame) -> pl.DataFrame:
    """按标的把 raw OHLC 调成「前复权到最新」，并算出 $factor / $vwap。

    adj_factor 为 null 的行沿用该标的最近一个有效因子（bfill 语义）；
    全程无因子的标的 factor=1（等价于不调整）。
    """
    df = df.sort(["symbol", "trade_date"])
    df = df.with_columns(
        # 每标的最近有效因子：倒排后 forward fill
        pl.col("adj_factor").reverse().forward_fill().reverse().over("symbol")
    ).with_columns(
        pl.col("adj_factor").fill_null(1.0)
    )
    latest = df.group_by("symbol").agg(pl.col("adj_factor").last().alias("_f_last"))
    df = df.join(latest, on="symbol", how="left").with_columns(
        f=(pl.col("adj_factor") / pl.col("_f_last"))
    )
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df = df.with_columns((pl.col(col) * pl.col("f")).alias(col))
    if "volume" in df.columns and "amount" in df.columns:
        df = df.with_columns(
            pl.when(pl.col("volume") > 0)
            .then((pl.col("amount") / pl.col("volume")) * pl.col("f"))
            .otherwise(None)
            .alias("vwap")
        )
    return df.rename({"f": "factor"})


def export(
    out_dir: str | Path,
    start: str | date | None = None,
    end: str | date | None = None,
    symbols: list[str] | None = None,
    sec_types: list[str] | None = None,
    fields: list[str] | None = None,
    top: int | None = None,
    benchmark: str | None = "000300.SH",
) -> dict:
    """导出日线湖到 qlib 目录。返回 manifest dict（也写入 <out>/qlib_export_meta.json）。

    参数：
    - out_dir：qlib 数据根目录（provider_uri）
    - start/end：日期窗口（含端点）
    - symbols：标的白名单（lquant 形态 `600000.SH`）
    - sec_types：sec_type 过滤，默认仅 `stock`（湖里混入 ETF 会污染股票池）
    - fields：导出字段，默认 DEFAULT_FIELDS；可选 OPTIONAL_FIELDS
    - top：若给 N，额外产出 instruments/top{N}.txt（按导出窗口末日 float_mv
      取前 N，作为流动性代理池；float_mv 缺失的标的按 total_mv 兜底）
    - benchmark：考核基准指数（lquant 形态，默认 `000300.SH`）。指数**不在
      日线湖里**（点位不是价格），单独从 DuckDB `index_daily` 读出并写成
      `features/SH000300/$close.day.bin` + 登记进 instruments，qlib 才能算
      超额收益。`None` = 不导出基准（workflow 侧 benchmark 也必须关掉）。
    """
    out = Path(out_dir)
    fields = list(fields) if fields else list(DEFAULT_FIELDS)
    bad = [f for f in fields if f not in ALL_FIELDS]
    if bad:
        raise ValueError(f"未知字段：{bad}（可选：{list(ALL_FIELDS)}）")
    need_base = {"open", "high", "low", "close", "volume", "amount", "factor", "vwap"} & set(fields)
    # 复权处理需要 adj_factor + OHLCV，无论 fields 是否全含
    read_cols = ["symbol", "trade_date", "sec_type", "adj_factor", "is_suspended"]
    read_cols += sorted(need_base - {"factor", "vwap"})
    read_cols += [f for f in fields if f in OPTIONAL_FIELDS]
    if top:  # 流动性排名需要市值列，即使不导出
        read_cols += ["float_mv", "total_mv"]
    read_cols = list(dict.fromkeys(read_cols))

    lf = read_daily(symbols=symbols, start=start, end=end)
    lf = lf.select(read_cols)
    if sec_types is None:
        sec_types = ["stock"]
    if sec_types:
        lf = lf.filter(pl.col("sec_type").is_in(sec_types))
    df = lf.collect()
    if df.is_empty():
        raise ValueError("湖内无可导出的日线数据（检查 LQ_ROOT/日期窗口/sec_type 过滤）")

    df = _adjusted(df) if (need_base & {"open", "high", "low", "close", "vwap"} or "factor" in fields) else df
    if "vwap" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("vwap"))

    bench_df = _load_benchmark_frame(benchmark, start=start, end=end)

    # 日历 = 股票日 ∪ 基准日：基准若比股票多出交易日（例如股票池在窗口内
    # 全部缺失），漏掉这些天会让 qlib 的基准序列错位。
    all_dates = set(df["trade_date"].unique().to_list())
    if bench_df is not None:
        all_dates |= set(bench_df["trade_date"].unique().to_list())
    dates = sorted(all_dates)
    date_idx = {d: i for i, d in enumerate(dates)}

    (out / "calendars").mkdir(parents=True, exist_ok=True)
    (out / "instruments").mkdir(parents=True, exist_ok=True)
    (out / "features").mkdir(parents=True, exist_ok=True)
    (out / "calendars" / "day.txt").write_text(
        "".join(d.strftime("%Y-%m-%d") + "\n" for d in dates), encoding="utf-8"
    )

    export_fields = list(fields)
    n_bins = 0
    instruments: list[tuple[str, str, str]] = []
    top_rows: list[tuple[str, float | None]] = []

    def _write_one(g: pl.DataFrame, *, allow_missing: bool = False) -> int:
        """把单标的长表写成 features/<QSYM>/<field>.day.bin，返回 bin 数。"""
        nonlocal n_bins
        sym = g["symbol"][0]
        qsym = to_qlib_symbol(sym)
        feat_dir = out / "features" / qsym
        feat_dir.mkdir(parents=True, exist_ok=True)
        gidx = np.array([date_idx[d] for d in g["trade_date"].to_list()], dtype=np.int64)
        start_i = int(gidx.min())
        span = int(gidx.max()) - start_i + 1
        instruments.append((qsym, dates[start_i].strftime("%Y-%m-%d"),
                            dates[int(gidx.max())].strftime("%Y-%m-%d")))
        last = g.tail(1)
        mv = last["float_mv"][0] if "float_mv" in last.columns else None
        if not mv or (isinstance(mv, float) and math.isnan(float(mv))):
            mv = last["total_mv"][0] if "total_mv" in last.columns else None
        top_rows.append((qsym, None if mv is None or (
            isinstance(mv, float) and math.isnan(float(mv))) else float(mv)))
        written = 0
        for f in export_fields:
            if f not in g.columns:
                if not allow_missing:
                    continue
                vals = np.full(span, np.nan, dtype="<f4")
            else:
                vals = np.full(span, np.nan, dtype="<f4")
                raw = g[f].cast(pl.Float64, strict=False).fill_nan(None).to_numpy()
                vals[gidx - start_i] = raw
            np.concatenate(([np.float32(start_i)], vals)).astype("<f4").tofile(
                feat_dir / f"{f}.day.bin"
            )
            written += 1
        n_bins += written
        return written

    df = df.sort(["symbol", "trade_date"])
    for g in df.partition_by("symbol"):
        _write_one(g)

    bench_symbol = None
    if bench_df is not None and len(bench_df):
        bench_symbol = to_qlib_symbol(str(bench_df["symbol"][0]))
        _write_one(bench_df.sort("trade_date"), allow_missing=True)

    instruments.sort()
    (out / "instruments" / "all.txt").write_text(
        "".join(f"{s}\t{a}\t{b}\n" for s, a, b in instruments), encoding="utf-8"
    )
    if top and top > 0:
        rows = [(s, m) for s, m in top_rows if m is not None]
        rows.sort(key=lambda x: (-x[1], x[0]))
        seg = {s: (a, b) for s, a, b in instruments}
        (out / "instruments" / f"top{top}.txt").write_text(
            "".join(f"{s}\t{seg[s][0]}\t{seg[s][1]}\n" for s, _ in rows[:top]),
            encoding="utf-8",
        )

    manifest = {
        "out_dir": str(out),
        "start": dates[0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "calendar_days": len(dates),
        "instruments": len(instruments),
        "fields": export_fields,
        "sec_types": sec_types,
        "bins": n_bins,
        "price_basis": "adj_factor 归一化到最新一日（复权价 = raw × factor/factor_latest）",
        # 基准来自 DuckDB index_daily（不入 parquet 湖）；workflow 的
        # benchmark 必须用同一个 qlib symbol，否则超额收益会算在别的标的上。
        "benchmark": bench_symbol,
        "benchmark_symbol_lquant": str(benchmark) if bench_symbol else None,
    }
    (out / "qlib_export_meta.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _load_benchmark_frame(benchmark: str | None, *, start=None, end=None) -> pl.DataFrame | None:
    """从 DuckDB ``index_daily`` 读基准指数 → qlib 导出形状的长表。

    指数与个股的区别：**不复权**（factor 恒 1.0）、不进 parquet 湖、无
    turnover_rate/市值等列。写进 qlib 的字段只有 close 是必需的（qlib 用
    ``$close`` 算超额），其余默认字段补 NaN 保持 shape 一致。
    读不到数据时返回 None（导出照常完成，manifest 里 benchmark=null）。
    """
    import polars as pl

    if not benchmark:
        return None
    from datetime import date as _date

    from lquant.backtest.benchmark import load_index_series

    s = _date.fromisoformat(str(start)) if start else None
    e = _date.fromisoformat(str(end)) if end else None
    series = load_index_series(benchmark, start=s, end=e)
    if len(series) < 2:
        return None
    return pl.DataFrame({
        "trade_date": [d for d, _ in series],
        "symbol": [benchmark] * len(series),
        "open": [c for _, c in series],
        "high": [c for _, c in series],
        "low": [c for _, c in series],
        "close": [c for _, c in series],
        "volume": [None] * len(series),
        "amount": [None] * len(series),
        "vwap": [c for _, c in series],
        "factor": [1.0] * len(series),
    }, schema_overrides={
        "trade_date": pl.Date, "symbol": pl.Utf8,
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
        "vwap": pl.Float64, "factor": pl.Float64,
    })


def check(out_dir: str | Path) -> dict:
    """结构自检：日历有序、instruments 与 features 目录一致、bin 可回读。"""
    out = Path(out_dir)
    problems: list[str] = []
    cal_p = out / "calendars" / "day.txt"
    if not cal_p.exists():
        raise FileNotFoundError(f"{cal_p} 不存在，先跑 lq qlib export")
    days = cal_p.read_text().split()
    if days != sorted(days) or len(set(days)) != len(days):
        problems.append("calendars/day.txt 无序或含重复日期")
    ins_p = out / "instruments" / "all.txt"
    lines = ins_p.read_text().strip().splitlines() if ins_p.exists() else []
    if not lines:
        problems.append("instruments/all.txt 为空")
    feat_dirs = {p.name for p in (out / "features").iterdir() if p.is_dir()} if (out / "features").exists() else set()
    ins_syms = {ln.split("\t")[0] for ln in lines if ln.strip()}
    if ins_syms - feat_dirs:
        problems.append(f"instruments 有 {len(ins_syms - feat_dirs)} 只无 features 目录")
    if feat_dirs - ins_syms:
        problems.append(f"features 有 {len(feat_dirs - ins_syms)} 只不在 instruments 里")
    # 抽样回读一个 bin
    sample_ok = None
    for d in sorted(feat_dirs)[:1]:
        for b in sorted((out / "features" / d).glob("*.bin"))[:1]:
            arr = np.fromfile(b, dtype="<f4")
            start_i = int(arr[0])
            if start_i < 0 or start_i >= len(days) and arr.size > 1:
                problems.append(f"{b.name} 起始下标越界")
            sample_ok = {"symbol": d, "file": b.name, "values": max(arr.size - 1, 0)}
    meta_p = out / "qlib_export_meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
    bench = (meta or {}).get("benchmark")
    if bench:
        # 基准必须真的落到 features 里：qlib 找不到 benchmark 标的不报错，
        # 只在回测里静默退化成「无超额收益」—— 那正是 Phase 1.3 要消掉的坑。
        if bench not in feat_dirs:
            problems.append(f"manifest 声明 benchmark={bench} 但 features/{bench} 不存在")
        elif not (out / "features" / bench / "close.day.bin").exists():
            problems.append(f"features/{bench}/close.day.bin 缺失（qlib 用 $close 算超额）")
        elif bench not in ins_syms:
            problems.append(f"benchmark {bench} 不在 instruments/all.txt 里")
    return {
        "days": len(days),
        "instruments": len(lines),
        "features_dirs": len(feat_dirs),
        "benchmark": bench,
        "sample": sample_ok,
        "meta": meta,
        "problems": problems,
    }


def _read_bin(path: str | Path) -> tuple[int, np.ndarray]:
    """读 qlib bin：返回 (日历起始下标, 值数组)。测试与自检用。"""
    arr = np.fromfile(path, dtype="<f4")
    return int(arr[0]), arr[1:]
