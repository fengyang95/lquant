"""北向资金（沪深股通）数据面。

**口径事实（写进代码，不靠口头约定）**

2024-08-19 起交易所不再公布北向「净买入」，只公布：

1. 成交总额 / 成交笔数（日频，`RPT_MUTUAL_DEAL_HISTORY`）；
2. 前十大成交活跃证券（日频，`RPT_MUTUAL_TOP10DEAL`）；
3. 单只持股（季度）。

因此**任何仍然展示「北向实时净流入」的模块都是失效口径**。2026-09 之前
lquant 的旧采集器还在解析东财 `kamt/get` 的「净买额」，源站把该字段置零后
它把 0.0 当成真值写进了库（库里 2026-09-08 之后的 16 行全是 0），其后源站改成
对象结构，采集器直接 KeyError —— 本次重建把它换成本模块。

**为什么净买额用 NULL 而不是 0**：0 会被下游当成「净买额恰好为零」这个观测值，
而真实语义是「该字段已停止披露」。所以 `<= NORTHBOUND_NET_LAST_DATE` 的历史日期
照常落库，之后的日期落 NULL，并用 `net_published` 布尔列显式标注口径。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import polars as pl

from lquant.core.errors import DataUnavailable, SourceSchemaChanged
from lquant.core.types import now_cn, today_cn
from lquant.market.em_client import em_get

__all__ = [
    "NORTHBOUND_NET_LAST_DATE",
    "fetch_northbound",
    "fetch_northbound_top10",
]

# 交易所最后一天公布北向「净买入」：2024-08-16。次一交易日 2024-08-19 起停发。
# 该日期由 RPT_MUTUAL_DEAL_HISTORY 的 NET_DEAL_AMT 逐日实测得到
# （2024-08-16 有值，2024-08-19 起恒为 null），不是凭报告抄的。
NORTHBOUND_NET_LAST_DATE = date(2024, 8, 16)

_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DEAL_REPORT = "RPT_MUTUAL_DEAL_HISTORY"
_TOP10_REPORT = "RPT_MUTUAL_TOP10DEAL"

# 报表金额单位：百万元。反推校验：同一报表族 RPT_MUTUAL_QUOTA.TRADE_QUOTA=52000
# 即沪股通每日额度 520 亿元 ⇒ 52000 × 1e6。搬数前先量单位，别信字段名。
_AMT_UNIT = 1e6

# MUTUAL_TYPE：001 沪股通 / 003 深股通 / 005 北向合计（深沪合计）。
# 002/004/006 是南向，本模块不取 —— 南向净买额仍在披露，但那是另一条资金线。
_DEAL_TYPES = {"001": "sh", "003": "sz", "005": "total"}
_TOP10_TYPES = {"001": "沪股通", "003": "深股通"}

_DEAL_COLUMNS = ("TRADE_DATE,MUTUAL_TYPE,DEAL_AMT,DEAL_NUM,BUY_AMT,SELL_AMT,"
                 "NET_DEAL_AMT")
_TOP10_COLUMNS = ("TRADE_DATE,MUTUAL_TYPE,SECURITY_CODE,DERIVE_SECURITY_CODE,"
                  "SECURITY_NAME,RANK,CLOSE_PRICE,CHANGE_RATE,DEAL_AMT,MUTUAL_RATIO")

_FLOW_SCHEMA = {
    "trade_date": pl.Date, "ts": pl.Datetime("us"),
    "sh_deal_amt": pl.Float64, "sz_deal_amt": pl.Float64, "total_deal_amt": pl.Float64,
    "deal_num": pl.Int64,
    "sh_net_inflow": pl.Float64, "sz_net_inflow": pl.Float64,
    "total_net_inflow": pl.Float64, "net_published": pl.Boolean,
    "collected_at": pl.Datetime("us"),
}

_TOP10_SCHEMA = {
    "trade_date": pl.Date, "board": pl.Utf8, "symbol": pl.Utf8, "name": pl.Utf8,
    "rank_no": pl.Int64, "close": pl.Float64, "change_pct": pl.Float64,
    "deal_amt": pl.Float64, "mutual_ratio": pl.Float64,
    "collected_at": pl.Datetime("us"),
}


def _as_date(v) -> date:
    """采集器统一入参：None 用今天，字符串转 date。"""
    if v is None:
        return today_cn()
    if isinstance(v, str):
        return datetime.strptime(v.replace("-", ""), "%Y%m%d").date()
    return v


def _num(v):
    """转 float；空值返回 None（**不是 0**，空值与 0 在资金面上语义相反）。"""
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _report(report: str, *, columns: str, filter_: str, sort: str = "TRADE_DATE",
            sort_type: str = "-1", page_size: int = 50) -> list[dict]:
    """调东财数据中心报表。

    列名/报表名漂移是永久错误（重试无意义）→ SourceSchemaChanged；
    日期无数据是正常空态 → 由调用方决定抛 DataUnavailable 还是返回空表。
    """
    params = {"reportName": report, "columns": columns, "pageSize": str(page_size),
              "pageNumber": "1", "sortColumns": sort, "sortTypes": sort_type,
              "source": "WEB", "client": "WEB", "filter": filter_}
    try:
        payload = em_get(f"{_DATACENTER}?{urlencode(params)}").json()
    except SourceSchemaChanged:
        raise
    except Exception as e:  # noqa: BLE001  网络/JSON 解析失败都算源不可用
        raise DataUnavailable("eastmoney", f"{report} 请求失败：{e}") from e
    if payload.get("success") is False:
        msg = str(payload.get("message") or "")
        # 「报表配置不存在」「列不存在」= 源站改版，重试无用
        if "不存在" in msg or "列" in msg:
            raise SourceSchemaChanged("eastmoney", f"{report}: {msg}")
        raise DataUnavailable("eastmoney", f"{report} 不可用：{msg}")
    return ((payload.get("result") or {}).get("data")) or []


def _deal_rows(start: date, end: date) -> dict[date, dict[str, dict]]:
    """按交易日归集 001/003/005 三行。"""
    types = ",".join(f'"{t}"' for t in _DEAL_TYPES)
    rows = _report(
        _DEAL_REPORT, columns=_DEAL_COLUMNS,
        filter_=(f"(MUTUAL_TYPE in ({types}))"
                 f"(TRADE_DATE>='{start.isoformat()}')(TRADE_DATE<='{end.isoformat()}')"),
        page_size=max(50, (end - start).days * 4 + 8))
    out: dict[date, dict[str, dict]] = {}
    for r in rows:
        t = str(r.get("MUTUAL_TYPE") or "")
        if t not in _DEAL_TYPES:
            continue
        raw = str(r.get("TRADE_DATE") or "")[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        out.setdefault(d, {})[t] = r
    return out


def _flow_row(d: date, by_type: dict[str, dict]) -> dict:
    """把一天的 001/003/005 三行折成一行看板记录（缺行/串列即抛）。"""
    missing = sorted(set(_DEAL_TYPES) - set(by_type))
    if missing:
        raise DataUnavailable(
            "eastmoney", f"北向成交额缺 MUTUAL_TYPE {missing}（{d}）：报表未返回该行")
    deal: dict[str, float] = {}
    for t, key in _DEAL_TYPES.items():
        amt = _num(by_type[t].get("DEAL_AMT"))
        if amt is None or amt <= 0:
            raise DataUnavailable(
                "eastmoney",
                f"北向成交额非正（{d} MUTUAL_TYPE={t}）：{by_type[t].get('DEAL_AMT')!r}")
        deal[key] = amt * _AMT_UNIT
    # 自洽校验：合计必须等于沪 + 深。源站换列/串列会在这里炸，而不是静默入库。
    if abs(deal["total"] - deal["sh"] - deal["sz"]) > max(1.0, deal["total"] * 1e-6):
        raise DataUnavailable(
            "eastmoney",
            f"北向成交额不自洽（{d}）：合计 {deal['total']:.0f} != "
            f"沪 {deal['sh']:.0f} + 深 {deal['sz']:.0f}")

    # 笔数取不到就给 NULL —— 它是次要字段，不该为了它把整天的成交额一起丢掉；
    # 但绝不能写成 0，那会被下游当成「当日只有 0 笔」。
    num = _num(by_type["005"].get("DEAL_NUM"))

    published = d <= NORTHBOUND_NET_LAST_DATE
    net: dict[str, float | None] = {"sh": None, "sz": None, "total": None}
    if published:
        for t, key in _DEAL_TYPES.items():
            v = _num(by_type[t].get("NET_DEAL_AMT"))
            if v is None:
                # 口径已声明该日应披露净买额 → 拿不到就是源站问题，fail-loud
                raise DataUnavailable(
                    "eastmoney",
                    f"北向净买额应已披露（{d} <= {NORTHBOUND_NET_LAST_DATE}）"
                    f"但 MUTUAL_TYPE={t} 返回空")
            net[key] = v * _AMT_UNIT
    return {
        "trade_date": d, "ts": now_cn(),
        "sh_deal_amt": deal["sh"], "sz_deal_amt": deal["sz"],
        "total_deal_amt": deal["total"],
        "deal_num": int(num) if num is not None else None,
        "sh_net_inflow": net["sh"], "sz_net_inflow": net["sz"],
        "total_net_inflow": net["total"], "net_published": published,
        "collected_at": now_cn(),
    }


def fetch_northbound(trade_date=None, *, days: int = 1, demo: bool = False) -> pl.DataFrame:
    """北向成交额（沪股通 + 深股通 + 合计）与成交笔数。

    days : 回看多少个自然日（含 trade_date）。看板「近 N 日」用得上；
           正常日频采集用 1。**非交易日不会产生行**（源站无该日数据）；
           窗口内源站**没返回**的交易日也不会补行 —— 缺失要靠调用方对日期，
           不在这里编造。

    净买额只在 `<= NORTHBOUND_NET_LAST_DATE` 的日期上落库，其余为 NULL。
    """
    today = _as_date(trade_date)
    if demo:
        return _demo_northbound(today, days)
    start = today - timedelta(days=max(0, days - 1))
    grouped = _deal_rows(start, today)
    if not grouped:
        raise DataUnavailable(
            "eastmoney",
            f"北向成交额无数据（{start}~{today}）：非交易日或报表未更新")
    rows = [_flow_row(d, grouped[d]) for d in sorted(grouped, reverse=True)]
    return pl.DataFrame(rows, schema=_FLOW_SCHEMA)


def fetch_northbound_top10(trade_date=None, *, demo: bool = False) -> pl.DataFrame:
    """北向前十大成交活跃证券（日频，按成交额）。

    注意：**北向没有 per-stock 净买额**（已停发），本表只有成交额与
    「占该股成交额比例」。南向（002/004）仍有净买额，但那是另一条线。
    """
    today = _as_date(trade_date)
    if demo:
        return _demo_top10(today)
    types = ",".join(f'"{t}"' for t in _TOP10_TYPES)
    rows = _report(
        _TOP10_REPORT, columns=_TOP10_COLUMNS,
        filter_=f'(MUTUAL_TYPE in ({types}))(TRADE_DATE=\'{today.isoformat()}\')',
        sort="RANK", sort_type="1", page_size=40)
    out = []
    for r in rows:
        code = str(r.get("DERIVE_SECURITY_CODE") or r.get("SECURITY_CODE") or "").strip()
        raw = str(r.get("TRADE_DATE") or "")[:10]
        if not code or not raw:
            continue
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            continue
        out.append({
            "trade_date": day,
            "board": _TOP10_TYPES[str(r.get("MUTUAL_TYPE") or "")],
            "symbol": code,
            "name": str(r.get("SECURITY_NAME") or ""),
            "rank_no": int(_num(r.get("RANK")) or 0),
            "close": _num(r.get("CLOSE_PRICE")),
            "change_pct": _num(r.get("CHANGE_RATE")),
            "deal_amt": (_num(r.get("DEAL_AMT")) or 0.0),
            "mutual_ratio": _num(r.get("MUTUAL_RATIO")),
            "collected_at": now_cn(),
        })
    if not out:
        raise DataUnavailable("eastmoney", f"北向前十大活跃无数据（{today}）：非交易日或源站未更新")
    return pl.DataFrame(out, schema=_TOP10_SCHEMA).sort(["board", "rank_no"])


# ---- demo（离线确定性样例，绝不进真实湖：source 列在调度层标注）----

def _demo_northbound(d: date, days: int = 1) -> pl.DataFrame:
    import random

    rows = []
    for i in range(max(1, days)):
        day = d - timedelta(days=i)
        random.seed(day.toordinal())
        sh = random.uniform(3e10, 1.2e11)
        sz = random.uniform(3e10, 1.2e11)
        published = day <= NORTHBOUND_NET_LAST_DATE
        net_sh = random.uniform(-8e9, 8e9) if published else None
        net_sz = random.uniform(-6e9, 6e9) if published else None
        rows.append({
            "trade_date": day, "ts": now_cn(),
            "sh_deal_amt": sh, "sz_deal_amt": sz, "total_deal_amt": sh + sz,
            "deal_num": int(sh / 2e4),
            "sh_net_inflow": net_sh, "sz_net_inflow": net_sz,
            "total_net_inflow": (net_sh + net_sz) if published else None,
            "net_published": published, "collected_at": now_cn(),
        })
    return pl.DataFrame(rows, schema=_FLOW_SCHEMA)


def _demo_top10(d: date) -> pl.DataFrame:
    import random

    random.seed(d.toordinal())
    rows = []
    for board, base in (("沪股通", 600000), ("深股通", 300000)):
        for i in range(10):
            rows.append({
                "trade_date": d, "board": board,
                "symbol": f"{base + i * 13:06d}.{'SH' if board == '沪股通' else 'SZ'}",
                "name": f"样例{board[:2]}{i:02d}", "rank_no": i + 1,
                "close": round(random.uniform(8, 120), 2),
                "change_pct": round(random.uniform(-10, 10), 2),
                "deal_amt": random.uniform(1e9, 8e9),
                "mutual_ratio": round(random.uniform(10, 60), 2),
                "collected_at": now_cn(),
            })
    return pl.DataFrame(rows, schema=_TOP10_SCHEMA)
