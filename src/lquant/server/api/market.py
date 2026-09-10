"""看板：情绪 / 板块 / 资金流 / 涨停池 / 采集调度。

读 DuckDB 看板表（market_* 由采集器写入）。
数据不存在时返回空结构而不是 500 —— 看板「没数据」是常态
（盘前/节假日/源站故障），前端按空态渲染。
"""
from __future__ import annotations

from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from lquant.core.db import reader
from lquant.market.collectors import list_collectors
from lquant.server.deps import resolve_symbol

router = APIRouter(prefix="/market", tags=["market"])


def _read(table: str, limit: int = 200) -> pl.DataFrame:
    try:
        with reader() as con:
            return con.execute(
                f'SELECT * FROM {table} ORDER BY trade_date DESC LIMIT {limit}').pl()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


@router.get("/overview")
def overview() -> dict:
    """大盘总览：情绪分 + 涨跌停 + 北向，一个接口给看板首屏。"""
    senti = _read("sentiment_daily", 30)
    nb = _read("northbound_flow", 30)
    latest = senti.row(0, named=True) if len(senti) else {}
    return {
        "sentiment": {
            "score": latest.get("sentiment_score"),
            "limit_up": latest.get("limit_up_count"),
            "limit_down": latest.get("limit_down_count"),
            "broken_rate": latest.get("broken_rate"),
            "max_consecutive": latest.get("max_consecutive"),
            "trade_date": str(latest.get("trade_date")) if latest else None,
        },
        "sentiment_history": senti.select(
            [c for c in ["trade_date", "sentiment_score", "limit_up_count",
                         "broken_rate"] if c in senti.columns]).to_dicts() if len(senti) else [],
        "northbound": nb.head(30).to_dicts() if len(nb) else [],
    }


@router.get("/sectors")
def sectors(kind_limit: int = Query(default=50, le=200)) -> list[dict]:
    df = _read("sector_daily", kind_limit)
    if not len(df):
        return []
    latest_date = df["trade_date"].max()
    return (df.filter(pl.col("trade_date") == latest_date)
              .sort("change_pct", descending=True).to_dicts())


@router.get("/money-flow")
def money_flow(
    top: int = Query(default=20, le=200),
    symbol: str | None = Query(default=None, max_length=12,
                               description="传了则返回该票的历史资金流而非全市场 Top"),
) -> list[dict]:
    df = _read("money_flow", 1000)
    if not len(df):
        return []
    if symbol:
        sym = resolve_symbol(symbol)
        df = df.filter(pl.col("symbol") == sym)
        return df.sort("trade_date", descending=True).head(30).to_dicts()
    latest_date = df["trade_date"].max()
    return (df.filter(pl.col("trade_date") == latest_date)
              .sort("main_net_inflow", descending=True).head(top).to_dicts())


@router.get("/limit-up")
def limit_up(limit: int = Query(default=50, le=500)) -> list[dict]:
    df = _read("limit_up_pool", 1000)
    if not len(df):
        return []
    latest_date = df["trade_date"].max()
    return (df.filter(pl.col("trade_date") == latest_date)
              .sort("first_limit_time").head(limit).to_dicts())


@router.get("/dragon-tiger")
def dragon_tiger(limit: int = Query(default=50, le=500)) -> list[dict]:
    """龙虎榜：最新交易日上榜个股及原因（空湖返回空列表）。"""
    df = _read("dragon_tiger", 1000)
    if not len(df):
        return []
    latest_date = df["trade_date"].max()
    return (df.filter(pl.col("trade_date") == latest_date)
              .head(limit).to_dicts())


@router.get("/collectors")
def collectors_status() -> list[dict]:
    return list_collectors()


