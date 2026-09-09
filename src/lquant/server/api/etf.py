"""ETF 专区 API（/etf）：元数据 + 相关性，走统一封套。

附录 A：ETF 元数据字段（折溢价、跟踪指数、T+N）—— etf_meta 表。
correlation：基于日线收益率两两相关，样本不足时返回空并附说明（不 500）。
ETF 源数据由数据采集/同步负责；这里只读，缺数据即空态。
"""
from __future__ import annotations

from fastapi import HTTPException, Query

from lquant.core.db import reader
from lquant.server.envelope import make_router

router = make_router(prefix="/etf", tags=["etf"])

_META_COLS = ["symbol", "name", "track_index", "fund_type", "is_cross_border",
              "sellable_after_days", "management_fee", "fund_size", "as_of"]


def _meta_rows(q: str | None = None, limit: int = 200,
               symbol: str | None = None) -> list[dict]:
    """读 etf_meta 表（纯逻辑，供 route 与 by-symbol 复用——不可直接调 route 函数，
    会绕过 FastAPI 的 Query 默认值注入，导致 int(Query(...)) 报错）。

    symbol 给定则只按代码前缀精确过滤（by-symbol 命中无须受 200 行 LIMIT 限制）。
    """
    sql = ("SELECT symbol, name, track_index, fund_type, is_cross_border, "
           "sellable_after_days, management_fee, custody_fee, fund_size, source, as_of "
           "FROM etf_meta")
    params: list = []
    if symbol:
        sql += " WHERE symbol LIKE ?"
        params.append(f"{symbol}%")
    elif q:
        sql += " WHERE symbol LIKE ? OR name LIKE ?"
        params += [f"%{q}%", f"%{q}%"]
    # symbol 过滤时只可能命中 ≤1 行，不套 LIMIT，避免第 200 只之后误 404。
    if not symbol:
        sql += " ORDER BY symbol"
        sql += f" LIMIT {int(limit)}"
    try:
        with reader() as con:
            rows = con.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 - 表缺失/未同步
        return []
    out = []
    for r in rows:
        out.append({"symbol": r[0], "name": r[1], "track_index": r[2],
                    "fund_type": r[3], "is_cross_border": bool(r[4]),
                    "sellable_after_days": r[5],
                    "management_fee": r[6], "custody_fee": r[7],
                    "fund_size": r[8], "source": r[9],
                    "as_of": str(r[10]) if r[10] else None})
    return out


@router.get("/meta")
def etf_meta(
    q: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=200, le=1000),
) -> list[dict]:
    """ETF 元数据列表（代码/跟踪指数/规模/申赎 T+N）。q 过滤代码或名称前缀。"""
    return _meta_rows(q=q, limit=limit)


@router.get("/by-symbol/{symbol}")
def meta_by_symbol(symbol: str) -> dict:
    """单只 ETF 元数据（个股 ETF 详情页用），无则 404。"""
    from lquant.server.deps import bare_code

    code = bare_code(symbol).upper()
    rows = _meta_rows(symbol=code)          # 代码前缀精确查，不受默认 200 行限制
    hit = next((r for r in rows if bare_code(r["symbol"]).upper() == code), None)
    if hit is None:
        raise HTTPException(404, f"无 {symbol} 的 ETF 元数据（先同步 ETF 元数据）")
    return hit


@router.get("/correlation")
def correlation(
    horizon: int = Query(default=60, ge=30, le=500),
    min_symbols: int = Query(default=2, ge=2, le=100),
) -> dict:
    """ETF 日收益率相关性矩阵。样本不足返回空 pairs + 说明，不 500。"""
    from lquant.data.store.parquet import read_daily

    try:
        df = (read_daily()
              .select(["trade_date", "symbol", "close", "pre_close"])
              .collect())
    except Exception:  # noqa: BLE001 - 湖空/路径缺失
        df = None
    if df is None or not len(df):
        return {"symbols": [], "pairs": [], "note": "数据湖无行情，先同步日线"}
    from lquant.server.deps import bare_code

    def is_etf(sym: str) -> bool:
        c = bare_code(sym)
        # 510/511/512/513/515/516/518 沪 ETF、159 深 ETF、560 深创新 ETF
        return c.startswith(("510", "511", "512", "513", "515", "516", "518",
                             "159", "560"))

    # 先筛到 ETF 再 pivot：避免 O(dates × 全市场) 大宽表。
    etf_syms = sorted(s for s in df["symbol"].unique().to_list() if is_etf(s))
    if len(etf_syms) < min_symbols:
        return {"symbols": etf_syms, "pairs": [],
                "note": f"仅 {len(etf_syms)} 只 ETF 有日线，需 ≥ {min_symbols} 只计算相关"}
    from polars import col

    # pivot 保留首现顺序而非时间序 → 先 sort("trade_date") 再 tail 才是「最近 N 日」。
    ef = (df.filter(col("pre_close") > 0, col("close") > 0)   # close<=0 → pct_change 出 inf
          .filter(col("symbol").is_in(etf_syms)).sort("trade_date"))
    px = ef.pivot(values="close", index="trade_date", on="symbol").tail(horizon)
    cols = [c for c in px.columns if c != "trade_date"]        # 全为 ETF，顺序=pivot 列序
    # polars 1.4x 无 DataFrame.pct_change（仅 Expr）—— 用 select 逐列计算日收益率；
    # corr().row(i)[j] == corr(cols[i], cols[j])，三角展开保持 cols 序。
    ret = px.select([col(c).pct_change() for c in cols]).drop_nulls()
    if len(ret) < horizon // 2:
        return {"symbols": cols, "pairs": [],
                "note": f"有效收益率仅 {len(ret)} 根，不足以计算"}
    import math

    corr = ret.corr()                       # polars DataFrame 相关
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.row(i)[j]
            # 零方差/停牌 → NaN，is not None 挡不住，须 isfinite 否则裸 NaN 破坏 JSON。
            if v is not None and math.isfinite(v):
                pairs.append({"a": cols[i], "b": cols[j], "corr": round(float(v), 4)})
    pairs.sort(key=lambda p: -p["corr"])
    return {"symbols": cols, "pairs": pairs, "note": None}