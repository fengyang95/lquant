"""基本面服务：行业相对分位评分 + 三表勾稽。

设计约定：

- **PIT 优先**：所有取数强制 ``pub_date <= asof``（见 ``lquant.fundamental.panel``）。
  公告日之前查不到就是查不到，绝不返回「最新一期」充数 —— 回测里的前视偏差
  绝大多数就是从这种「顺手取最新」来的。
- **空表是常态不是错误**：财务表没同步过时返回空结构 + ``hint``，
  而不是 500。前端据此提示「先同步财务数据」。
- **分位必须用全市场截面算**：即使只查一只票，也要先把全表喂给分位计算，
  再筛出目标票 —— 否则「行业分位」会退化成「自己跟自己比」。
"""
from __future__ import annotations

from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lquant.core.db import reader
from lquant.core.types import today_cn
from lquant.fundamental import (
    METRICS,
    MODULE_LABELS,
    MODULE_WEIGHTS,
    aggregate,
    percentile_table,
    reconcile,
    score_universe,
)

router = APIRouter(prefix="/fundamental", tags=["fundamental"])

_ITEMS: tuple[str, ...] = tuple(m.item for m in METRICS)

#: 财务表没数据时的统一提示（前端据此引导用户去同步）
_EMPTY_HINT = "财务数据为空：先执行 `lq data financial --symbols ...` 回填 PIT 财务"


def _load_financial(con, asof: date, items: tuple[str, ...]) -> pl.DataFrame:
    """读取 ``pub_date <= asof`` 的 PIT 财务长表。"""
    placeholders = ",".join("?" * len(items))
    sql = (f"SELECT symbol, stat_date, pub_date, item, value FROM financial_pit "
           f"WHERE pub_date <= ? AND item IN ({placeholders})")
    try:
        return con.execute(sql, [asof, *items]).pl()
    except Exception:  # noqa: BLE001 - 表未建/未同步：返回空而不是 500
        return pl.DataFrame()


