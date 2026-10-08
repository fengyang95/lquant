"""收盘对账：用官方日线重算当日 NAV，覆盖盘中近似值。

两段式净值的第二段（第一段是 service.tick 的盘中盯市）：
- 盘中：最新价近似盯市，快照标记 source=intraday
- 盘后：官方日线 close 落库后重算 NAV，标记 source=official，
  并与盘中值对账 —— 偏差大说明行情源/涨跌停/停牌处理有 bug

绩效展示与回测对拍一律用 official 口径（与回测同源，严格可比）；
intraday 保留作审计。停牌/缺收盘价的持仓沿用盯市价并标记 stale。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.paper import store

# 与 alert.compare_nav 的日频容忍度对齐：1% 内 ok，3% 内 warning，超之 critical
_TOL_OK = 0.01
_TOL_WARN = 0.03


def reconcile(name: str, d: date | str) -> dict:
    """对账单个交易日。要求当日已写过 intraday 快照；official 覆盖重算。"""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    broker = store.load_broker(name)

    held = [p.symbol for p in broker.positions.values() if p.qty > 0]
    closes: dict[str, float] = {}
    if held:
        daily = _official_closes(held, d)
        closes = dict(zip(daily["symbol"].to_list(),
                          daily["close"].to_list(), strict=True)) if len(daily) else {}

    stale: list[str] = []
    n_covered = 0
    official_nav = broker.cash
    for p in broker.positions.values():
        if p.qty <= 0:
            continue
        px = closes.get(p.symbol)
        if px is None or px <= 0:
            stale.append(p.symbol)          # 停牌/未覆盖：沿用盯市价
            px = p.last_price or p.avg_cost
        else:
            n_covered += 1
        official_nav += p.qty * float(px)

    intraday = _intraday_nav(name, d)
    report = {
        "account": name,
        "trade_date": d.isoformat(),
        "nav_official": round(official_nav, 2),
        "nav_intraday": round(intraday, 2) if intraday is not None else None,
        "rel_dev": None,
        "stale_symbols": stale,
        "n_held": len(held),
        # n_uncovered 用「拿到正收盘价的持仓数」反推：close=0 也会被
        # {symbol: close} 装进来，直接 len(held)-len(closes) 会低估未覆盖数
        "n_uncovered": len(held) - n_covered,
        "verdict": "ok",
        "detail": "",
    }
    if intraday is not None and official_nav:
        dev = abs(official_nav - intraday) / abs(official_nav)
        report["rel_dev"] = round(dev, 6)
        if dev <= _TOL_OK:
            report["verdict"], report["detail"] = "ok", f"偏差 {dev:.3%} 在容忍度内"
        elif dev <= _TOL_WARN:
            report["verdict"] = "warning"
            report["detail"] = f"偏差 {dev:.3%} 偏高 —— 检查行情源延迟/滑点假设"
        else:
            report["verdict"] = "critical"
            report["detail"] = f"偏差 {dev:.3%} 严重超阈 —— 模拟盘盯市可能有 bug"

    store.record_nav(name, d, official_nav, broker.cash,
                     sum(1 for p in broker.positions.values() if p.qty > 0),
                     "official")
    return report


def _official_closes(symbols: list[str], d: date) -> pl.DataFrame:
    """官方日线收盘价。函数级导入便于测试 monkeypatch。"""
    from lquant.data.store.parquet import read_daily
    df = read_daily(symbols=symbols, start=d, end=d).collect()
    if not len(df):
        return df
    return df.select("symbol", "close")


def _intraday_nav(name: str, d: date) -> float | None:
    f = store.nav_frame(name)
    if not len(f):
        return None
    row = f.filter((pl.col("trade_date") == d.isoformat())
                   & (pl.col("source") == "intraday"))
    return float(row["nav"][0]) if len(row) else None
