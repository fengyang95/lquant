"""基准多因子策略真实数据 E2E(2021–2024)。

用法:
    PYTHONPATH=src python scripts/run_baseline_e2e.py \
        [--start 2021-01-01 --end 2024-12-31] [--out docs/BACKTEST_BASELINE_E2E.md]

流程:预检数据可用性 → 读日线湖 → JQRunner 沙箱实跑 → 打印 metrics 并把
运行记录写进 docs/BACKTEST_BASELINE_E2E.md(参数、时间戳、git commit、
metrics 表、nav 摘要、trades/rejected 统计、持仓数漂移量化)。

数据根目录按 lquant.core.config 解析(相对路径相对 CWD):在 worktree 里
`data` 是指向主仓 `data/` 的符号链接,无需复制数据。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import polars as pl

from lquant.backtest.jqapi import JQRunner
from lquant.data.store import catalog
from lquant.data.store import parquet as store
from lquant.research.strategies.baseline_multifactor import (
    FACTOR_FORMULAS,
    NET_PROFIT_YOY_ITEM,
    STRATEGY_CODE,
)

INITIAL_CASH = 10_000_000


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="基准多因子策略真实数据 E2E")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--out", default="docs/BACKTEST_BASELINE_E2E.md")
    ap.add_argument("--no-write", action="store_true", help="只打印,不写文档")
    return ap.parse_args()


def preflight(start: str, end: str) -> tuple[int, int, list[str], str, str]:
    """数据可用性预检。

    返回 (financial_pit 行数, 同比 items 数, items, 日线实际覆盖 min, max)。
    日线为空 → 退出;区间覆盖不足 → 打印警告(不退出,实跑按可得数据处理)。
    """
    head = store.read_daily(start=start, end=end).select("symbol").head(1).collect()
    if head.is_empty():
        raise SystemExit("日线湖无数据:先执行 lq data daily 回填")
    lf = store.read_daily(start=start, end=end)
    cov = (
        lf.select(pl.col("trade_date").min().alias("mn"), pl.col("trade_date").max().alias("mx"))
        .collect()
        .row(0)
    )
    if str(cov[0]) > str(start) or str(cov[1]) < str(end):
        print(
            f"[WARN] 日线湖覆盖 {cov[0]} ~ {cov[1]},不足请求区间 "
            f"{start} ~ {end}(缺 2021-2023 回填)",
            flush=True,
        )
    with catalog.reader() as con:
        n_fin = con.execute(
            "SELECT count(*) FROM financial_pit WHERE pub_date BETWEEN ? AND ?", [start, end]
        ).fetchone()[0]
        if n_fin == 0:
            raise SystemExit("financial_pit 无数据:先执行 lq data financial --all")
        items = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT item FROM financial_pit WHERE item LIKE '%yoy%' "
                "OR item LIKE '%YOY%' LIMIT 50"
            ).fetchall()
        ]
    return n_fin, len(items), items, str(cov[0]), str(cov[1])


def load_bars(start: str, end: str) -> pl.DataFrame:
    """读日线并剔除北交所。"""
    df = (
        store.read_daily(start=start, end=end)
        .filter(~pl.col("symbol").str.ends_with(".BJ"))
        .collect()
    )
    if df.is_empty():
        raise SystemExit("过滤北交所后日线为空")
    return df


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        return "unknown"


def fmt_metrics(metrics: dict) -> list[tuple[str, str]]:
    skip = {"benchmark"}
    rows = []
    for k, v in metrics.items():
        if k in skip:
            continue
        if isinstance(v, float):
            rows.append((k, f"{v:.6f}" if abs(v) < 1 else f"{v:,.2f}"))
        else:
            rows.append((k, str(v)))
    return rows


def nav_summary(nav: list[tuple], n: int = 5) -> list[tuple[str, float]]:
    if not nav:
        return []
    idx = sorted({0, len(nav) - 1, *{len(nav) * i // n for i in range(1, n)}})
    return [(str(nav[i][0]), nav[i][1]) for i in idx]


def positions_drift(records: dict) -> list[tuple[str, int, float]]:
    """各调仓日实际持仓数(record 观测)+ 持仓总市值。

    keep 带(top50)内但 top20 外的持仓不再平衡 → 权重漂移,这里量化。
    """
    n_pos = dict(records.get("n_positions", []))
    mv = dict(records.get("mv", []))
    return [(str(d), int(n_pos[d]), float(mv[d])) for d in sorted(n_pos)]


def render(
    start: str,
    end: str,
    elapsed: float,
    metrics: dict,
    nav: list[tuple],
    trades: list,
    rejected: list,
    drift: list[tuple[str, int, float]],
    yoy_items: list[str],
    n_bars: int,
    n_symbols: int,
    cov: tuple[str, str],
    out_path: str,
) -> str:
    mrows = "\n".join(f"| {k} | {v} |" for k, v in fmt_metrics(metrics))
    navrows = "\n".join(f"| {d} | {v:,.2f} |" for d, v in nav_summary(nav))
    first_nav = nav[0][1] if nav else float("nan")
    last_nav = nav[-1][1] if nav else float("nan")
    total_ret = (last_nav / first_nav - 1) if first_nav else float("nan")
    drows = "\n".join(f"| {d} | {n} | {mv:,.0f} |" for d, n, mv in drift)
    rej_summary = "无"
    if rejected:
        reasons: dict[str, int] = {}
        for _d, _s, r in rejected:
            reasons[r] = reasons.get(r, 0) + 1
        rej_summary = "; ".join(
            f"`{r}` x{n}" for r, n in sorted(reasons.items(), key=lambda x: -x[1])
        )
    drift_desc = "n/a"
    if drift:
        counts = [n for _, n, _ in drift]
        drift_desc = (
            f"{min(counts)}–{max(counts)}(均值 "
            f"{sum(counts) / len(counts):.1f},首次 {counts[0]},"
            f"末期 {counts[-1]})"
        )
    return f"""# 基准多因子策略 E2E 运行记录

