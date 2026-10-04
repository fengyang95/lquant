"""大盘看板数据的缺失检查与补齐。

设计动机：close 调度（15:05）错跑/失败（服务重启、源站故障）会让 index_daily
永久缺那一天 —— 指数日线可回溯，缺了就补。sector/sentiment 等快照类数据
不可回溯，只报告覆盖状态、不假装能补。

入口：
- ensure_market_coverage(days) —— 检查 + 补齐，返回报告 dict。
- 由 sync 作业 kind="backfill"（盘前 09:10）与 API POST /market/backfill 触发；
  API 服务启动线程也会跑一次（部署后自动检查缺失）。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.core.types import now_cn
from lquant.market.collectors.index_daily import INDEX_POOL

__all__ = ["missing_trade_dates", "ensure_market_coverage", "backfill_index_history",
           "DEFAULT_LOOKBACK_DAYS"]

DEFAULT_LOOKBACK_DAYS = 90


def missing_trade_dates(have: set[date], calendar: list[date]) -> list[date]:
    """交易日历中有、库里没有的日期，升序。"""
    return sorted(set(calendar) - set(have))


def _target_provider():
    from lquant.data.providers import get_provider

    provider = get_provider()
    return provider.providers[0] if hasattr(provider, "providers") else provider


def _trade_calendar(start: date, end: date) -> list[date]:
    """[start, end] 内的交易日，升序。日历不可用时抛 RuntimeError。"""
    df = _target_provider().trade_calendar(start, end)
    if df is None or df.is_empty():
        raise RuntimeError(f"交易日历不可用（provider 未返回数据: {start}~{end}）")
    if "trade_date" not in df.columns or "is_open" not in df.columns:
        raise RuntimeError(f"交易日历列异常: {df.columns}")
    out = [
        d
        for d, is_open in zip(df["trade_date"].to_list(),
                              df["is_open"].to_list(), strict=True)
        if int(is_open) == 1 and start <= d <= end
    ]
    return sorted(out)


def _index_have() -> dict[str, set[date]]:
    """index_daily 各 symbol 已有的日期集合。"""
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT symbol, trade_date FROM index_daily"
        ).fetchall()
    out: dict[str, set[date]] = {}
    for symbol, d in rows:
        out.setdefault(str(symbol), set()).add(d)
    return out


def _persist_index(df: pl.DataFrame) -> int:
    from lquant.market.scheduler import persist

    counts = persist({"index_daily": df})
    return int(counts.get("index_daily", 0))


def ensure_market_coverage(days: int = DEFAULT_LOOKBACK_DAYS, *,
                           demo: bool = False) -> dict:
    """检查 index_daily 缺失的交易日并补齐；快照类表只报告状态。

    返回 {table, days, missing_before, fetched, persisted, missing_after,
          snapshots}，missing_* 为 {symbol: [缺失日...]}。
    """
    days = max(1, min(int(days), 365))
    today = now_cn().date()  # 项目时区（Asia/Shanghai），避免非 CN 主机日期漂移
    start = today - timedelta(days=days)
    calendar = _trade_calendar(start, today)
    # 今日盘中/未收盘时今天的日线尚不存在，不算缺失（close 调度负责今天）
    historical = [d for d in calendar if d < today]
    if not historical:
        historical = calendar

    have = _index_have()
    missing = {
        sym: missing_trade_dates(have.get(sym, set()), historical)
        for sym in INDEX_POOL
    }
    missing_before = {k: v for k, v in missing.items() if v}

    fetched = persisted = 0
    if missing_before:
        earliest = min(d for dates in missing_before.values() for d in dates)
        df = _fetch_index(start=earliest, end=today.isoformat(), demo=demo)
        fetched = len(df)
        if len(df):
            persisted = _persist_index(df)

    have_after = _index_have()
    missing_after = {
        sym: missing_trade_dates(
            have_after.get(sym, set()) & set(historical), historical)
        for sym in INDEX_POOL
    }
    missing_after = {k: v for k, v in missing_after.items() if v}

    return {
        "table": "index_daily",
        "days": days,
        "missing_before": missing_before,
        "fetched": fetched,
        "persisted": persisted,
        "missing_after": missing_after,
        "snapshots": _snapshot_coverage(),
    }


def _fetch_index(start: date, end: str, *, demo: bool) -> pl.DataFrame:
    from lquant.market.collectors import fetch_index_daily

    return fetch_index_daily(start=start.isoformat(), end=end, demo=demo)


def backfill_index_history(
    start: date,
    end: date,
    *,
    symbols: list[str] | None = None,
    demo: bool = False,
) -> dict:
    """把 [start, end] 的指数日线补齐进 ``index_daily``（回测基准的历史来源）。

    与 ``ensure_market_coverage`` 的分工：后者是**近端补齐**（默认 90 天，
    交给盘前调度兜底漏采），本函数是**历史回填**（首次建基准或扩窗口时
    一次性拉多年）。指数可回溯，因此可以安全重跑 —— upsert 幂等。

    symbols 缺省用 ``INDEX_POOL``；传入后只回填这些标的（如单独补 000300.SH）。
    """
    from loguru import logger

    only = list(symbols) if symbols else list(INDEX_POOL)
    unknown = [s for s in only if s not in INDEX_POOL]
    if unknown:
        # 不在池里的指数名映射不到中文名，落库后 name 为 NULL，看板会显示裸代码。
        # 不阻断（调用方可能就是想补冷门指数），但要显式告知。
        logger.warning(f"以下指数不在 INDEX_POOL，name 将为空: {unknown}")

    before = _index_have()
    have_in_window = {
        sym: {d for d in before.get(sym, set()) if start <= d <= end}
        for sym in only
    }
    df = _fetch_index(start=start, end=end.isoformat(), demo=demo)
    fetched = len(df)
    persisted = 0
    if fetched:
        if symbols:
            df = df.filter(pl.col("symbol").is_in(only))
        persisted = _persist_index(df) if len(df) else 0

    after = _index_have()
    returned = {
        "symbols": only,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fetched": fetched,
        "persisted": persisted,
        "coverage_before": {k: len(v) for k, v in have_in_window.items()},
        "coverage_after": {
            k: len({d for d in after.get(k, set()) if start <= d <= end})
            for k in only
        },
    }
    return returned


def _snapshot_coverage() -> dict:
    """快照类看板表（不可回溯）的覆盖状态，仅报告。"""
    from lquant.core.db import reader

    out = {}
    with reader() as con:
        for table in ("sector_daily", "sentiment_daily"):
            try:
                row = con.execute(
                    f"SELECT MIN(trade_date), MAX(trade_date), "
                    f"COUNT(DISTINCT trade_date) FROM {table}").fetchone()
            except Exception:  # noqa: BLE001  表可能未建
                row = None
            out[table] = {
                "min_date": str(row[0]) if row and row[0] else None,
                "max_date": str(row[1]) if row and row[1] else None,
                "days": row[2] if row else 0,
            }
    return out