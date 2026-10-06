"""大盘看板数据的缺失检查与补齐。

设计动机：close 调度（15:05）错跑/失败（服务重启、源站故障）会让 index_daily
永久缺那一天 —— 指数日线可回溯，缺了就补。sector/sentiment 等快照类数据
不可回溯，只报告覆盖状态、不假装能补。

**资金流是例外中的例外**：每日横截面采集只有当天，但东财单票接口可回溯
约 120 个交易日，所以 backfill_money_flow_history 能把任意标的的历史补齐
（这也是「随便输一只股票都有资金面」的前提）。

入口：
- ensure_market_coverage(days) —— 检查 + 补齐指数缺口，返回报告 dict。
- 由 sync 作业 kind="backfill"（盘前 09:10）与 API POST /market/backfill 触发；
  API 服务启动线程也会跑一次（部署后自动检查缺失）。
- backfill_money_flow_history(symbols) / purge_demo_flow() —— 由 CLI
  `lq data money-flow` 驱动（网络开销大，不进自动调度）。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.core.types import now_cn
from lquant.market.collectors.index_daily import INDEX_POOL
from lquant.market.schema import SOURCE_REAL

__all__ = ["missing_trade_dates", "ensure_market_coverage", "backfill_index_history",
           "backfill_money_flow_history", "purge_demo_flow", "money_flow_symbols",
           "real_flow_predicate", "DEFAULT_LOOKBACK_DAYS"]

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


# --------------------------------------------------------------- 资金流回填


#: 每攒够这么多只标的落一次库。单票历史约 120 行，攒一批再 upsert
#: 比一只一写快得多，又不会把全市场几百万行都压在内存里。
_FLOW_BATCH = 200

#: 老库没有 source 列，这一代 demo 行只能靠名称前缀认出来
#: （collectors/money_flow._demo_flow 里写死的中文名）。
_LEGACY_DEMO_NAME = "样例%"


def _columns(con, table: str) -> set[str]:
    """表实际有哪些列（表不存在时返回空集）。"""
    try:
        return {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}
    except Exception:  # noqa: BLE001  连接异常时按「没有列」处理
        return set()


def real_flow_predicate(con) -> str:
    """「这一行是真实资金流」的 SQL 条件（分析、覆盖统计、清理共用一份）。

    只引用表里**确实存在**的列：``name`` 是可空列，``source`` 是后加的，
    老库/精简 schema 可能都没有。两个都没有时返回 ``TRUE``
    （无从判断 → 全当真实，宁可在报告里显示也不静默吞掉数据）。
    """
    cols = _columns(con, "money_flow")
    clauses = []
    if "source" in cols:
        clauses.append("coalesce(source, '') <> 'demo'")
    if "name" in cols:
        clauses.append(f"coalesce(name, '') NOT LIKE '{_LEGACY_DEMO_NAME}'")
    return " AND ".join(clauses) or "TRUE"


def purge_demo_flow(*, dry_run: bool = False) -> dict:
    """清掉 ``money_flow`` 里的合成数据，并把老行的 source 补成真实源。

    为什么必须清：demo 采集写进来的行和真实行共用同一个主键
    （trade_date, symbol），其中一部分代码是真实上市公司的
    （200 个 demo 代码里 75 个能对上真实标的），分析会把伪造的净流入
    当成真实资金流评分。所以 demo 行不是「占位」而是**污染**。

    两代 demo 行都要清：
    1. 有 ``source`` 列之后写的：``source = 'demo'``；
    2. 之前写的：只能用 ``name LIKE '样例%'`` 认（当时没有来源标记）。

    剩下的老行（source 为 NULL）来源必是真实采集，补成 'eastmoney'，
    让「来源未知」不再是一个长期状态。
    """
    from lquant.core.db import reader, writer

    with reader() as con:
        real = real_flow_predicate(con)
        has_source = "source" in _columns(con, "money_flow")
        demo_pred = f"NOT ({real})"
        demo_rows = con.execute(
            f"SELECT COUNT(*) FROM money_flow WHERE {demo_pred}").fetchone()[0]
        relabel = 0
        if has_source:
            relabel = con.execute(
                f"SELECT COUNT(*) FROM money_flow WHERE source IS NULL "
                f"AND {real}").fetchone()[0]

    if dry_run:
        return {"dry_run": True, "demo_rows": demo_rows, "relabel_rows": relabel,
                "deleted": 0}

    deleted = 0
    with writer() as con:
        if demo_rows:
            con.execute(f"DELETE FROM money_flow WHERE {demo_pred}")
            deleted = demo_rows
        if relabel:
            con.execute(f"UPDATE money_flow SET source = ? WHERE source IS NULL "
                        f"AND {real}", [SOURCE_REAL])
    return {"dry_run": False, "demo_rows": demo_rows,
            "relabel_rows": relabel, "deleted": deleted}


def _flow_have(symbols: list[str] | None = None) -> dict[str, int]:
    """各标的在 money_flow 里的**非 demo**行数。"""
    from lquant.core.db import reader

    try:
        with reader() as con:
            real = real_flow_predicate(con)
            rows = con.execute(
                f"SELECT symbol, COUNT(*) FROM money_flow WHERE {real} "
                "GROUP BY symbol").fetchall()
    except Exception:  # noqa: BLE001  表可能未建
        rows = []
    counts = {str(s): int(n) for s, n in rows}
    if symbols is None:
        return counts
    return {s: counts.get(s, 0) for s in symbols}


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    """归一 + 去重 + 保序（裸码 600519 与 600519.SH 视为同一只）。"""
    from lquant.core.types import parse_symbol

    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        s = str(raw).strip()
        if not s:
            continue
        try:
            sym = str(parse_symbol(s))
        except ValueError:
            sym = s
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def backfill_money_flow_history(symbols: list[str], *, days: int | None = None,
                                demo: bool = False, qps: float | None = None,
                                progress=None) -> dict:
    """按标的回填**逐日**主力资金流进 ``money_flow``。

    为什么需要它：每日横截面采集（``fetch_money_flow``）只能覆盖当天
    全市场，历史缺口补不回来；而资金面角度要看「最近 N 个交易日」。
    东财的 fflow/daykline 单票接口可回溯约 120 个交易日，
    所以任意标的的历史都能补齐 —— 这是唯一能事后补齐的看板表。

    :param symbols: 标的列表，裸码或带后缀都行，自动去重
    :param days: 每只只保留最近 N 个交易日；None = 接口给多少存多少
    :param qps: 请求速率；None 用 collector 的默认值（见 HIST_QPS 的说明）
    :param progress: 可选回调 ``(done, total, symbol)``，给 CLI 报进度
    :return: 回填报告（fetched/persisted/failed/覆盖前后对比）

    单票失败不中断整批：失败进 ``failed`` 明细，成功的照常落库。
    """
    from lquant.market.collectors.money_flow import (
        HIST_QPS,
        fetch_money_flow_history,
    )
    from lquant.market.scheduler import persist

    rate = HIST_QPS if qps is None else qps
    syms = _dedupe_symbols(list(symbols))
    before = _flow_have(syms)
    failed: dict[str, str] = {}
    fetched = persisted = 0
    batch: list[pl.DataFrame] = []

    def flush() -> int:
        nonlocal batch
        if not batch:
            return 0
        df = pl.concat(batch, how="vertical_relaxed") if len(batch) > 1 else batch[0]
        batch = []
        return int(persist({"money_flow": df}).get("money_flow", 0))

    for i, sym in enumerate(syms, 1):
        try:
            df = fetch_money_flow_history(sym, days=days, demo=demo, qps=rate)
            fetched += len(df)
            if len(df):
                batch.append(df)
        except Exception as e:  # noqa: BLE001  单票失败不放弃整批
            failed[sym] = f"{type(e).__name__}: {e}"
        if progress is not None:
            progress(i, len(syms), sym)
        if len(batch) >= _FLOW_BATCH:
            persisted += flush()
    persisted += flush()

    after = _flow_have(syms)
    return {
        "table": "money_flow",
        "symbols": len(syms),
        "days": days,
        "qps": rate,
        "fetched": fetched,
        "persisted": persisted,
        "failed": failed,
        "covered_before": sum(1 for s in syms if before.get(s)),
        "covered_after": sum(1 for s in syms if after.get(s)),
    }


def money_flow_symbols(*, limit: int | None = None) -> list[str]:
    """可回填的标的池：日线湖里的真实标的（按代码序）。

    用日线湖而不是 ``security`` 表：真实库里 ``security`` 可能只有几行
    （基础信息没同步过），日线湖才是「这台机器上确实有行情的标的」。
    路径走 ``lake_glob`` —— SQL/polars 里硬编码 'data/parquet/...' 是相对
    CWD 的，换目录就静默读空集。
    """
    from lquant.data.store.parquet import lake_glob

    try:
        df = pl.scan_parquet(lake_glob("daily"), missing_columns="insert",
                             extra_columns="ignore").select("symbol").unique().collect()
    except Exception:  # noqa: BLE001  湖结构异常/为空时退回空池，由调用方降级
        return []
    syms = sorted(str(s) for s in df["symbol"].to_list() if s)
    return syms[:limit] if limit else syms