由 `scripts/run_baseline_e2e.py` 生成,人工补注见文末。

## 运行参数

| 项 | 值 |
|---|---|
| 区间 | {start} ~ {end} |
| 初始资金 | {INITIAL_CASH:,} |
| 调仓 | 月度第 1 交易日开盘,top 20 等权,跌出 top 50 卖出 |
| 因子公式 | {", ".join(FACTOR_FORMULAS)} |
| 净利润同比 item | `{NET_PROFIT_YOY_ITEM}` |
| 运行时间戳 | {datetime.now().isoformat(timespec="seconds")} |
| git commit | `{git_commit()}` |
| 耗时 | {elapsed:.1f} s |
| 日线实际覆盖 | {cov[0]} ~ {cov[1]} |
| 日线条数 | {n_bars:,} |
| 标的数(剔除 .BJ) | {n_symbols:,} |

## Metrics

| 指标 | 值 |
|---|---|
{mrows}

## NAV 摘要

| 日期 | NAV |
|---|---|
{navrows}

区间收益 {total_ret:.2%}({first_nav:,.0f} → {last_nav:,.0f})。

> 注:请求区间 {start} ~ {end},但日线湖实际覆盖 {cov[0]} ~ {cov[1]}
> (2021–2023 未回填),本记录为覆盖区间内的实跑结果。

## Trades / Rejected

- 成交 {len(trades):,} 笔;拒单 {len(rejected):,} 笔({rej_summary})。

## 各调仓日实际持仓数(keep 带漂移量化)

| 日期 | 持仓数 | 持仓市值 |
|---|---|---|
{drows}

keep 带(top 50)内但 top 20 外的持仓不会被再平衡 → 持仓数/权重随时间漂移:
区间内持仓数 {drift_desc}。

## growth item 确认

financial_pit 中与"同比/yoy"相关的 DISTINCT item:{yoy_items or "(无)"}。
策略采用 `{NET_PROFIT_YOY_ITEM}`(tushare fina_indicator,主源)。
"""


MANUAL_NOTES_HEADING = "## 人工补注"


def extract_manual_notes(doc: str) -> str:
    """提取文档中的"人工补注"段(到文末);没有则返回空串。"""
    idx = doc.find(MANUAL_NOTES_HEADING)
    if idx < 0:
        return ""
    return doc[idx:].rstrip() + "\n"


def merge_manual_notes(new_doc: str, old_doc: str) -> str:
    """重跑时保留旧文档的人工补注段,避免脚本整体覆盖抹掉人工内容。"""
    notes = extract_manual_notes(old_doc)
    if not notes:
        return new_doc
    # 旧补注已含同名段时不再重复追加(幂等)。
    if MANUAL_NOTES_HEADING in new_doc:
        return new_doc
    return new_doc.rstrip() + "\n\n" + notes


def main() -> None:
    args = _parse_args()
    t0 = time.time()
    _n_fin, _n_item, yoy_items, cov_mn, cov_mx = preflight(args.start, args.end)
    df = load_bars(args.start, args.end)
    n_bars, n_symbols = df.height, df["symbol"].n_unique()
    print(f"[preflight] bars={n_bars:,} symbols={n_symbols:,} yoy_items={yoy_items}", flush=True)

    res = JQRunner(STRATEGY_CODE, initial_cash=INITIAL_CASH, factor_formulas=FACTOR_FORMULAS).run(
        df
    )
    if res.error:
        print(f"[E2E FAILED] {res.error}", file=sys.stderr)
        raise SystemExit(1)

    elapsed = time.time() - t0
    print("[metrics]", flush=True)
    for k, v in fmt_metrics(res.metrics):
        print(f"  {k}: {v}")
    drift = positions_drift(res.records)
    print(f"[drift] {drift}")
    print(f"[trades] {len(res.trades):,}  [rejected] {len(res.rejected):,}")

    if not args.no_write:
        doc = render(
            args.start,
            args.end,
            elapsed,
            res.metrics,
            res.nav,
            res.trades,
            res.rejected,
            drift,
            yoy_items,
            n_bars,
            n_symbols,
            (cov_mn, cov_mx),
            args.out,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            doc = merge_manual_notes(doc, out.read_text(encoding="utf-8"))
        out.write_text(doc, encoding="utf-8")
        print(f"[written] {out}")


if __name__ == "__main__":
    main()
