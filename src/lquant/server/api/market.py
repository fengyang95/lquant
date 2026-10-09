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
from lquant.core.types import now_cn_naive
from lquant.market.collectors import list_collectors
from lquant.market.collectors.northbound import NORTHBOUND_NET_LAST_DATE
from lquant.server.deps import resolve_symbol

router = APIRouter(prefix="/market", tags=["market"])


def _latest_day(table: str, limit: int = 200) -> pl.DataFrame:
    """按 trade_date 取表；同日多次采集只留最新一条。

    northbound_flow 的主键是 (trade_date, ts) —— ts 是为「盘中有更新」设计的，
    而北向自 2024-05 起取消盘中实时，一天采两次就会留两行同日记录。
    看板要的是「每日一行」，重复行会把近 N 日窗口挤掉。
    """
    df = _read(table, limit)
    if not len(df) or "trade_date" not in df.columns or "ts" not in df.columns:
        return df
    return df.sort("ts").unique(subset=["trade_date"], keep="last",
                                maintain_order=True).sort("trade_date", descending=True)


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
    nb = _latest_day("northbound_flow", 90)
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
def sectors(kind: str = Query(default="industry",
                              pattern="^(industry|concept|area)$")) -> list[dict]:
    """板块行情，按 kind 过滤（行业/概念/地域）。

    每种 kind 取各自的最新交易日（部分 kind 某天采集失败不影响其他 kind）。
    kind 列是后加的：老库没有该列时用 COALESCE(kind,'industry') 兜底，
    兼容 ALTER 之前的历史数据（历史上只采过行业）。
    """
    try:
        with reader() as con:
            df = con.execute(
                "SELECT * FROM sector_daily WHERE COALESCE(kind, 'industry') = ? "
                "AND trade_date = (SELECT MAX(trade_date) FROM sector_daily "
                "WHERE COALESCE(kind, 'industry') = ?)",
                [kind, kind],
            ).pl()
    except Exception:  # noqa: BLE001 - 表未建/结构迁移中 → 空态
        return []
    if not len(df):
        return []
    return df.sort("change_pct", descending=True).to_dicts()


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


@router.get("/northbound")
def northbound(days: int = Query(default=30, le=250)) -> dict:
    """北向资金数据面。

    `flow`：成交额/笔数（净买额 2024-08-19 起停发，那之后 `*_net_inflow` 恒为
    null 且 `net_published=false`）；`top10`：最新交易日前十大成交活跃证券。
    """
    flow = _latest_day("northbound_flow", max(days * 3, 30))
    top10 = _read("northbound_top10", 40)
    return {
        "flow": flow.head(days).to_dicts() if len(flow) else [],
        "top10": top10.to_dicts() if len(top10) else [],
        "net_last_date": NORTHBOUND_NET_LAST_DATE.isoformat(),
    }


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

    try:
        d = date.fromisoformat(req.trade_date) if req.trade_date else None
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail=f"trade_date 格式非法（需 YYYY-MM-DD）: {req.trade_date}") from e
    return collect_and_save(schedule=None, trade_date=d, demo=req.demo)


@router.post("/backfill")
def backfill(req: CollectIn) -> dict:
    """手动触发大盘数据缺失检查与补齐。

    正常由 sync 作业 backfill（盘前 09:10）与 API 启动线程自动触发；
    部署后想立刻补数据也可以手动调这个接口。
    """
    from lquant.market.backfill import ensure_market_coverage

    try:
        return ensure_market_coverage(days=90, demo=req.demo)
    except Exception as e:  # noqa: BLE001 - 日历源故障时给出可读错误而非裸 500
        raise HTTPException(status_code=503, detail=f"补齐失败: {e}") from e


@router.get("/collect-status")
def collect_status() -> dict:
    """采集健康度（M9）：各采集器成功率 / 最后成功 / 当日缺口。"""
    from lquant.market.collect_log import health

    return health()


# ---------------- 市场宽度 / 批量聚合（看板丰富化） ----------------

def _ratio_expr(col: str, mapping: dict[str, float]):
    """把 {symbol: 比例} 映射成列表达式（未登记标的 → null，不猜）。"""
    return pl.col(col).replace_strict(mapping, default=None)


def _limit_ratio_map(symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """按规则表算 per-symbol 涨跌幅比例 → (非 ST 比例表, ST 比例表)。

    此前这里硬编码「300/301/688/689 → 20%，43/83/87/92 → 30%，其余 10%」，
    是**第四份**重复实现，且漏了：
    - 主板 ST 的 5%：ST 股涨停会被漏计 → 涨跌停家数系统性偏低；
    - ETF 按跟踪指数：588 科创 ETF 实际 20%，却被当成主板 10% → 计数偏高。
    改为复用 `build_rules`（与回测/模拟盘同一份规则表）。
    """
    from lquant.backtest.engine import build_rules

    base: dict[str, float] = {}
    st: dict[str, float] = {}
    for sym, r in build_rules(symbols).items():
        base[sym] = r.limit_ratio(is_st=False) or float("nan")
        st[sym] = r.limit_ratio(is_st=True) or float("nan")
    return base, st


@router.get("/breadth")
def breadth(days: int = Query(default=60, le=250)) -> dict:
    """市场宽度：基于数据湖日线截面，全体标的逐日涨跌统计。

    涨跌停用价格变动阈值近似（创业板/科创板 20%、主板 10%），
    与真实涨跌停池（/limit-up，来自采集）互补。
    """
    from lquant.data.store.parquet import read_daily

    try:
        lf = read_daily()
        # is_st 是可选列：老湖/合成湖可能没有，缺则补 null（= 未知 → 按非 ST 阈值），
        # 不能因为少一列就让整个市场宽度接口退化成 503/None
        cols = lf.collect_schema().names()
        want = ["trade_date", "symbol", "close", "pre_close", "amount"]
        df = lf.select([*want, "is_st"] if "is_st" in cols else want).collect()
        if "is_st" not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_st"))
    except Exception:  # noqa: BLE001 - 湖缺失/损坏时与 _daily_aggregate 同款兜底
        return {"latest": None, "history": []}
    if not len(df):
        return {"latest": None, "history": []}
    # 涨跌停阈值来自规则表（含 ST 分板 / ETF 跟踪指数），并按当日 is_st 切换
    base_map, st_map = _limit_ratio_map(df["symbol"].unique().to_list())
    df = df.filter(pl.col("pre_close") > 0).with_columns(
        (pl.col("close") / pl.col("pre_close") - 1).alias("chg"),
        pl.when(pl.col("is_st").fill_null(False))
          .then(_ratio_expr("symbol", st_map))
          .otherwise(_ratio_expr("symbol", base_map))
          .alias("_lim"),
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

    now = now_cn_naive()
    due = due_schedules(now)
    items = [{"name": k, "time": v["time"], "desc": v["desc"], "due": k in due}
             for k, v in SCHEDULES.items()]
    cov = status()
    return {"schedules": items, "now": now.isoformat(timespec="minutes"),
            "coverage": cov.to_dicts() if len(cov) else []}