@router.get("/index")
def index_quotes(days: int = Query(default=20, le=250)) -> list[dict]:
    """主要指数最新行情 + 近 N 日收盘序列（来源 index_daily 采集表）。

    无数据时返回空列表 —— 指数日线由「收盘采集」同步，跑一轮即有。
    """
    with reader() as con:
        try:
            df = con.execute(
                "SELECT trade_date, symbol, name, close, pre_close FROM ("
                "  SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rk "
                "  FROM index_daily) WHERE rk = 1").pl()
            hist = con.execute(
                "SELECT trade_date, symbol, close FROM ("
                "  SELECT * FROM index_daily ORDER BY trade_date DESC LIMIT ?)"
                " ORDER BY trade_date", [days * 8]).pl()
        except Exception:  # noqa: BLE001
            return []
    if not len(df):
        return []
    out = []
    for r in df.to_dicts():
        chg = (r["close"] / r["pre_close"] - 1) if r.get("pre_close") else None
        h = hist.filter(pl.col("symbol") == r["symbol"]).sort("trade_date")
        out.append({
            "symbol": r["symbol"], "name": r["name"], "close": r["close"],
            "chg": round(chg, 5) if chg is not None else None,
            "trade_date": str(r["trade_date"]),
            "dates": [str(d) for d in h["trade_date"].to_list()],
            "closes": [round(c, 2) for c in h["close"].to_list()],
        })
    return out


class CollectIn(BaseModel):
    trade_date: str | None = None        # YYYY-MM-DD，缺省=今天
    demo: bool = False                   # 无真实源时用合成数据


@router.post("/collect")
def collect(req: CollectIn) -> dict:
    """手动触发一轮采集（正常由调度器在收盘后自动跑）。

    走 scheduler.collect_and_save：采集+落库+collect_log 健康度记录一条龙。
    """
    from lquant.market.scheduler import collect_and_save

    d = date.fromisoformat(req.trade_date) if req.trade_date else None
    return collect_and_save(schedule=None, trade_date=d, demo=req.demo)


@router.get("/collect-status")
def collect_status() -> dict:
    """采集健康度（M9）：各采集器成功率 / 最后成功 / 当日缺口。"""
    from lquant.market.collect_log import health

    return health()


# ---------------- 市场宽度 / 批量聚合（看板丰富化） ----------------

def _limit_threshold(sym: str) -> float:
    """涨跌停判定阈值（近似）：创业板/科创板 20%，北交所 30%，其余主板 10%。

    北交所代码规则：43 开头（老三板转来）、83/87 开头（新三板精选层/北交所）、
    92 开头（北交所新代码段）。
    """
    bare = sym.split(".", 1)[0]
    if bare.startswith(("43", "83", "87", "92")):
        return 0.295
    if bare.startswith(("300", "301", "688", "689")):
        return 0.195
    return 0.095


@router.get("/breadth")
def breadth(days: int = Query(default=60, le=250)) -> dict:
    """市场宽度：基于数据湖日线截面，全体标的逐日涨跌统计。

    涨跌停用价格变动阈值近似（创业板/科创板 20%、主板 10%），
    与真实涨跌停池（/limit-up，来自采集）互补。
    """
    from lquant.data.store.parquet import read_daily

    df = (read_daily()
          .select(["trade_date", "symbol", "close", "pre_close", "amount"])
          .collect())
    if not len(df):
        return {"latest": None, "history": []}
    df = df.filter(pl.col("pre_close") > 0).with_columns(
        (pl.col("close") / pl.col("pre_close") - 1).alias("chg"),
        pl.col("symbol").map_elements(_limit_threshold, return_dtype=pl.Float64).alias("_lim"),
    )
    daily = df.group_by("trade_date").agg(
        n=pl.len(),
        up=(pl.col("chg") > 0).sum(),
        down=(pl.col("chg") < 0).sum(),
        flat=(pl.col("chg") == 0).sum(),
        limit_up=(pl.col("chg") >= pl.col("_lim") - 0.005).sum(),
        limit_down=(pl.col("chg") <= -(pl.col("_lim") - 0.005)).sum(),
        med_chg=pl.col("chg").median(),
        total_amount=pl.col("amount").sum(),
    ).sort("trade_date")
    history = daily.tail(days).to_dicts()
    for r in history:
        r["trade_date"] = str(r["trade_date"])
        r["up_ratio"] = round(r["up"] / r["n"], 4) if r["n"] else None
        r["med_chg"] = round(r["med_chg"], 5) if r["med_chg"] is not None else None
        r["total_amount"] = round(r["total_amount"], 0)
    return {"latest": history[-1] if history else None, "history": history}


