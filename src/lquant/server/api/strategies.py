"""策略枚举 —— 前端回测表单的下拉数据源，也是自定义策略的暴露口。"""
from __future__ import annotations

from fastapi import APIRouter

from lquant.backtest.strategy import STRATEGIES

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("")
def list_strategies() -> list[dict]:
    """已注册策略（内置 + 用户通过 register_strategy 注册的）。"""
    _import_order = STRATEGIES.keys()
    return [{"name": n, **STRATEGIES.meta(n)} for n in _import_order]
