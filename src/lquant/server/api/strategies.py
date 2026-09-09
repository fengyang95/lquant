"""策略库 / 版本化 CRUD + 校验。用户策略与内置注册策略在列表合并暴露。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lquant.backtest.strategy import STRATEGIES

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=10, max_length=200_000)
    description: str = ""
    config: dict = {}
    benchmark: str | None = None


class StrategySourceIn(BaseModel):
    """PUT 用 body（name 沿用现有策略，不可改名）。"""
    source: str = Field(min_length=10, max_length=200_000)
    description: str = ""
    config: dict = {}
    benchmark: str | None = None


class ValidateIn(BaseModel):
    source: str = Field(min_length=1, max_length=200_000)


def _get_live(sid: str) -> dict:
    """get_strategy 不过滤软删，存活校验用 list_strategies 的过滤口径。"""
    from lquant.backtest.strategy_store import get_strategy, list_strategies

    if not any(s["id"] == sid for s in list_strategies()):
        raise KeyError(sid)
    return get_strategy(sid)


@router.get("")
def list_strategies_api() -> list[dict]:
    """策略列表：内置注册策略（source=builtin）+ 用户策略库（source=user）。"""
    from lquant.backtest.strategy_store import list_strategies

    names = STRATEGIES.keys()
    builtin = [{"name": n, **STRATEGIES.meta(n), "source": "builtin"} for n in names]
    return builtin + [{**s, "source": "user"} for s in list_strategies()]


@router.post("")
def create_strategy(req: StrategyIn) -> dict:
    from lquant.backtest.strategy_store import save_strategy

    try:
        return save_strategy(req.name, req.source, description=req.description,
                             config=req.config, benchmark=req.benchmark)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/validate")
def validate_strategy(req: ValidateIn) -> dict:
    from lquant.backtest.validation import validate_source

    return {"errors": validate_source(req.source)}


@router.get("/{sid}")
def get_strategy_api(sid: str) -> dict:
    try:
        return _get_live(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e


@router.put("/{sid}")
def update_strategy(sid: str, req: StrategySourceIn) -> dict:
    """PUT = 以任意历史版本 id 定位策略，再以同名保存一版（版本 +1）。"""
    from lquant.backtest.strategy_store import get_strategy, save_strategy

    try:
        cur = get_strategy(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e
    try:
        return save_strategy(cur["name"], req.source, description=req.description,
                             config=req.config, benchmark=req.benchmark)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/{sid}/versions")
def strategy_versions(sid: str) -> list[dict]:
    from lquant.backtest.strategy_store import get_strategy, list_versions

    try:
        cur = get_strategy(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e
    return list_versions(cur["name"])


@router.delete("/{sid}")
def delete_strategy_api(sid: str) -> dict:
    """软删整个策略（该 name 下所有版本）。"""
    from lquant.backtest.strategy_store import delete_strategy, get_strategy, list_versions

    try:
        cur = get_strategy(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e
    for v in list_versions(cur["name"]):
        delete_strategy(v["id"])
    return {"deleted": sid}
