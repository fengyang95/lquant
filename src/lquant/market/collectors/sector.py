"""板块与行业涨跌。

东财把板块分成三层，fs 参数不同：
- m:90+t:2   行业板块（申万一级，31 个）
- m:90+t:3   概念板块（几百个，含大量噪音）
- m:90+t:1   地域板块

行业轮动策略只用 t:2。概念板块数量多且重叠严重，
直接拿来做因子会引入大量共线性，必须先正交化或去重。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from lquant.core.types import now_cn, parse_symbol, today_cn
from lquant.market.em_client import em_get

__all__ = ["fetch_sectors", "fetch_concepts", "fetch_areas", "SECTOR_FS"]

_PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
SECTOR_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3", "area": "m:90+t:1"}

# f12 代码 f14 名称 f3 涨跌幅 f6 成交额（元）f8 换手率 f62 主力净流入
# f128 领涨股名称 f136 领涨股涨跌幅 f207 领涨股代码 f104/f105 上涨/下跌家数
_FIELDS = "f12,f14,f3,f6,f8,f62,f128,f136,f207,f104,f105"


def _norm(code: str) -> str:
    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def _as_date(v) -> date:
    if v is None:
        return today_cn()
    if isinstance(v, str):
        return datetime.strptime(v.replace("-", ""), "%Y%m%d").date()
    return v


def _num(v, default: float = 0.0) -> float:
    if v in (None, "", "-"):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_sectors(trade_date=None, kind: str = "industry", *, top: int = 100,
                  demo: bool = False) -> pl.DataFrame:
    """板块行情。kind: industry | concept | area"""
    today = _as_date(trade_date)
    if demo:
        return _demo_sectors(today, kind)

    fs = SECTOR_FS.get(kind, SECTOR_FS["industry"])
    url = (f"{_PUSH2}?pn=1&pz={top}&po=1&np=1&fltt=2&invt=2"
           f"&fid=f3&fs={fs}&fields={_FIELDS}")
    resp = em_get(url)
    data = resp.json().get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())

    rows = []
    for it in diff:
        rows.append({
            "trade_date": today,
            "sector_code": str(it.get("f12")),
            "sector_name": it.get("f14"),
            "kind": kind,
            "change_pct": _num(it.get("f3")),
            "turnover_rate": _num(it.get("f8")),
            "amount": _num(it.get("f6")),   # f6 = 板块成交额（元），此前硬编码 0.0
            "main_net_inflow": _num(it.get("f62")),
            "leader_symbol": _norm(it.get("f207")) if it.get("f207") else None,
            "leader_name": it.get("f128"),
            "leader_change": _num(it.get("f136")),
            "up_count": int(_num(it.get("f104"))),
            "down_count": int(_num(it.get("f105"))),
            "collected_at": now_cn(),
        })
    if not rows:
        return _empty()
    return pl.DataFrame(rows).sort("change_pct", descending=True)


def fetch_concepts(trade_date=None, *, top: int = 200, demo: bool = False) -> pl.DataFrame:
    return fetch_sectors(trade_date, "concept", top=top, demo=demo)


def fetch_areas(trade_date=None, *, top: int = 60, demo: bool = False) -> pl.DataFrame:
    return fetch_sectors(trade_date, "area", top=top, demo=demo)


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "trade_date": pl.Date, "sector_code": pl.Utf8, "sector_name": pl.Utf8,
        "kind": pl.Utf8,
        "change_pct": pl.Float64, "turnover_rate": pl.Float64, "amount": pl.Float64,
        "main_net_inflow": pl.Float64, "leader_symbol": pl.Utf8, "leader_name": pl.Utf8,
        "leader_change": pl.Float64, "up_count": pl.Int64, "down_count": pl.Int64,
        "collected_at": pl.Datetime("us")})


def _demo_sectors(d, kind: str) -> pl.DataFrame:
    import random
    random.seed(hash((d.toordinal(), kind)) % 10000)
    if kind == "industry":
        names = ["半导体", "电力设备", "医药生物", "证券", "食品饮料", "电子", "计算机",
                 "通信", "汽车", "机械设备", "化工", "有色金属", "银行", "房地产",
                 "建筑装饰", "公用事业", "交通运输", "传媒", "纺织服饰", "家用电器"]
    elif kind == "concept":
        names = ["人工智能", "储能", "光伏", "机器人", "芯片", "算力", "国企改革",
                 "锂电池", "鸿蒙", "创新药", "华为汽车", "数据中心", "虚拟现实"]
    else:
        names = ["北京", "上海", "深圳", "杭州", "广州", "江苏", "浙江",
                 "山东", "四川", "福建", "湖南", "湖北", "陕西"]
    # 各 kind 独立代码段：sector_daily 主键 (trade_date, sector_code)，
    # 同天三种 kind 一起写入时不能撞主键互相覆盖
    code_base = {"industry": 1000, "concept": 2000, "area": 3000}[kind]
    rows = []
    for i, n in enumerate(names):
        chg = round(random.uniform(-5, 8), 2)
        rows.append({
            "trade_date": d, "sector_code": f"BK{code_base + i:04d}", "sector_name": n,
            "kind": kind,
            "change_pct": chg, "turnover_rate": round(random.uniform(0.3, 5), 2),
            "amount": round(random.uniform(1e9, 8e10), 2),
            "main_net_inflow": round(random.uniform(-2e9, 3e9), 2),
            "leader_symbol": f"{600000 + i * 23:06d}.SH", "leader_name": f"龙头{i}",
            "leader_change": round(chg + random.uniform(0, 9), 2),
            "up_count": random.randint(5, 90), "down_count": random.randint(3, 80),
            "collected_at": now_cn(),
        })
    return pl.DataFrame(rows).sort("change_pct", descending=True)
