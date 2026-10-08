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

from dataclasses import dataclass
from datetime import date, timedelta

from lquant.core.db import reader
from lquant.core.types import now_cn, today_cn
from lquant.data.ingest.tasks import TaskConflictError, create_task
from lquant.data.quality.issues import Issue, save_issues
from lquant.data.store.parquet import read_daily, read_daily_basic

__all__ = ["DEFAULT_DAY_MIN_RATIO", "DayGap", "day_completeness",
           "day_gap_issues", "scan_coverage"]

# issue 的 extra.dates 超过这个数就截断，避免 detail JSON 无限膨胀
_MAX_DATES_IN_EXTRA = 30
_TABLES = ("daily", "daily_basic")

# 15:00 收盘 + 数据源（tushare/东财日线）落地缓冲：此前的当日数据不可信。
# 对账窗口必须把「还没收盘的今天」排除，否则盘中/午后跑对账必报
# COVERAGE_GAP error，还可能触发一轮注定空手而归的 daily_update 修复
_MARKET_SETTLED_HOUR = 16


def _trade_days(start: date, end: date) -> list[date]:
    """窗口内交易日（日历表里 is_open=1 的日子）。"""
    from lquant.data.store.catalog import TradeCalendarRepo

    return TradeCalendarRepo().range(start, end)


#: 日级完整性阈值：某交易日实际写入标的数 / 本次应写标的数 低于它即判「不完整」。
#: 0.7 是经验值（正常批次总有个别标的停牌/源站缺失）；可被调用方覆盖。
DEFAULT_DAY_MIN_RATIO = 0.7


@dataclass(frozen=True)
class DayGap:
    """某交易日的完整性缺口。kind: ``day`` = 窗口内的整日/大面积缺口；
    ``tip`` = **最新若干日**的缺口（最典型的形态：`end=today` 只写完一部分
    标的，水位却推到最大分区 —— 分区看着新鲜，覆盖率断崖）。"""

    trade_date: date
    symbols: int
    expected: int
    kind: str = "day"

    @property
    def ratio(self) -> float:
        return self.symbols / self.expected if self.expected else 0.0

    def detail(self) -> str:
        return (f"{self.trade_date} 只有 {self.symbols}/{self.expected} 只"
                f"（{self.ratio:.0%} < {DEFAULT_DAY_MIN_RATIO:.0%}）"
                + ("，且是最新交易日（疑似只写了一半）" if self.kind == "tip" else ""))


def day_completeness(per_day_counts: dict[date, int], expected: int, *,
                     min_ratio: float = DEFAULT_DAY_MIN_RATIO,
                     tip_days: int = 1) -> list[DayGap]:
    """按「本次应写标的数」评估每个交易日的覆盖率，返回不完整的那些日。

    为什么用**本次应写数**而不是 security 表里的全市场数：security 表本身可能
    是空的/不全（真实库实测只有 3 行），拿它当分母会把「期望」算错。本次任务
    尝试了多少只，是同一批次内自洽的分母，足以回答「这个交易日到底写全了没」。

    最新的 ``tip_days`` 天额外标 ``kind="tip"``：水位前移门禁主要防的就是它 ——
    尾部那天只写了一半时，不能当作「这一天已经齐了」。
    """
    if expected <= 0:
        return []
    days = sorted(per_day_counts)
    tail = set(days[-tip_days:]) if tip_days > 0 else set()
    gaps: list[DayGap] = []
    for d in days:
        n = int(per_day_counts[d])
        if n / expected >= min_ratio:
            continue
        gaps.append(DayGap(trade_date=d, symbols=n, expected=expected,
                           kind="tip" if d in tail else "day"))
    return gaps


def day_gap_issues(gaps: list[DayGap], *, dataset: str = "daily_bar",
                   severity: str = "error") -> list[Issue]:
    """缺口 → data_quality_issue（与既有 issue 表同构，按内容指纹幂等）。"""
    return [Issue(rule="DAY_INCOMPLETE", severity=severity, dataset=dataset,
                  trade_date=g.trade_date, count=max(g.expected - g.symbols, 0),
                  detail=g.detail(),
                  extra={"kind": g.kind, "symbols": g.symbols,
                         "expected": g.expected, "ratio": round(g.ratio, 4)})
            for g in gaps]


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

    read_daily 返回 LazyFrame，read_daily_basic 返回 DataFrame ——
    统一 ``.lazy()`` 后 select + collect，两条路径写法一致。
    """
    if table == "daily":
        df = read_daily().select("symbol", "trade_date").collect()
    else:
        # read_daily_basic 恒返回 DataFrame（空湖也是）→ 统一 lazy 后取数
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

    收盘前（北京时间 < 16:00）今日不可能有完整日线，窗口 end 自动退到
    昨日 —— 否则任何盘中触发的对账都会把「今天还没同步」误报成缺口。
    """
    end = today_cn()
    now = now_cn()
    if (now.hour, now.minute) < (_MARKET_SETTLED_HOUR, 0):
        end -= timedelta(days=1)
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
