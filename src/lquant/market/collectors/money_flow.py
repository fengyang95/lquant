"""资金流：个股主力净流入 + 北向资金。

主力资金流的正确用法是看**占比**而不是绝对值：
10 亿净流入对茅台是毛毛雨，对小盘股是巨量。
所以 main_net_ratio 才是跨股票可比的那一个。

北向资金自 2024-08 起不再实时披露，只在盘后公布总额 ——
所以 northbound_flow 表的采集时间只能在收盘后，盘中调用会拿到空值。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from lquant.core.types import now_cn, parse_symbol
from lquant.market.em_client import em_get

__all__ = ["fetch_money_flow", "fetch_northbound"]

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
        return datetime.now().date()
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


def fetch_northbound(trade_date=None, *, demo: bool = False) -> pl.DataFrame:
    """北向资金当日净流入（沪股通 + 深股通）。2024-08 后仅盘后可得。"""
    today = _as_date(trade_date)
    if demo:
        import random
        random.seed(today.toordinal())
        sh = random.uniform(-8e9, 8e9)
        sz = random.uniform(-6e9, 6e9)
        return pl.DataFrame([{
            "trade_date": today, "ts": now_cn(),
            "sh_net_inflow": sh, "sz_net_inflow": sz, "total_net_inflow": sh + sz,
            "collected_at": now_cn()}])

    resp = em_get("https://push2.eastmoney.com/api/qt/kamt/get?fields1=f1,f2,f3,f4"
                  "&fields2=f51,f52,f54,f56&ut=b2884a393a59ad64002292a3e90d46a5")
    data = resp.json().get("data") or {}
    sh = sz = 0.0
    for it in (data.get("hk2sh") or [], data.get("hk2sz") or []):
        pass
    # 该接口返回结构随版本变化，稳妥做法：取 kamt.rtmin 的最后一条
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/kamt.rtmin/get"
                   "?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56"
                   "&ut=b2884a393a59ad64002292a3e90d46a5")
        rows = (r.json().get("data") or {}).get("s2n") or []
        for line in rows:
            parts = str(line).split(",")
            if len(parts) >= 4:
                if "SH" in parts[1] or "沪" in parts[1]:
                    sh = _num(parts[-1])
                elif "SZ" in parts[1] or "深" in parts[1]:
                    sz = _num(parts[-1])
    except Exception:  # noqa: BLE001
        pass
    return pl.DataFrame([{
        "trade_date": today, "ts": now_cn(),
        "sh_net_inflow": sh, "sz_net_inflow": sz, "total_net_inflow": sh + sz,
        "collected_at": now_cn()}])


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
