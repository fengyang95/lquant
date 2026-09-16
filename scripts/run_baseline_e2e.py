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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from lquant.backtest.jqapi import JQRunner
from lquant.backtest.metrics import perf_from_returns, turnover_from_trades
from lquant.data.store import catalog
from lquant.data.store import parquet as store
from lquant.research.strategies.baseline_multifactor import (
    FACTOR_FORMULAS,
    NET_PROFIT_YOY_ITEM,
    STRATEGY_CODE,
)

INITIAL_CASH = 10_000_000
DEFAULT_WARMUP_DAYS = 120


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="基准多因子策略真实数据 E2E")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument(
        "--warmup-days",
        type=int,
        default=DEFAULT_WARMUP_DAYS,
        help="预热自然日数:实际喂引擎的 start 提前这么多天(默认 120,0 关闭)",
    )
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


def resolve_warmup_start(start: str, warmup_days: int) -> str:
    """计算实际喂引擎的 start。

    请求 start 提前 warmup_days 个自然日;湖在该预热窗内(严格早于请求
    start)无任何数据时打印 WARN 并跳过预热(引擎 start = 请求 start),
    不得报错退出。返回最终喂给引擎的 start。
    """
    if warmup_days <= 0:
        return start
    s = date.fromisoformat(start)
    warm_start = (s - timedelta(days=warmup_days)).isoformat()
    mn = (
        store.read_daily(start=warm_start, end=start)
        .filter(pl.col("trade_date") < s)
        .select(pl.col("trade_date").min())
        .collect()
        .item()
    )
    if mn is None:
        print(
            f"[WARN] 湖在预热窗 {warm_start} ~ {start} 内无数据,跳过预热"
            "(前几个月可能仍是 0 持仓伪影)",
            flush=True,
        )
        return start
    return warm_start


@dataclass
class ClippedResult:
    """剔除预热期后的回测结果(metrics 用请求 start 之后的序列重算)。"""

    nav: list[tuple[date, float]]
    trades: list
    rejected: list
    records: dict
    metrics: dict


def clip_warmup(res, start: str) -> ClippedResult:
    """过滤早于请求 start 的日期,并基于剩余序列重算 metrics。

    预热期只用于让因子/持仓"热身",其调仓与净值不得进入结论。
    """
    s = date.fromisoformat(start)
    nav = [(d, v) for d, v in res.nav if date.fromisoformat(str(d)) >= s]
    trades = [t for t in res.trades if date.fromisoformat(str(t.trade_date)) >= s]
    rejected = [
        r
        for r in res.rejected
        if date.fromisoformat(str(r[0] if isinstance(r, tuple) else r.trade_date)) >= s
    ]
    records = {
        k: [(d, x) for d, x in series if date.fromisoformat(str(d)) >= s]
        for k, series in res.records.items()
    }
    rets = [nav[i][1] / nav[i - 1][1] - 1 for i in range(1, len(nav)) if nav[i - 1][1] > 0]
    dates = [d for d, _ in nav][1 : 1 + len(rets)]
    perf = perf_from_returns(rets, dates=[str(d) for d in dates])
    perf.pop("nav", None)
    metrics = {
        **perf,
        "initial_cash": res.metrics.get("initial_cash", INITIAL_CASH),
        "final_nav": nav[-1][1] if nav else INITIAL_CASH,
        "n_trades": len(trades),
        "n_rejected": len(rejected),
        "total_fee": sum(f.fee for f in trades),
        "turnover": turnover_from_trades(
            [(t.trade_date, t.qty * t.price) for t in trades]
        ),
        "benchmark": res.metrics.get("benchmark"),
    }
    return ClippedResult(nav=nav, trades=trades, rejected=rejected,
                         records=records, metrics=metrics)


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
    warm_start: str,
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
| 预热 | 引擎实际从 {warm_start} 起跑,请求 start 之前的预热期调仓/净值不入结论 |
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
# 重跑时保留到文末的段(取最早出现的段起,一并提取):人工补注 + 缺口清单。
PRESERVED_HEADINGS = ("## 缺口清单", MANUAL_NOTES_HEADING)


def extract_manual_notes(doc: str) -> str:
    """提取文档中需重跑保留的段(从最早出现保留段起到文末);没有则返回空串。"""
    idxs = [doc.find(h) for h in PRESERVED_HEADINGS if doc.find(h) >= 0]
    if not idxs:
        return ""
    idx = min(idxs)
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
    warm_start = resolve_warmup_start(args.start, args.warmup_days)
    _n_fin, _n_item, yoy_items, cov_mn, cov_mx = preflight(args.start, args.end)
    df = load_bars(warm_start, args.end)
    n_bars, n_symbols = df.height, df["symbol"].n_unique()
    print(f"[preflight] bars={n_bars:,} symbols={n_symbols:,} yoy_items={yoy_items}", flush=True)
    if warm_start < args.start:
        print(f"[warmup] engine start={warm_start}, 结论区间 {args.start} ~ {args.end}", flush=True)

    res = JQRunner(STRATEGY_CODE, initial_cash=INITIAL_CASH, factor_formulas=FACTOR_FORMULAS).run(
        df
    )
    if res.error:
        print(f"[E2E FAILED] {res.error}", file=sys.stderr)
        raise SystemExit(1)
    res = clip_warmup(res, args.start)

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
            warm_start,
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
