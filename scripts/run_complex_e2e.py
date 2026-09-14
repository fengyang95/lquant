"""复杂策略真实数据 E2E(2025-01 ~ 2026-09)。

用法:
    PYTHONPATH=src python scripts/run_complex_e2e.py

用更宽的聚宽 API 面检验自研回测引擎在完备真实数据上的正确性:
- 多表 get_fundamentals(valuation+indicator+growth)月度选股,order_target_value 调仓
- get_current_data 剔 ST/停牌;attribute_history 121 日动量过滤
- 止损止盈走 run_daily('every_bar') + order_target(0)

运行后四类语义审计:
1. 停牌日无成交;2. 涨停不买/跌停不卖;3. T+1 无当日双向成交;4. 现金>=0。
"""

from __future__ import annotations

import subprocess
import sys
import time

import polars as pl

from lquant.backtest.jqapi import JQRunner
from lquant.data.store import parquet as store

INITIAL_CASH = 10_000_000
START = "2025-01-01"
END = "2026-09-11"

STRATEGY_CODE = '''
def initialize(context):
    set_benchmark('000300.SH')
    set_order_cost(type="stock", open_tax=0, close_tax=0.001,
                   open_commission=0.0003, close_commission=0.0003, min_commission=5)
    set_slippage(PriceRelatedSlippage(0.0024))
    run_monthly(rebalance, monthday=1, time='open')
    run_daily(risk_check, time='every_bar')
    g.top_n = 15
    g.stop_line = -0.10
    g.take_line = 0.25

def risk_check(context):
    for sym in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sym]
        if pos.total_amount <= 0:
            continue
        ret = pos.price / pos.avg_cost - 1 if pos.avg_cost > 0 else 0
        if ret >= g.take_line or ret <= g.stop_line:
            order_target(sym, 0)
            record(stop_or_take=1)

def rebalance(context):
    # 财务+估值多表查询放调仓日(月度),全市场查询单次 ~170s(G13 性能缺口)
    q = query(
        valuation.pe_ratio, valuation.market_cap,
        indicator.roe, growth.inc_revenue_year_on_year,
    )
    fd = get_fundamentals(q)
    if fd is None or len(fd) == 0:
        return
    df = fd[(fd['pe_ratio'] > 0) & (fd['pe_ratio'] < 60)]
    df = df[df['market_cap'] > 30e4]
    df = df[df['roe'] > 5]
    df = df[df['inc_revenue_year_on_year'] > 0].dropna()
    df = df.sort_values('roe', ascending=False)
    if len(df) == 0:
        return
    cur = get_current_data()
    pool = []
    for sym in df['code']:
        cd = cur[sym]
        if cd.paused or cd.is_st:
            continue
        n = attribute_history(sym, 121, '1d', ['close'], skip_paused=True)
        if len(n) < 121:
            continue
        mom = n['close'][-1] / n['close'][0] - 1
        if mom > 0:
            pool.append(sym)
    g.pool = pool[: g.top_n]
    for sym in list(context.portfolio.positions.keys()):
        if sym not in g.pool:
            order_target(sym, 0)
    tv = context.portfolio.total_value
    for sym in g.pool:
        order_target_value(sym, tv / g.top_n)
'''


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except Exception:
        return "unknown"


def load_bars(start: str, end: str) -> pl.DataFrame:
    df = (
        store.read_daily(start=start, end=end)
        .filter(~pl.col("symbol").str.ends_with(".BJ"))
        .collect()
    )
    if df.is_empty():
        raise SystemExit("日线湖无数据")
    # 股票域预筛:取期初流动性前 600 的标的,控制每日全市场
    # get_fundamentals 对 financial_pit(72M 行)的查询成本(G13 性能缺口,
    # 见 docs/BACKTEST_BASELINE_E2E.md)。预筛后财务查询走 symbol scope。
    anchor = df.filter(pl.col("trade_date") == df["trade_date"].min())
    top = (
        anchor.filter(pl.col("float_mv").is_not_null())
        .sort("float_mv", descending=True)
        .head(600)["symbol"]
        .to_list()
    )
    return df.filter(pl.col("symbol").is_in(top))


def audit(res, bars: pl.DataFrame) -> list[str]:
    """语义审计,返回违例描述(空列表 = 全过)。"""
    violations: list[str] = []
    tr = res.trades_frame()
    if tr.is_empty():
        return ["无任何成交——策略没有产生交易,审计无意义"]
    j = tr.join(
        bars.select("trade_date", "symbol", "is_suspended", "pre_close"),
        on=["trade_date", "symbol"],
        how="left",
    )
    # 1) 停牌日无成交
    bad = j.filter(pl.col("is_suspended") == True)  # noqa: E712
    if bad.height:
        violations.append(
            f"停牌日成交 {bad.height} 笔: {bad.select('trade_date', 'symbol').head(5).rows()}"
        )
    # 2) T+1:同日同标的既有买入又有卖出
    two = (
        j.group_by("trade_date", "symbol")
        .agg(pl.col("side").n_unique().alias("ns"))
        .filter(pl.col("ns") > 1)
    )
    if two.height:
        violations.append(f"T+1 违例 {two.height} 组: {two.head(5).rows()}")
    # 3) 涨停买/跌停卖(主板 10% 粗查)
    j2 = j.filter(pl.col("pre_close").is_not_null() & (pl.col("pre_close") > 0))
    buy_up = j2.filter((pl.col("side") == "buy") & (pl.col("price") >= pl.col("pre_close") * 1.1 - 1e-6))
    sell_dn = j2.filter((pl.col("side") == "sell") & (pl.col("price") <= pl.col("pre_close") * 0.9 + 1e-6))
    if buy_up.height:
        violations.append(f"涨停买入 {buy_up.height} 笔: {buy_up.select('trade_date', 'symbol').head(5).rows()}")
    if sell_dn.height:
        violations.append(f"跌停卖出 {sell_dn.height} 笔: {sell_dn.select('trade_date', 'symbol').head(5).rows()}")
    return violations


def main() -> None:
    t0 = time.time()
    bars = load_bars(START, END)
    n_bars, n_sym = bars.height, bars["symbol"].n_unique()
    print(f"[preflight] bars={n_bars:,} symbols={n_sym:,}", flush=True)

    res = JQRunner(STRATEGY_CODE, initial_cash=INITIAL_CASH).run(bars)
    if res.error:
        print(f"[E2E FAILED] {res.error}", file=sys.stderr)
        raise SystemExit(1)

    elapsed = time.time() - t0
    print(f"[elapsed] {elapsed:.1f}s  commit={git_commit()}")
    print("[metrics]")
    for k, v in res.metrics.items():
        print(f"  {k}: {v:.6f}" if isinstance(v, float) and abs(v) < 1 else f"  {k}: {v}")
    print(f"[trades] {len(res.trades):,}  [rejected] {len(res.rejected):,}")
    if res.rejected:
        reasons: dict[str, int] = {}
        for _d, _s, r in res.rejected:
            reasons[r] = reasons.get(r, 0) + 1
        print("[rejected-reasons]", sorted(reasons.items(), key=lambda x: -x[1])[:10])
    nav = res.nav
    if nav:
        print(f"[nav] first={nav[0][1]:,.0f} last={nav[-1][1]:,.0f} "
              f"ret={nav[-1][1] / nav[0][1] - 1:+.2%}")
    violations = audit(res, bars)
    print("[audit]", "PASS" if not violations else "FAIL")
    for v in violations:
        print("  !!", v)
    if violations:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
