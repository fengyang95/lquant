"""自定义分析库 API —— CRUD，保存前跑 run_user_analysis 冒烟。"""
from __future__ import annotations

import polars as pl
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/analyses", tags=["analyses"])

# 冒烟用最小空数据 payload（run_user_analysis 契约）
_EMPTY_PAYLOAD = {
    "dates": [], "nav": [], "returns": [], "trades": pl.DataFrame(),
    "positions": {}, "records": {}, "metrics": {},
}


class AnalysisIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=10, max_length=200_000)


class AnalysisSourceIn(BaseModel):
    """PUT 用 source-only body。"""
    source: str = Field(min_length=10, max_length=200_000)


def _smoke(source: str) -> None:
    """保存前冒烟：validate + run_user_analysis（空数据 payload）。"""
    from lquant.backtest.analysis import run_user_analysis
    from lquant.backtest.validation import validate_source

    errs = validate_source(source, require_initialize=False)
    if errs:
        raise HTTPException(422, "；".join(errs))
    try:
        run_user_analysis(source, _EMPTY_PAYLOAD)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _get_live(aid: str) -> dict:
    """get_analysis 不过滤软删，存活校验用 list_analyses 的过滤口径（按 id）。"""
    from lquant.backtest.strategy_store import get_analysis, list_analyses

    if not any(a["id"] == aid for a in list_analyses()):
        raise KeyError(aid)
    return get_analysis(aid)


@router.get("")
def list_analyses_api() -> list[dict]:
    from lquant.backtest.strategy_store import list_analyses

    return list_analyses()


@router.post("")
def create_analysis(req: AnalysisIn) -> dict:
    from lquant.backtest.strategy_store import save_analysis

    _smoke(req.source)
    return save_analysis(req.name, req.source)


@router.get("/{aid}")
def get_analysis_api(aid: str) -> dict:
    try:
        return _get_live(aid)
    except KeyError as e:
        raise HTTPException(404, f"分析不存在: {aid}") from e


@router.put("/{aid}")
def update_analysis(aid: str, req: AnalysisSourceIn) -> dict:
    """PUT = 同名再存一条并软删旧条（分析无版本概念，等效于更新）。

    存活校验按 name 口径：id 已被软删但同名仍有存活条 → 等效更新仍可用；
    整个 name 已软删 → 404，PUT 不复活。
    """
    from lquant.backtest.strategy_store import delete_analysis, get_analysis, list_analyses, save_analysis

    try:
        cur = get_analysis(aid)
        if not any(a["name"] == cur["name"] for a in list_analyses()):
            raise KeyError(aid)
    except KeyError as e:
        raise HTTPException(404, f"分析不存在: {aid}") from e
    _smoke(req.source)
    saved = save_analysis(cur["name"], req.source)
    delete_analysis(aid)
    return saved


@router.delete("/{aid}")
def delete_analysis_api(aid: str) -> dict:
    from lquant.backtest.strategy_store import delete_analysis, get_analysis

    try:
        get_analysis(aid)  # id 级校验：重复 DELETE 已替换/软删的 id 保持幂等 200
    except KeyError as e:
        raise HTTPException(404, f"分析不存在: {aid}") from e
    delete_analysis(aid)
    return {"deleted": aid}
