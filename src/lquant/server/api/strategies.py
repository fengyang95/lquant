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
    """PUT 用 body（name 沿用现有策略，不可改名）。

    description/config/benchmark 用 None 哨兵：省略时沿用现有值，不静默清空。
    """
    source: str = Field(min_length=10, max_length=200_000)
    description: str | None = None
    config: dict | None = None
    benchmark: str | None = None


class ValidateIn(BaseModel):
    source: str = Field(min_length=1, max_length=200_000)


def _get_live(sid: str) -> dict:
    """get_strategy 不过滤软删：先按任意历史版本 id 解析，再校验该策略（按 name）
    仍有存活版本 —— 软删后的 id 不复活，历史版本 id 仍可 PUT。"""
    from lquant.backtest.strategy_store import get_strategy, list_strategies

    cur = get_strategy(sid)
    if not any(s["name"] == cur["name"] for s in list_strategies()):
        raise KeyError(sid)
    return cur


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
    from lquant.backtest.strategy_store import save_strategy

    try:
        cur = _get_live(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e
    try:
        sent = req.model_fields_set          # 区分「省略」与「显式 null」
        return save_strategy(
            cur["name"], req.source,
            description=req.description if "description" in sent
            else cur["description"],
            config=req.config if "config" in sent else cur["config"],
            benchmark=req.benchmark if "benchmark" in sent else cur["benchmark"])
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
    from lquant.backtest.strategy_store import delete_strategy, list_versions

    try:
        cur = _get_live(sid)
    except KeyError as e:
        raise HTTPException(404, f"策略不存在: {sid}") from e
    for v in list_versions(cur["name"]):
        delete_strategy(v["id"])
    return {"deleted": sid}