@router.get("/batch")
def batch(
    symbols: str = Query(min_length=6, description="逗号分隔，如 600519,510300.SH,159915"),
    days: int = Query(default=60, le=500),
) -> dict:
    """批量个股/ETF 聚合：单票最新行情 + 等权净值 + 各票归一净值（缺日为 null）。"""
    syms: list[str] = []
    for s in symbols.split(","):
        s = s.strip()
        if s:
            syms.append(resolve_symbol(s))
    if not syms:
        raise HTTPException(422, "symbols 为空")
    if len(syms) > 20:
        raise HTTPException(422, "一次最多 20 只（对比图可读性）")
    syms = sorted(set(syms))

    from lquant.data.store.parquet import read_daily

    df = (read_daily(symbols=syms)
          .select(["trade_date", "symbol", "close", "pre_close", "amount"])
          .collect())
    if not len(df):
        return {"symbols": syms, "latest": [], "summary": None,
                "dates": [], "series": {}, "equal_weight_nav": []}

    # 名称（security 表里查得到就带，查不到留空）
    names: dict[str, str] = {}
    try:
        with reader() as con:
            rows = con.execute(
                "SELECT symbol, name FROM security WHERE symbol IN "
                f"({','.join('?' * len(syms))})", syms).fetchall()
            names = {r[0]: r[1] for r in rows}
    except Exception:  # noqa: BLE001
        pass

    df = df.filter(pl.col("pre_close") > 0).with_columns(
        (pl.col("close") / pl.col("pre_close") - 1).alias("chg"))

    latest = (df.sort("trade_date").group_by("symbol").last()
              .sort("chg", descending=True)
              .select(["symbol", "trade_date", "close", "chg", "amount"]).to_dicts())
    for r in latest:
        r["trade_date"] = str(r["trade_date"])
        r["name"] = names.get(r["symbol"])
        r["amount_yi"] = round((r["amount"] or 0) / 1e8, 2)

    # 归一净值：各票 close/close[首日]，按日期并集对齐；只取最近 days 根
    wide = (df.pivot(values="close", index="trade_date", on="symbol")
            .sort("trade_date").tail(days))
    dates = [str(d) for d in wide["trade_date"].to_list()]
    series: dict[str, list[float | None]] = {}
    for s in syms:
        if s not in wide.columns:
            continue
        vals = wide[s].to_list()
        base = next((v for v in vals if v), None)
        series[s] = [round(v / base, 4) if v and base else None for v in vals]
    # 等权净值：逐日对在场的票取均值（等权再平衡近似）
    if series:
        eq = []
        for i in range(len(dates)):
            xs = [v[i] for v in series.values() if v[i] is not None]
            eq.append(round(sum(xs) / len(xs), 4) if xs else None)
    else:
        eq = []

    chgs = [r["chg"] for r in latest if r["chg"] is not None]
    summary = {
        "n": len(latest),
        "up": sum(1 for c in chgs if c > 0),
        "down": sum(1 for c in chgs if c < 0),
        "avg_chg": round(sum(chgs) / len(chgs), 5) if chgs else None,
        "best": latest[0] if latest else None,
        "worst": latest[-1] if latest else None,
    }
    return {"symbols": syms, "latest": latest, "summary": summary,
            "dates": dates, "series": series, "equal_weight_nav": eq}


# ---------------- 快照 / 热榜 / 调度（看板补充） ----------------

