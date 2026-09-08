"""龙虎榜（ evening 时点）。

交易所 T+1 盘后公布，采的是「昨日上榜」—— 当天采集当天榜会拿到空，
所以调度时间设在 18:00 之后。数据可回溯（东财数据中心保留历史），
失败不算 critical。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.types import now_cn, parse_symbol
from lquant.market.em_client import em_get

__all__ = ["fetch_dragon_tiger"]

_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _norm(code: str) -> str:
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def _num(v) -> float:
    if v in (None, "", "-"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_dragon_tiger(trade_date: date | str | None = None, *,
                       demo: bool = False) -> pl.DataFrame:
    """龙虎榜明细。trade_date 缺省 = 上一交易日（榜是 T+1 公布的）。"""
    if demo:
        return _demo(_as_date(trade_date))

    d = _as_date(trade_date)
    url = (
        f"{_API}?reportName=RPT_DAILYBILLBOARD_DETAILSNEW"
        f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,"
        f"BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,EXPLANATION"
        f"&filter=(TRADE_DATE='{d.isoformat()}')"
        f"&pageSize=200&pageNumber=1&sortColumns=BILLBOARD_NET_AMT"
        f"&sortTypes=-1&source=WEB&client=WEB"
    )
    resp = em_get(url)
    data = (resp.json() or {}).get("result") or {}
    items = data.get("data") or []
    rows = []
    seen: set[str] = set()
    for it in items:
        sym = _norm(it.get("SECURITY_CODE"))
        if sym in seen:                       # 同票多上榜原因合并取第一条
            continue
        seen.add(sym)
        rows.append({
            "trade_date": d,
            "symbol": sym,
            "name": it.get("SECURITY_NAME_ABBR"),
            "close": _num(it.get("CLOSE_PRICE")),
            "change_pct": _num(it.get("CHANGE_RATE")),
            "net_buy": _num(it.get("BILLBOARD_NET_AMT")),
            "buy_amount": _num(it.get("BILLBOARD_BUY_AMT")),
            "sell_amount": _num(it.get("BILLBOARD_SELL_AMT")),
            "reason": it.get("EXPLANATION"),
            "collected_at": now_cn(),
        })
    return pl.DataFrame(rows, schema=_SCHEMA) if rows else _empty()


_SCHEMA = {
    "trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
    "close": pl.Float64, "change_pct": pl.Float64, "net_buy": pl.Float64,
    "buy_amount": pl.Float64, "sell_amount": pl.Float64,
    "reason": pl.Utf8, "collected_at": pl.Datetime("us"),
}


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=_SCHEMA)


def _as_date(v) -> date:
    """缺省取昨日（榜 T+1 公布）；周末自动回退到周五。"""
    if v is None:
        d = datetime.now().date() - timedelta(days=1)
        if d.weekday() >= 5:
            d -= timedelta(days=d.weekday() - 4)
        return d
    if isinstance(v, str):
        return datetime.strptime(v.replace("-", ""), "%Y%m%d").date()
    return v


def _demo(d: date) -> pl.DataFrame:
    import random

    random.seed(d.toordinal() * 7 + 3)
    rows = []
    for i in range(12):
        buy = random.uniform(2e7, 8e8)
        rows.append({
            "trade_date": d,
            "symbol": f"{600000 + i * 37:06d}.SH" if i % 2 else f"{1 + i * 53:06d}.SZ",
            "name": f"样例榜{i:02d}",
            "close": round(random.uniform(6, 90), 2),
            "change_pct": round(random.uniform(-9, 10), 2),
            "net_buy": buy - buy * random.uniform(0.3, 0.9),
            "buy_amount": buy,
            "sell_amount": buy * random.uniform(0.3, 0.9),
            "reason": ["日涨幅偏离值达7%的证券", "连续三个交易日内收盘价格涨跌幅偏离值累计20%",
                       "有价格涨跌幅限制的日换手率达到20%"][i % 3],
            "collected_at": now_cn(),
        })
    return pl.DataFrame(rows, schema=_SCHEMA).sort("net_buy", descending=True)
