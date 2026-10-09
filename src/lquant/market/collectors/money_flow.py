"""个股主力资金流。

主力资金流的正确用法是看**占比**而不是绝对值：
10 亿净流入对茅台是毛毛雨，对小盘股是巨量。
所以 main_net_ratio 才是跨股票可比的那一个。

北向资金已迁到 `market/collectors/northbound.py`（2024-08 起净买额停发，
口径不同，不宜与主力资金混在一个模块里）。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from lquant.core.types import now_cn, parse_symbol, today_cn
from lquant.market.em_client import em_get

__all__ = ["fetch_money_flow"]

_PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"

# fs 参数覆盖：沪主板 + 深主板 + 创业板 + 科创板
_MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
# f62 主力净流入 f184 主力净占比 f66 超大单 f72 大单 f78 中单 f84 小单
_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"


def _norm(code: str) -> str:
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def fetch_money_flow(trade_date=None, top: int = 200, *, demo: bool = False) -> pl.DataFrame:
    """按主力净流入排序取前 top 只。

    返回的 trade_date 用当天日期；盘后调用才有意义。
    """
    today = _as_date(trade_date)
    if demo:
        return _demo_flow(today, top)

    url = (f"{_PUSH2}?fid=f62&po=1&pz={top}&pn=1&np=1&fltt=2&invt=2"
           f"&fs={_MARKET_FS}&fields={_FIELDS}")
    resp = em_get(url)
    data = resp.json().get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())

    rows = []
    for it in diff:
        rows.append({
            "trade_date": today,
            "symbol": _norm(it.get("f12")),
            "name": it.get("f14"),
            "close": _num(it.get("f2")),
            "change_pct": _num(it.get("f3")),
            "main_net_inflow": _num(it.get("f62")),
            "main_net_ratio": _num(it.get("f184")),
            "super_large_net": _num(it.get("f66")),
            "large_net": _num(it.get("f72")),
            "medium_net": _num(it.get("f78")),
            "small_net": _num(it.get("f84")),
            "collected_at": now_cn(),
        })
    return pl.DataFrame(rows) if rows else _empty_flow()


def _as_date(v) -> date:
    """采集器统一入参：None 用今天，字符串转 date。"""
    if v is None:
        return today_cn()
    if isinstance(v, str):
        return datetime.strptime(v.replace("-", ""), "%Y%m%d").date()
    return v


def _num(v) -> float:
    if v in (None, "", "-"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _empty_flow() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8, "close": pl.Float64,
        "change_pct": pl.Float64, "main_net_inflow": pl.Float64, "main_net_ratio": pl.Float64,
        "super_large_net": pl.Float64, "large_net": pl.Float64, "medium_net": pl.Float64,
        "small_net": pl.Float64, "collected_at": pl.Datetime("us")})


def _demo_flow(d: date, n: int) -> pl.DataFrame:
    import random
    random.seed(d.toordinal())
    rows = []
    for i in range(n):
        inflow = random.uniform(-3e8, 8e8)
        rows.append({
            "trade_date": d,
            "symbol": f"{600000 + i * 13:06d}.SH" if i % 2 else f"{300000 + i * 17:06d}.SZ",
            "name": f"样例{i:03d}",
            "close": round(random.uniform(5, 80), 2),
            "change_pct": round(random.uniform(-10, 10), 2),
            "main_net_inflow": inflow,
            "main_net_ratio": round(random.uniform(-15, 25), 2),
            "super_large_net": inflow * 0.6,
            "large_net": inflow * 0.3,
            "medium_net": -inflow * 0.4,
            "small_net": -inflow * 0.5,
            "collected_at": now_cn(),
        })
    return pl.DataFrame(rows).sort("main_net_inflow", descending=True)
