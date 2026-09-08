"""自选股：DuckDB 小表 CRUD。

放置约定：自选股是「用户态数据」，与行情/因子无关，独立一张表；
每请求懒建表（CREATE IF NOT EXISTS 开销可忽略，且免去迁移脚本）。
列表接口顺带带出每只票最近收盘价与涨跌幅（读 Parquet 湖），
前端自选页首屏不用再逐票请求。
"""
from __future__ import annotations

import polars as pl
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lquant.core.db import reader, writer
from lquant.data.store.parquet import read_daily
from lquant.server.deps import resolve_symbol

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

_DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol    VARCHAR PRIMARY KEY,
    name      VARCHAR,
    note      VARCHAR,
    added_at  TIMESTAMP
)
"""


def _ensure() -> None:
    with writer() as con:
        con.execute(_DDL)


class WatchIn(BaseModel):
    symbol: str = Field(min_length=6, max_length=16)
    note: str = ""


def _lookup_name(symbol: str) -> str | None:
    try:
        with reader() as con:
            r = con.execute("SELECT name FROM security WHERE symbol = ?", [symbol]).fetchone()
        return r[0] if r else None
    except Exception:  # noqa: BLE001
        return None


def _last_quotes(symbols: list[str]) -> dict[str, dict]:
    """每票最近两根日线 → 最新收盘与涨跌幅。空湖返回空 dict。"""
    if not symbols:
        return {}
    try:
        df = read_daily(symbols).select(
            ["symbol", "trade_date", "close"]).collect()
        if not len(df):
            return {}
        out: dict[str, dict] = {}
        for (sym,), g in df.group_by("symbol"):
            g = g.sort("trade_date").tail(2)
            rows = g.to_dicts()
            last = rows[-1]
            prev = rows[-2] if len(rows) > 1 else None
            chg = (last["close"] / prev["close"] - 1) if prev and prev["close"] else None
            out[sym] = {"close": last["close"], "trade_date": str(last["trade_date"]),
                        "change_pct": round(chg, 4) if chg is not None else None}
        return out
    except Exception:  # noqa: BLE001
        return {}


@router.get("")
def list_watchlist() -> list[dict]:
    _ensure()
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT symbol, name, note, added_at FROM watchlist ORDER BY added_at DESC"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
    quotes = _last_quotes([r[0] for r in rows])
    out = []
    for sym, name, note, added in rows:
        q = quotes.get(sym, {})
        out.append({"symbol": sym, "name": name, "note": note,
                    "added_at": str(added), "close": q.get("close"),
                    "trade_date": q.get("trade_date"),
                    "change_pct": q.get("change_pct")})
    return out


@router.post("")
def add(item: WatchIn) -> dict:
    _ensure()
    sym = resolve_symbol(item.symbol)
    # 先查名再进 writer：writer 连接内再开 reader 会因 DuckDB 文件锁静默失败
    name = _lookup_name(sym)
    with writer() as con:
        dup = con.execute("SELECT 1 FROM watchlist WHERE symbol = ?", [sym]).fetchone()
        if dup:
            raise HTTPException(409, f"{sym} 已在自选")
        con.execute(
            "INSERT INTO watchlist (symbol, name, note, added_at) VALUES (?, ?, ?, now())",
            [sym, name, item.note],
        )
    return {"added": sym, "name": name}


@router.delete("/{symbol}")
def remove(symbol: str) -> dict:
    _ensure()
    sym = resolve_symbol(symbol)
    # DELETE 的 fetchall() 恒返回计数行，不能当「删没删到」的判据；
    # 用 RETURNING 拿实际删掉的行
    with writer() as con:
        n = con.execute(
            "DELETE FROM watchlist WHERE symbol = ? RETURNING symbol", [sym]
        ).fetchall()
    if not n:
        raise HTTPException(404, f"{sym} 不在自选")
    return {"removed": sym}
