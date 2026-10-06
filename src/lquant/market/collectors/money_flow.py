"""资金流：个股主力净流入 + 北向资金。

主力资金流的正确用法是看**占比**而不是绝对值：
10 亿净流入对茅台是毛毛雨，对小盘股是巨量。
所以 main_net_ratio 才是跨股票可比的那一个。

**两个接口解决两个不同的问题**（历史上只用了前者，且用法是错的 ——
`fid=f62&po=1&pz=N` 是「按主力净流入降序取前 N」，于是 money_flow 表里
只剩当天净流入最多的 100 只：任意一只普通股票永远查不到，而且留下来的
样本全是净流入，资金面角度天然偏多）：

1. ``clist/get`` 横截面（``fetch_money_flow``）—— 当日全市场快照。
   东财把 ``pz`` 静默截断在 100，所以要翻页：5562 只 ≈ 56 页。
2. ``fflow/daykline/get`` 单票历史（``fetch_money_flow_history``）——
   回溯约 120 个交易日，用来把任意标的的历史补齐。
   「随便输一个代码都有资金流可看」只能靠这个接口。

北向资金自 2024-08 起不再实时披露，只在盘后公布总额 ——
所以 northbound_flow 表的采集时间只能在收盘后，盘中调用会拿到空值。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.errors import DataUnavailable
from lquant.core.types import now_cn, now_cn_naive, parse_symbol, today_cn
from lquant.market.em_client import em_get
from lquant.market.schema import SOURCE_DEMO, SOURCE_HISTORY, SOURCE_REAL

__all__ = ["fetch_money_flow", "fetch_money_flow_history", "fetch_northbound",
           "PAGE_SIZE", "MAX_PAGES"]

_PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
_FFLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

# fs 参数覆盖：沪主板 + 科创板 + 深主板 + 创业板 + 北交所
_MARKET_FS = "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80,m:0+t:81+s:2048"
# f62 主力净流入 f184 主力净占比 f66 超大单 f72 大单 f78 中单 f84 小单
_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"

#: 东财 clist 单页上限。`pz` 给大于 100 的值不会报错，只会静默返回 100 条 ——
#: 这正是历史上「top=200 实际只拿到 100 只」的原因，所以分页是必须的。
PAGE_SIZE = 100
#: 翻页硬上限：5562 只 ≈ 56 页，留余量兜底，避免 total 异常时死循环。
MAX_PAGES = 80
#: 翻页请求速率。全市场一轮 56 次请求，比单次请求的采集器给源站的压力大一个
#: 量级，所以比 em_get 默认的 3 qps 更保守：push2 集群有 WAF，
#: 被封 IP 的代价是**资金流整体不可用**（实测：连续裸发几次 curl 之后
#: push2*/push2his* 全部返回 000，而 datacenter-web 仍正常），
#: 多花十几秒完全划算。
PAGE_QPS = 1.5
#: 单票历史请求速率。全市场回填是几千次连续请求（7000 只 ≈ 1.3 小时 @1.5qps），
#: 长时间连续访问比横截面更容易触发 WAF，所以默认比 em_get 的 3 qps 慢。
#: `lq data money-flow --qps` 可以按网络情况调整。
HIST_QPS = 1.5
#: demo 模式下生成的横截面行数（真实模式默认全市场）。
DEMO_ROWS = 200
#: demo 模式下生成的单票历史天数。
DEMO_HISTORY_DAYS = 60


def _norm(code: str) -> str:
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def _secid(symbol: str) -> str:
    """东财 secid：沪 1、深/北 0。"""
    try:
        sym = parse_symbol(symbol)
    except ValueError:
        return f"0.{symbol}"
    return f"{1 if sym.exchange == 'SH' else 0}.{sym.code}"


def _flow_schema() -> dict:
    """money_flow 的落库列 + source。

    显式给 schema 而不是靠推断：name 全为空时推断会得到 Null 列，
    upsert 时才炸。
    """
    return {
        "trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8, "close": pl.Float64,
        "change_pct": pl.Float64, "main_net_inflow": pl.Float64,
        "main_net_ratio": pl.Float64, "super_large_net": pl.Float64,
        "large_net": pl.Float64, "medium_net": pl.Float64, "small_net": pl.Float64,
        "source": pl.Utf8, "collected_at": pl.Datetime("us"),
    }


def _empty_flow() -> pl.DataFrame:
    return pl.DataFrame(schema=_flow_schema())


def _rows_to_frame(rows: list[dict], *, sort_desc: bool = False) -> pl.DataFrame:
    if not rows:
        return _empty_flow()
    df = pl.DataFrame(rows, schema=_flow_schema())
    return df.sort("main_net_inflow", descending=True) if sort_desc else df


# ------------------------------------------------------------ 横截面（当日全市场）


def _fetch_page(pn: int, pz: int, qps: float = PAGE_QPS) -> tuple[int | None, list[dict]]:
    """取第 ``pn`` 页，返回 (total, diff)。"""
    url = (f"{_PUSH2}?fid=f62&po=1&pz={pz}&pn={pn}&np=1&fltt=2&invt=2"
           f"&fs={_MARKET_FS}&fields={_FIELDS}")
    data = em_get(url, qps=qps).json().get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return data.get("total"), diff


def _snapshot_row(day: date, it: dict) -> dict:
    return {
        "trade_date": day,
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
        "source": SOURCE_REAL,
        "collected_at": now_cn_naive(),
    }


def fetch_money_flow(trade_date=None, top: int | None = None, *,
                     demo: bool = False, qps: float = PAGE_QPS) -> pl.DataFrame:
    """当日全市场主力资金流横截面（``top`` 给了就只取净流入前 N）。

    默认抓**全市场**（翻页），而不是「净流入前 200」——
    只留净流入前 N 会让资金面角度永远看不到净流出，
    且任意一只不在榜上的股票完全没有资金流数据。

    任何一页请求失败都直接抛出，不返回半份数据：
    半份数据只有净流入靠前的那些，静默偏多比没有数据更危险。
    """
    today = _as_date(trade_date)
    if demo:
        return _demo_flow(today, top or DEMO_ROWS)

    limit = int(top) if top else None
    rows: list[dict] = []
    total: int | None = None
    for pn in range(1, MAX_PAGES + 1):
        page_total, diff = _fetch_page(pn, PAGE_SIZE, qps)
        if total is None:
            total = page_total
        if not diff:
            break
        rows.extend(_snapshot_row(today, it) for it in diff)
        if len(diff) < PAGE_SIZE:
            break
        if total is not None and len(rows) >= total:
            break
        if limit is not None and len(rows) >= limit:
            break
    if limit is not None:
        rows = rows[:limit]
    return _rows_to_frame(rows, sort_desc=True)


# ------------------------------------------------------------ 单票历史（逐日）


_HIST_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"

# fields2=f51..f63 按位置返回，索引即语义。已用 600519 实测校验：
# 大单+超大单 == 主力净额、四类净额合计 ≈ 0、f62 收盘价/f63 涨跌幅与日线一致。
#   0 日期 1 主力净额 2 小单净额 3 中单净额 4 大单净额 5 超大单净额
#   6 主力净占比 7 小单占比 8 中单占比 9 大单占比 10 超大单占比
#   11 收盘价 12 涨跌幅 13/14 备用
_H_MAIN, _H_SMALL, _H_MEDIUM, _H_LARGE, _H_SUPER = 1, 2, 3, 4, 5
_H_RATIO, _H_CLOSE, _H_CHG = 6, 11, 12
_MIN_HIST_FIELDS = 13


def _parse_day(s: str) -> date | None:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _hist_rows(symbol: str, name, klines: list, *,
               days: int | None = None) -> list[dict]:
    ts = now_cn_naive()
    rows: list[dict] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < _MIN_HIST_FIELDS:
            continue
        d = _parse_day(parts[0])
        if d is None:
            continue
        rows.append({
            "trade_date": d,
            "symbol": symbol,
            "name": name,
            "close": _num(parts[_H_CLOSE]),
            "change_pct": _num(parts[_H_CHG]),
            "main_net_inflow": _num(parts[_H_MAIN]),
            "main_net_ratio": _num(parts[_H_RATIO]),
            "super_large_net": _num(parts[_H_SUPER]),
            "large_net": _num(parts[_H_LARGE]),
            "medium_net": _num(parts[_H_MEDIUM]),
            "small_net": _num(parts[_H_SMALL]),
            "source": SOURCE_HISTORY,
            "collected_at": ts,
        })
    rows.sort(key=lambda r: r["trade_date"])
    return rows[-days:] if days else rows


def fetch_money_flow_history(symbol: str, *, days: int | None = None,
                             demo: bool = False, qps: float = HIST_QPS) -> pl.DataFrame:
    """单只标的的逐日主力资金流（东财回溯约 120 个交易日）。

    ``days`` 只保留最近 N 个交易日。横截面接口拿不到历史 ——
    要补一只普通股票（不在净流入榜上）的资金流，只能走这个接口。
    """
    sym = _norm(symbol)
    if demo:
        return _demo_flow_history(sym, days or DEMO_HISTORY_DAYS)

    url = (f"{_FFLOW}?lmt=0&klt=101&secid={_secid(sym)}"
           f"&fields1=f1,f2,f3,f7&fields2={_HIST_FIELDS2}")
    data = em_get(url, qps=qps).json().get("data") or {}
    rows = _hist_rows(sym, data.get("name"), data.get("klines") or [], days=days)
    return _rows_to_frame(rows)


# ------------------------------------------------------------ 入参/数值工具


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


# ------------------------------------------------------------ 北向资金


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
    found = False
    # 主接口：hk2sh/hk2sz 每行 "date,净买额,..."，取最后一条为最新值
    for key, target in (("hk2sh", "sh"), ("hk2sz", "sz")):
        rows = data.get(key) or []
        if rows:
            parts = str(rows[-1]).split(",")
            if len(parts) >= 2:
                val = _num(parts[1])
                if target == "sh":
                    sh = val
                else:
                    sz = val
                found = True
    # 主接口缺失/结构变化 → rtmin 兜底（分钟线最后一条）
    if not found:
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
                        found = True
                    elif "SZ" in parts[1] or "深" in parts[1]:
                        sz = _num(parts[-1])
                        found = True
        except Exception:  # noqa: BLE001
            pass
    if not found:
        raise DataUnavailable(
            "eastmoney", f"北向资金无数据（{today}）：主接口与 rtmin 兜底均未取得有效行")
    return pl.DataFrame([{
        "trade_date": today, "ts": now_cn(),
        "sh_net_inflow": sh, "sz_net_inflow": sz, "total_net_inflow": sh + sz,
        "collected_at": now_cn()}])


# ------------------------------------------------------------ demo


def _demo_flow(d: date, n: int) -> pl.DataFrame:
    import random
    random.seed(d.toordinal())
    ts = now_cn_naive()
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
            "source": SOURCE_DEMO,
            "collected_at": ts,
        })
    return _rows_to_frame(rows, sort_desc=True)


def _demo_flow_history(symbol: str, n: int) -> pl.DataFrame:
    """单票 demo 历史：按代码定种子，保证同代码反复调用结果一致。"""
    import random
    random.seed(f"{symbol}:{n}")
    ts = now_cn_naive()
    today = today_cn()
    rows = []
    for i in range(n):
        d = today - timedelta(days=n - i)
        inflow = random.uniform(-3e8, 8e8)
        rows.append({
            "trade_date": d,
            "symbol": symbol,
            "name": f"样例-{symbol}",
            "close": round(random.uniform(5, 80), 2),
            "change_pct": round(random.uniform(-10, 10), 2),
            "main_net_inflow": inflow,
            "main_net_ratio": round(random.uniform(-15, 25), 2),
            "super_large_net": inflow * 0.6,
            "large_net": inflow * 0.3,
            "medium_net": -inflow * 0.4,
            "small_net": -inflow * 0.5,
            "source": SOURCE_DEMO,
            "collected_at": ts,
        })
    return _rows_to_frame(rows)
