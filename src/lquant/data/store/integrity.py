"""数据湖完整性：数据根自检 + 分区连续性。

这两类问题此前都是**静默**的（2026-10-03 数据模块审计）：

1. **影子湖** —— `parquet_dir` 被解析成 `<x>/parquet/parquet`（`LQ_DATA_DIR`
   已含 `/parquet` 时 `config/app.yaml` 又拼了一次），或进程 CWD 不同导致
   `./data/parquet` 落到别处。于是写入进的是另一棵树，两条路径**各自报成功**，
   真实的湖少掉的年份没有任何人发现。
2. **整年分区缺失** —— `read_daily` 用 `daily/**/*.parquet` glob，缺一年就是
   静默少一年；`latest_trade_date()` 返回最近日期，看起来依然健康；
   `scan_coverage` 只看近端窗口（默认 5~30 天），永远看不到历史空洞。
   实测真实湖整整缺了 2025 年（约 124 万行 / 5,143 只标的）。

设计口径：
- 只做**只读**检查，绝不写回湖内数据（lake 检查的既有约定）。
- 日历/参考表缺失时降级跳过，检查自身不能成为同步链路的单点故障。
- 「历史起点之前没有数据」不算问题（那是覆盖策略），只报**区间内部的空洞**
  与**年份内部被截断** —— 这两类一定是 bug。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "data_root",
    "nested_lake_reason",
    "find_lake_roots",
    "check_data_root",
    "check_partition_continuity",
]

# 一个目录里出现这些名字的子目录，说明它是一棵「湖」的根
_LAKE_MARKERS = ("daily", "minute", "daily_basic")


def data_root() -> Path:
    """当前生效的 parquet 根（与 store.parquet._root() 同一来源）。"""
    from lquant.core.config import get_settings

    return Path(get_settings().parquet_dir)


def nested_lake_reason(parquet_dir: Path | str | None = None) -> str | None:
    """**廉价**的路径形状自检（不扫盘）：可疑时返回原因，正常返回 None。

    只依赖路径本身与父目录的直接子目录名，可在每次取数据根时调用。
    """
    p = Path(parquet_dir) if parquet_dir is not None else data_root()
    parent = p.parent
    # <x>/parquet/parquet —— LQ_DATA_DIR 已含 /parquet 时的双重拼接
    if p.name == "parquet" and parent.name == "parquet":
        return (
            f"parquet_dir={p} 的父目录同名 parquet —— LQ_DATA_DIR 疑似已包含 "
            f"`/parquet`，config/app.yaml 又拼了一次；写入会落到影子湖，"
            f"而两条路径各自报成功"
        )
    # 父目录本身就是一棵湖（含 daily/ 等），说明自己被嵌套在另一棵湖里
    try:
        for marker in _LAKE_MARKERS:
            if (parent / marker).is_dir():
                return f"parquet_dir={p} 嵌套在另一棵数据湖 {parent} 之内 —— 同一份数据会有两套真相"
    except OSError:  # pragma: no cover - 权限/IO 异常不应让检查崩掉
        return None
    return None


def find_lake_roots(root: Path | str, *, max_depth: int = 4) -> list[Path]:
    """在 `root` 下找出所有「湖根」目录（含 `year=*` 分区的 daily/minute/...）。

    深度受限的有界扫描；符号链接不跟随（避免绕进别处或成环）。
    """
    root = Path(root)
    out: list[Path] = []
    if not root.is_dir():
        return out
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        if d.name in _LAKE_MARKERS and any(
            e.is_dir() and e.name.startswith("year=") for e in entries
        ):
            out.append(d)
            continue  # 已是湖根，不必再深入
        if depth >= max_depth:
            continue
        for e in entries:
            if e.is_dir() and not e.is_symlink() and not e.name.startswith("."):
                stack.append((e, depth + 1))
    return sorted(out)


def check_data_root(parquet_dir: Path | str | None = None) -> list:
    """数据根自检：形状可疑 + 同一数据目录下存在多棵湖。

    返回 Issue 列表（fatal：写错树是数据正确性问题，不是提示）。
    """
    from lquant.data.quality.issues import Issue

    p = Path(parquet_dir) if parquet_dir is not None else data_root()
    found: list = []

    reason = nested_lake_reason(p)
    if reason:
        found.append(
            Issue(rule="DATA_ROOT_NESTED", severity="fatal", dataset="lake", detail=reason, count=1)
        )

    # 全量扫描：同一 data 目录下若出现多棵同名湖，就是「影子湖」
    daily_roots = [r for r in find_lake_roots(p.parent) if r.name == "daily"]
    canonical = p / "daily"
    shadows = [r for r in daily_roots if r.resolve() != canonical.resolve()]
    if shadows:
        found.append(
            Issue(
                rule="SHADOW_LAKE",
                severity="fatal",
                dataset="lake",
                detail=(
                    f"数据根 {p.parent} 下存在 {len(daily_roots)} 棵日线湖，"
                    f"生效的是 {canonical}，另有："
                    + "; ".join(str(s) for s in shadows[:5])
                    + " —— 两条路径各自报成功，读取口径可能取错树"
                ),
                count=len(shadows),
            )
        )
    return found


def _partition_stats(path: Path) -> tuple[date | None, date | None, int] | None:
    """单个分区文件的 (min_date, max_date, rows)；读不动返回 None。"""
    import polars as pl

    try:
        r = (
            pl.scan_parquet(str(path))
            .select(
                pl.col("trade_date").min().alias("lo"),
                pl.col("trade_date").max().alias("hi"),
                pl.len().alias("n"),
            )
            .collect()
        )
    except Exception as e:  # noqa: BLE001 - 坏文件不该让整个检查崩掉
        log.warning(f"分区读取失败，跳过 {path}: {e}")
        return None
    if not len(r):
        return None
    row = r.row(0)
    return row[0], row[1], int(row[2] or 0)


def check_partition_continuity(
    calendar_days: list[date] | None,
    *,
    parquet_dir: Path | str | None = None,
    today: date | None = None,
) -> list:
    """按年分区与交易日历对账：区间内部缺年 / 年份被截断。

    Args:
        calendar_days: 交易日列表（升序或乱序皆可）。None/空 → 跳过
            （日历缺失不能成为检查链路的单点故障）。
        parquet_dir: 湖根，缺省用当前设置。
        today: 「今天」，用于给最后一年设上界（日历可能已预填未来交易日）。

    只报两类**确定是 bug** 的情况：
    - 湖的年份跨度**内部**整年缺失（如 2021-2024 + 2026 却缺 2025）；
    - 年份分区被截断（首尾交易日明显短于日历）。
    历史起点之前的空白属于覆盖策略，不报。
    """
    from lquant.data.quality.issues import Issue

    if not calendar_days:
        return []

    p = Path(parquet_dir) if parquet_dir is not None else data_root()
    daily = p / "daily"
    if not daily.is_dir():
        return []

    cal = sorted(set(calendar_days))
    cal_by_year: dict[int, list[date]] = {}
    for d in cal:
        cal_by_year.setdefault(d.year, []).append(d)

    files = sorted(daily.glob("year=*/part-*.parquet"))
    if not files:
        return []
    have: dict[int, tuple[date | None, date | None, int]] = {}
    for f in files:
        year_name = f.parent.name
        if not year_name.startswith("year="):
            continue
        try:
            year = int(year_name.split("=", 1)[1])
        except ValueError:
            continue
        st = _partition_stats(f)
        if st is not None:
            have[year] = st

    if not have:
        return []

    cap = today or date.today()
    years = sorted(have)
    first_year, last_year = years[0], years[-1]
    found: list = []

    # 1) 跨度内部整年缺失
    for year in range(first_year, last_year + 1):
        if year in have:
            continue
        days = cal_by_year.get(year)
        if not days:
            continue
        found.append(
            Issue(
                rule="PARTITION_MISSING",
                severity="fatal",
                dataset="daily_bar",
                trade_date=days[0],
                detail=(
                    f"{year} 年分区整年缺失：日历有 {len(days)} 个交易日"
                    f"（{days[0]}~{days[-1]}），但 {daily}/year={year}/ 下没有"
                    f"任何分区文件 —— read_daily 用 glob 读取，缺一年就是静默少一年"
                ),
                count=len(days),
            )
        )

    # 2) 年份内部被截断
    for year in years:
        days = cal_by_year.get(year)
        if not days:
            continue
        lo, hi, n = have[year]
        if lo is None or hi is None:
            continue
        exp_lo, exp_hi = days[0], days[-1]
        if year == last_year:
            exp_hi = min(exp_hi, cap)  # 日历可能预填未来交易日
        if year != first_year and lo > exp_lo:
            miss = sum(1 for d in days if lo > d >= exp_lo)
            found.append(
                Issue(
                    rule="PARTITION_TRUNCATED",
                    severity="error",
                    dataset="daily_bar",
                    trade_date=exp_lo,
                    detail=(
                        f"{year} 年分区起始被截断：实际从 {lo} 开始，"
                        f"日历首个交易日是 {exp_lo}，缺 {miss} 个交易日"
                        f"（{n} 行）"
                    ),
                    count=miss,
                )
            )
        if hi < exp_hi:
            miss = sum(1 for d in days if exp_hi >= d > hi)
            found.append(
                Issue(
                    rule="PARTITION_TRUNCATED",
                    severity="error",
                    dataset="daily_bar",
                    trade_date=hi,
                    detail=(
                        f"{year} 年分区末尾被截断：实际到 {hi} 为止，"
                        f"应到 {exp_hi}，缺 {miss} 个交易日（{n} 行）"
                    ),
                    count=miss,
                )
            )
    return found