def _load_industry(con, asof: date) -> pl.DataFrame:
    sql = ("SELECT symbol, std, code, name, std_date FROM industry_classify "
           "WHERE std_date <= ?")
    try:
        return con.execute(sql, [asof]).pl()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _json_safe(obj):
    """NaN/Inf → None（前端 JSON.parse 不接受字面量 NaN）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if hasattr(obj, "item") and not isinstance(obj, (int, float)):
        try:
            return _json_safe(obj.item())
        except Exception:  # noqa: BLE001
            return str(obj)
    if isinstance(obj, float):
        import math
        return obj if math.isfinite(obj) else None
    return obj


@router.get("/metrics")
def list_metrics() -> dict:
    """指标目录 + 模块权重（前端据此渲染分组与满分）。"""
    return {
        "modules": [{"name": k, "label": MODULE_LABELS[k], "weight": v}
                    for k, v in MODULE_WEIGHTS.items()],
        "metrics": [{"item": m.item, "label": m.label, "module": m.module,
                     "max_score": m.max_score, "higher_better": m.higher_better,
                     "direction": m.direction} for m in METRICS],
    }


@router.get("/score")
def score_one(
    symbol: str = Query(min_length=6, max_length=16),
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    min_samples: int = Query(default=5, ge=2, le=100),
) -> dict:
    """单只股票的基本面评分（含逐指标明细与行业分位）。"""
    from lquant.server.deps import resolve_symbol

    day = date.fromisoformat(asof) if asof else today_cn()
    sym = resolve_symbol(symbol)
    with reader() as con:
        panel = _load_financial(con, day, _ITEMS)
        industry = _load_industry(con, day)
    if panel.is_empty() or industry.is_empty():
        return {"symbol": sym, "asof": day.isoformat(), "available": False,
                "hint": _EMPTY_HINT, "score": None, "items": []}

    detail = score_universe(panel, industry, day, min_samples=min_samples)
    if detail.is_empty():
        return {"symbol": sym, "asof": day.isoformat(), "available": False,
                "hint": "该观察日无可用财务数据（或行业分位样本不足）",
                "score": None, "items": []}

    snap = aggregate(detail)
    mine = snap.filter(pl.col("symbol") == sym)
    if mine.is_empty():
        return {"symbol": sym, "asof": day.isoformat(), "available": False,
                "hint": "该标的没有财务数据，或未被行业分类覆盖",
                "score": None, "items": []}

    row = mine.row(0, named=True)
    items = detail.filter(pl.col("symbol") == sym).sort(["module", "item"])
    return _json_safe({
        "symbol": sym,
        "asof": day.isoformat(),
        "available": True,
        "score": row,
        "items": items.to_dicts(),
    })


class UniverseIn(BaseModel):
    asof: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    symbols: list[str] = Field(default_factory=list, max_length=2000)
    min_samples: int = Field(default=5, ge=2, le=100)
    min_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=100, ge=1, le=1000)


@router.post("/scores")
def score_many(req: UniverseIn) -> dict:
    """全市场（或指定股票池）基本面评分排名。

    ``min_coverage`` 用来剔除「只匹配到两三个指标就拿了高分」的票 ——
    归一化分数跨覆盖度可比，但低覆盖度的高分不具备可比的可信度。
    """
    day = date.fromisoformat(req.asof) if req.asof else today_cn()
    with reader() as con:
        panel = _load_financial(con, day, _ITEMS)
        industry = _load_industry(con, day)
    if panel.is_empty() or industry.is_empty():
        return {"asof": day.isoformat(), "available": False, "hint": _EMPTY_HINT,
                "n_scored": 0, "rows": []}

    detail = score_universe(panel, industry, day, min_samples=req.min_samples)
    if detail.is_empty():
        return {"asof": day.isoformat(), "available": False,
                "hint": "该观察日无可用财务数据（或行业分位样本不足）",
                "n_scored": 0, "rows": []}

    snap = aggregate(detail)
    if req.symbols:
        wanted = {s.strip().upper() for s in req.symbols}
        snap = snap.filter(pl.col("symbol").is_in(list(wanted)))
    snap = snap.filter(pl.col("coverage") >= req.min_coverage)
    snap = snap.sort("normalized_score", descending=True).head(req.limit)
    return _json_safe({
        "asof": day.isoformat(),
        "available": True,
        "n_scored": snap.height,
        "rows": snap.to_dicts(),
    })


@router.get("/percentiles")
def percentiles(
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    min_samples: int = Query(default=5, ge=2, le=100),
) -> dict:
    """各行业 × 各指标的 P25/P50/P75 分位表（用于解释某个分数是怎么来的）。"""
    day = date.fromisoformat(asof) if asof else today_cn()
    with reader() as con:
        panel = _load_financial(con, day, _ITEMS)
        industry = _load_industry(con, day)
    if panel.is_empty() or industry.is_empty():
        return {"asof": day.isoformat(), "available": False, "hint": _EMPTY_HINT,
                "rows": []}
    tbl = percentile_table(panel, industry, day, min_samples=min_samples)
    return _json_safe({"asof": day.isoformat(), "available": not tbl.is_empty(),
                       "rows": tbl.to_dicts()})


class ReconcileIn(BaseModel):
    symbol: str = Field(min_length=6, max_length=16)
    net_income: float | None = None
    other_comprehensive: float | None = None
    delta_retained: float | None = None
    operating_cashflow: float | None = None
    cashflow_net_change: float | None = None
    balance_cash_change: float | None = None
    deducted_net_income: float | None = None


@router.post("/reconcile")
def run_reconcile(req: ReconcileIn) -> dict:
    """三表勾稽校验（留存收益 / 现金变动 / 利润质量）。

    输入缺项一律记为「未检查」，**不计入通过判定也不给分** ——
    数据缺失不能被当成质量优秀。
    """
    from lquant.server.deps import resolve_symbol

    if not req.symbol.strip():
        raise HTTPException(422, "symbol 不能为空")
    report = reconcile(
        resolve_symbol(req.symbol),
        net_income=req.net_income,
        other_comprehensive=req.other_comprehensive,
        delta_retained=req.delta_retained,
        operating_cashflow=req.operating_cashflow,
        cashflow_net_change=req.cashflow_net_change,
        balance_cash_change=req.balance_cash_change,
        deducted_net_income=req.deducted_net_income,
    )
    return _json_safe(report.to_dict())


__all__ = ["router"]
