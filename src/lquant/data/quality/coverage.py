"""日线湖覆盖度对账：扫描「日历有交易日、湖里没数据」的缺口。

两类缺口：
- 整日缺失（missing_dates）：该交易日湖里 0 行 —— 通常是同步任务失败或
  没跑，直接 error 落库并（可选）建 daily_update 修复任务；
- 标的级稀疏（sparse_symbols）：湖里已有该标的、但窗口内缺部分交易日 ——
  通常是单标的拉取失败。只对湖里已有行的标的报，避免新上市股票
  （本来就不该有全窗口数据）误报。

daily_basic 湖整体为空是常态（可能从未回填）：整日缺失降为 info 且不
触发 repair；但 sparse 仍然按 error 报（有行就说明该标的回填过）。

issue 复用内容指纹幂等：同一缺口重复扫描覆盖原行，不膨胀。
"""
from __future__ import annotations

from datetime import date, timedelta

from lquant.core.db import reader
from lquant.core.types import today_cn
from lquant.data.ingest.tasks import TaskConflictError, create_task
from lquant.data.quality.issues import Issue, save_issues
from lquant.data.store.parquet import read_daily, read_daily_basic

__all__ = ["scan_coverage"]

# issue 的 extra.dates 超过这个数就截断，避免 detail JSON 无限膨胀
_MAX_DATES_IN_EXTRA = 30
_TABLES = ("daily", "daily_basic")


def _trade_days(start: date, end: date) -> list[date]:
    """窗口内交易日（日历表里 is_open=1 的日子）。"""
    from lquant.data.store.catalog import TradeCalendarRepo

    return TradeCalendarRepo().range(start, end)


def _expected_symbols(end: date, start: date) -> list[str]:
    """窗口内应有标的：上市早于 end、（未退市 或 退市晚于 start）。

    daily 湖不收指数，sec_type 限定 stock/etf/lof。直接 SQL 查 security
    表 —— active_symbols 不含退市窗口逻辑。
    """
    with reader() as con:
        rows = con.execute(
            "SELECT symbol FROM security "
            "WHERE list_date <= ? "
            "AND (delist_date IS NULL OR delist_date > ?) "
            "AND sec_type IN ('stock', 'etf', 'lof') ORDER BY symbol",
            [end, start],
        ).fetchall()
    return [r[0] for r in rows]


def _lake_pairs(table: str) -> set[tuple[str, date]]:
    """湖内 (symbol, trade_date) 全集（只取两列，省内存）。

    read_daily 返回 LazyFrame；read_daily_basic 返回 DataFrame（空湖时
    是带 schema 的 LazyFrame）—— .lazy() 统一后 select + collect。
    """
    if table == "daily":
        df = read_daily().select("symbol", "trade_date").collect()
    else:
        # read_daily_basic 返回 DataFrame（空湖才是 LazyFrame）→ 统一 lazy
        df = read_daily_basic().lazy().select("symbol", "trade_date").collect()
    return set(zip(df["symbol"].to_list(), df["trade_date"].to_list(),
                   strict=True))


def _gap_report(table: str, days: list[date], expected: list[str],
                pairs: set[tuple[str, date]],
                day_severity: str) -> tuple[dict, list[Issue]]:
    """单张表的对账：整日缺失 + 标的级稀疏，返回 (report, issues)。"""
    present_dates = {d for _, d in pairs}
    missing_dates = sorted(set(days) - present_dates)
    by_symbol: dict[str, set[date]] = {}
    for sym, d in pairs:
        by_symbol.setdefault(sym, set()).add(d)

    n_expected = len(expected)
    issues: list[Issue] = []
    for d in missing_dates:
        issues.append(Issue(
            rule="COVERAGE_GAP", severity=day_severity, dataset=table,
            trade_date=d, count=n_expected,
            detail=f"湖内 {n_expected} 只标的中 0 只在该日有行",
        ))

    sparse: dict[str, list[date]] = {}
    for sym in sorted(by_symbol):
        have = by_symbol[sym] & set(days)
        gaps = sorted(set(days) - have)
        if gaps:
            sparse[sym] = gaps
            truncated = [x.isoformat() for x in gaps[:_MAX_DATES_IN_EXTRA]]
            issues.append(Issue(
                rule="COVERAGE_GAP", severity="error", dataset=table,
                symbol=sym, count=len(gaps),
                detail=f"窗口内缺失 {len(gaps)} 个交易日（共 {len(days)} 天）",
                extra={"dates": truncated},
            ))
    report = {
        "missing_dates": missing_dates,
        "sparse_symbols": {s: g for s, g in sorted(sparse.items())},
    }
    return report, issues


def _maybe_repair(missing_dates: list[date], end: date, repair: bool) -> dict:
    """daily 有整日缺口时建 daily_update 修复任务；冲突/无缺口不抛。"""
    if not repair:
        return {"created": False, "task_id": None, "reason": None}
    if not missing_dates:
        return {"created": False, "task_id": None, "reason": "no_gap"}
    earliest = min(missing_dates)
    try:
        task = create_task("daily_update", {
            "start": earliest.isoformat(), "end": end.isoformat(),
            "note": "coverage_gap_repair", "auto_crosscheck": True,
        })
    except TaskConflictError:
        return {"created": False, "task_id": None, "reason": "active_task_exists"}
    return {"created": True, "task_id": task["task_id"], "reason": None}


def scan_coverage(days: int = 30, *, repair: bool = False) -> dict:
    """对账窗口 [today-cn - days, today-cn] 内 daily / daily_basic 的覆盖度。

    返回 report dict（missing_dates / sparse_symbols / issues_recorded /
    repair），缺口以 COVERAGE_GAP issue 落库；repair=True 且 daily 有整日
    缺口时建 daily_update 任务（basic 缺口不触发 repair）。
    """
    end = today_cn()
    start = end - timedelta(days=days)
    trade_days = _trade_days(start, end)
    expected = _expected_symbols(end, start)

    tables: dict[str, dict] = {}
    issues: list[Issue] = []
    for table in _TABLES:
        pairs = _lake_pairs(table)
        severity = "info" if table == "daily_basic" else "error"
        report, table_issues = _gap_report(
            table, trade_days, expected, pairs, severity)
        tables[table] = report
        issues.extend(table_issues)

    issues_recorded = save_issues(issues)
    daily_missing = tables["daily"]["missing_dates"]
    return {
        "window": {"start": start, "end": end},
        "tables": tables,
        "issues_recorded": issues_recorded,
        "repair": _maybe_repair(daily_missing, end, repair),
    }
