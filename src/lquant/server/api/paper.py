"""模拟盘：回放 / 持仓 / 净值 / 与回测对拍。

进程内保存最近一次回放（session 级）。
正式部署时模拟盘是常驻进程（盘中接行情推送），
这里的 replay 接口用于链路验证与教学演示。
"""
from __future__ import annotations

import polars as pl
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lquant.core.db import reader
from lquant.data.store.parquet import read_daily
from lquant.paper import PaperConfig, PaperEngine, compare_nav, compare_trades

router = APIRouter(prefix="/paper", tags=["paper"])

# 进程内缓存最近一次回放（重启即失，正式部署持久化到 paper 表）
_last: dict = {}


class ReplayIn(BaseModel):
    top_n: int = Field(default=5, ge=1, le=50)
    start: str = "2026-01-01"
    factor: str = "mom_20"
    formula: str = "pct_change_20"
    initial_cash: float = Field(default=1_000_000, gt=0)


class _TopNProbe:
    """演示策略：首日按因子排名建仓 TopN，之后持有。

    正式的模拟盘策略与回测共用同一份信号源（这是对拍有意义的前提）。
    """

    def __init__(self, ranked_first_day: list[str]) -> None:
        self.queue = list(ranked_first_day)
        self.done = False

    def signals(self, broker, quote: dict) -> list[dict]:
        if self.done or quote["symbol"] != (self.queue[0] if self.queue else None):
            return []
        self.done = True
        out = []
        for s in self.queue:
            q = int(broker.cash / len(self.queue) / (quote["price"] * 1.01)) // 100 * 100
            if q > 0:
                out.append({"symbol": s, "side": "buy", "qty": q, "price": quote["price"]})
        return out


def _compute_factor(df: pl.DataFrame, formula: str) -> pl.DataFrame:
    col = formula.replace("_", "")
    if formula.startswith("pct_change_"):
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change(n).over("symbol").alias(col))
    if formula.startswith("rolling_std_"):
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change().over("symbol").rolling_std(n).alias(col))
    raise HTTPException(422, f"暂不支持的因子公式: {formula}")


@router.post("/replay")
def replay(req: ReplayIn) -> dict:
    """离线回放一段日线，验证模拟盘链路（撮合/T+N/费率/拒单）。"""
    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空")
    col = req.formula.replace("_", "")
    d = _compute_factor(df, req.formula).drop_nulls([col])

    # 首日因子排名 → 建仓篮子
    first_day = d["trade_date"].min()
    ranked = (d.filter(pl.col("trade_date") == first_day)
                .sort(col, descending=True)["symbol"].head(req.top_n).to_list())
    if not ranked:
        raise HTTPException(422, "首日无可用因子值")

    eng = PaperEngine(_TopNProbe(ranked),
                      PaperConfig(initial_cash=req.initial_cash))
    summary = eng.replay(d.filter(pl.col("symbol").is_in(ranked)))

    nav_df = pl.DataFrame({"trade_date": [r["trade_date"] for r in eng.nav_series],
                           "nav": [r["nav"] for r in eng.nav_series]})
    _last.clear()
    _last.update({"nav": nav_df, "orders": eng.orders_frame(), "summary": summary})

    return {
        "summary": summary,
        "positions": eng.broker.positions_frame().to_dicts(),
        "nav": nav_df.to_dicts(),
        "alerts": eng.alerts,
    }


@router.get("/state")
def state() -> dict:
    """最近一次回放的状态（重启后为空）。"""
    if not _last:
        return {"has_state": False}
    return {
        "has_state": True,
        "summary": _last["summary"],
        "positions_head": _last.get("positions", [])[:20] if "positions" in _last else [],
    }


class CompareIn(BaseModel):
    run_id: str


@router.post("/compare")
def compare_with_backtest(req: CompareIn) -> dict:
    """模拟盘回放 vs 回测对拍。"""
    if not _last:
        raise HTTPException(409, "先 POST /api/paper/replay 生成模拟盘净值")
    with reader() as con:
        rows = con.execute(
            "SELECT trade_date, nav FROM backtest_nav WHERE run_id = ? ORDER BY trade_date",
            [req.run_id]).fetchall()
        order_rows = con.execute(
            "SELECT ts, symbol, side, qty FROM backtest_order WHERE run_id = ?",
            [req.run_id]).fetchall()
    if not rows:
        raise HTTPException(404, f"回测不存在或无净值: {req.run_id}")
    bt_nav = pl.DataFrame({"trade_date": [r[0] for r in rows], "nav": [r[1] for r in rows]})
    bt_orders = pl.DataFrame({"ts": [str(r[0]) for r in order_rows],
                              "symbol": [r[1] for r in order_rows],
                              "side": [r[2] for r in order_rows],
                              "qty": [r[3] for r in order_rows]})
    rep = compare_nav(bt_nav, _last["nav"])
    trades_cmp = compare_trades(bt_orders, _last["orders"])
    return {"nav_deviation": rep.as_dict(), "trade_comparison": trades_cmp}