@router.get("/snapshot")
def snapshot(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=300),
    sort: str = Query(default="change_pct", pattern="^(change_pct|amount|turnover_rate)$"),
) -> dict:
    """全市场最新日度截面，分页。前端做涨跌排序表 / 快速筛选。"""
    df = _daily_aggregate()
    if not len(df):
        return {"trade_date": None, "total": 0, "page": page, "size": size, "rows": []}
    df = df.filter(pl.col("pre_close") > 0).with_columns(
        (pl.col("close") / pl.col("pre_close") - 1).alias("change_pct"))
    latest = df["trade_date"].max()
    cur = df.filter(pl.col("trade_date") == latest).sort(sort, descending=True)
    total = cur.height
    rows = cur.slice((page - 1) * size, size).to_dicts()
    for r in rows:
        r["trade_date"] = str(latest)
        # 最新日 close 可能为空 → change_pct None，round(None) 会炸这一页。
        r["change_pct"] = round(r["change_pct"], 5) if r["change_pct"] is not None else None
    return {"trade_date": str(latest), "total": total, "page": page, "size": size,
            "rows": rows}


@router.get("/heat")
def heat(top: int = Query(default=15, ge=1, le=100)) -> dict:
    """热榜：涨/跌 Top、放量 Top、龙虎榜（看板当日要点）。"""
    df = _daily_aggregate()
    base: dict = {"gainers": [], "losers": [], "volume": [], "dragon_tiger": []}
    if len(df):
        df = df.filter(pl.col("pre_close") > 0).with_columns(
            (pl.col("close") / pl.col("pre_close") - 1).alias("change_pct"))
        latest = df["trade_date"].max()
        cur = df.filter(pl.col("trade_date") == latest)
        base["gainers"] = heat_rows(cur.sort("change_pct", descending=True).head(top), latest)
        base["losers"] = heat_rows(cur.sort("change_pct").head(top), latest)
        base["volume"] = heat_rows(
            cur.sort("amount", descending=True).head(top), latest)
    try:
        dt = _read("dragon_tiger", top)
        if len(dt):
            d = dt["trade_date"].max()
            base["dragon_tiger"] = [
                {"symbol": r["symbol"], "trade_date": str(d),
                 "name": r.get("name"), "change_pct": r.get("change_pct"),
                 "reason": r.get("reason")}
                for r in dt.filter(pl.col("trade_date") == d).to_dicts()][:top]
    except Exception:  # noqa: BLE001 - 表缺失按空态处理
        pass
    return base


def _daily_aggregate() -> pl.DataFrame:
    """数据湖日线聚合（含空湖/空 schema 兜底）：空湖读 read_daily 直接炸列）。"""
    from lquant.data.store.parquet import read_daily

    try:
        df = (read_daily()
              .select(["trade_date", "symbol", "close", "pre_close", "amount",
                       "turnover_rate"])
              .collect())
        return df if len(df) else pl.DataFrame()
    except Exception:  # noqa: BLE001 - 湖未初始化/空 schema，按空态处理
        return pl.DataFrame()


def heat_rows(cur: pl.DataFrame, latest) -> list[dict]:
    out = []
    for r in cur.to_dicts():
        chg = r["change_pct"] if r["change_pct"] is not None else None
        out.append({"symbol": r["symbol"], "trade_date": str(latest),
                    "change_pct": round(chg, 5) if chg is not None else None,
                    "amount": round(r["amount"], 0) if r["amount"] is not None else None,
                    "turnover": r["turnover_rate"]})
    return out


@router.get("/schedules")
def schedules() -> dict:
    """采集时点 + 当前时刻应跑哪些（调度器状态，settings/看板用）。"""
    from lquant.market.scheduler import SCHEDULES, due_schedules, status

    now = __import__("datetime").datetime.now()
    due = due_schedules(now)
    items = [{"name": k, "time": v["time"], "desc": v["desc"], "due": k in due}
             for k, v in SCHEDULES.items()]
    cov = status()
    return {"schedules": items, "now": now.isoformat(timespec="minutes"),
            "coverage": cov.to_dicts() if len(cov) else []}
