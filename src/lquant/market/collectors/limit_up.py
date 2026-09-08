"""涨停池 / 炸板池 / 跌停池。

东财的涨停池接口**只提供当日数据**，没有历史回溯。
当天不采，这笔数据就永远消失了 —— 所以它是整个看板里唯一
"失败必须告警"的采集任务，且要在收盘后尽快跑（15:05 左右）。

字段口径（东财 getTopicZTPool）：
- p     价格，放大 1000 倍
- zdp   涨跌幅，已是百分数
- lbc   连板数
- zbc   炸板次数
- fbt   首次封板时间戳（秒）
- hs    换手率，放大 100 倍
"""
from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl

from lquant.core.errors import DataUnavailable
from lquant.core.types import now_cn
from lquant.market.em_client import em_get

__all__ = ["fetch_limit_up_pool", "fetch_limit_down_pool", "fetch_broken_pool"]

_UT = "7eea3edcaed734bea9cbfc24409ed989"
_BASE = "https://push2ex.eastmoney.com"


def _ymd(d: date | str | None) -> str:
    if d is None:
        return date.today().strftime("%Y%m%d")
    if isinstance(d, str):
        return d.replace("-", "")
    return d.strftime("%Y%m%d")


def _parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _fetch_pool(kind: str, trade_date: date | str | None = None,
                pagesize: int = 300) -> list[dict]:
    """kind: ZT(涨停) / ZB(炸板) / DT(跌停)"""
    url = (f"{_BASE}/getTopic{kind}Pool?ut={_UT}&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize={pagesize}&sort=fbt%3Aasc&date={_ymd(trade_date)}")
    resp = em_get(url)
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable(f"涨停池响应非 JSON: {e}") from e
    if not payload or not payload.get("data"):
        return []
    return payload["data"].get("pool", []) or []


def _ts_to_hhmmss(v) -> str:
    if not v:
        return ""
    try:
        return datetime.fromtimestamp(int(v)).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(v)


def _norm_symbol(code: str) -> str:
    from lquant.core.types import parse_symbol
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def fetch_limit_up_pool(trade_date: date | str | None = None, *,
                        demo: bool = False) -> pl.DataFrame:
    """涨停池。demo=True 时生成合成数据用于离线链路验证。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "up")

    rows = []
    for it in _fetch_pool("ZT", trade_date):
        rows.append({
            "trade_date": d,
            "symbol": _norm_symbol(it.get("c")),
            "name": it.get("n"),
            "close": float(it.get("p", 0)) / 1000.0,
            "change_pct": float(it.get("zdp", 0)),
            "amount": float(it.get("fund", 0) or 0),
            "turnover_rate": float(it.get("hs", 0) or 0) / 100.0,
            "first_limit_time": _ts_to_hhmmss(it.get("fbt")),
            "last_limit_time": _ts_to_hhmmss(it.get("lbt")),
            "open_count": int(it.get("zbc", 0) or 0),
            "limit_up_type": _limit_type(it),
            "industry": it.get("hybk"),
            "collected_at": now_cn(),
        })
    cols = ["trade_date", "symbol", "name", "close", "change_pct", "amount",
            "turnover_rate", "first_limit_time", "last_limit_time", "open_count",
            "limit_up_type", "industry", "collected_at"]
    return pl.DataFrame(rows, schema={c: None for c in cols}, orient="row") if not rows \
        else pl.DataFrame(rows)


def _limit_type(it: dict) -> str:
    """一字板 / T 字板 / 换手板 —— 三者含义完全不同。"""
    zbc = int(it.get("zbc", 0) or 0)
    lbc = int(it.get("lbc", 0) or 0)
    hi = float(it.get("h", 0) or 0) / 1000.0
    lo = float(it.get("l", 0) or 0) / 1000.0
    if hi > 0 and abs(hi - lo) < 1e-9:
        return "一字板"
    if zbc > 0:
        return "T字板" if lbc > 1 else "换手板"
    return "换手板"


def fetch_broken_pool(trade_date: date | str | None = None, *,
                      demo: bool = False) -> pl.DataFrame:
    """炸板池：曾涨停但收盘未封住。炸板率是情绪最重要的反向指标。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "broken")
    rows = [{"trade_date": d, "symbol": _norm_symbol(it.get("c")), "name": it.get("n"),
             "close": float(it.get("p", 0)) / 1000.0,
             "change_pct": float(it.get("zdp", 0)),
             "amount": float(it.get("fund", 0) or 0),
             "first_limit_time": _ts_to_hhmmss(it.get("fbt")),
             "open_count": int(it.get("zbc", 0) or 0),
             "industry": it.get("hybk"),
             "collected_at": now_cn()}
            for it in _fetch_pool("ZB", trade_date)]
    return pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
                "close": pl.Float64, "change_pct": pl.Float64, "amount": pl.Float64,
                "first_limit_time": pl.Utf8, "open_count": pl.Int64,
                "industry": pl.Utf8, "collected_at": pl.Datetime("Asia/Shanghai")})


def fetch_limit_down_pool(trade_date: date | str | None = None, *,
                          demo: bool = False) -> pl.DataFrame:
    """跌停池。与涨停池的数量比，是判断市场极值情绪的快捷指标。"""
    d = _parse_ymd(_ymd(trade_date))
    if demo:
        return _demo_pool(d, "down")
    rows = [{"trade_date": d, "symbol": _norm_symbol(it.get("c")), "name": it.get("n"),
             "close": float(it.get("p", 0)) / 1000.0,
             "change_pct": float(it.get("zdp", 0)),
             "amount": float(it.get("fund", 0) or 0),
             "industry": it.get("hybk"), "collected_at": now_cn()}
            for it in _fetch_pool("DT", trade_date)]
    return pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
                "close": pl.Float64, "change_pct": pl.Float64, "amount": pl.Float64,
                "industry": pl.Utf8, "collected_at": pl.Datetime("Asia/Shanghai")})


def _demo_pool(d: date, kind: str) -> pl.DataFrame:
    """合成数据：格式与真实采集一致，用于离线验证看板链路。"""
    import random

    random.seed(hash((d, kind)) % 10000)
    n = {"up": 42, "broken": 15, "down": 6}[kind]
    rows = []
    for i in range(n):
        rows.append({
            "trade_date": d,
            "symbol": f"{600000 + i * 7:06d}.SH" if i % 2 else f"{300000 + i * 11:06d}.SZ",
            "name": f"样例{i:03d}",
            "close": round(random.uniform(5, 60), 2),
            "change_pct": 10.0 if kind == "up" else (-10.0 if kind == "down"
                                                     else round(random.uniform(2, 9), 2)),
            "amount": round(random.uniform(1e7, 3e9), 2),
            "turnover_rate": round(random.uniform(0.5, 25), 2),
            "first_limit_time": f"{random.randint(9, 14):02d}:{random.randint(0, 59):02d}:00",
            "last_limit_time": "15:00:00",
            "open_count": random.choice([0, 0, 0, 1, 2, 3]),
            "limit_up_type": random.choice(["一字板", "换手板", "T字板"]),
            "industry": random.choice(["半导体", "电力设备", "医药生物", "券商", "食品饮料"]),
            "collected_at": now_cn(),
        })
    df = pl.DataFrame(rows)
    if kind == "down":
        return df.drop(["turnover_rate", "limit_up_type"])
    if kind == "broken":
        return df
    return df
