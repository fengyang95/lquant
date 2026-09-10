"""预处理方法注册表（对齐 AlphaPurify 的 METHOD_REGISTRY）。

从原始因子直接算 IC，A 股场景下 IC 基本被极值股和行业暴露绑架，是假信号。

注册键是 `{stage}.{name}` 而不是裸 name —— 因为 `none` 在四个 stage 都要有
（「不做处理」本身是个合法选项，用于对照实验），裸名会撞车。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lquant.core.errors import FactorError
from lquant.core.registry import Registry

METHODS: Registry = Registry("preprocess")

STAGES = ("winsorize", "standardize", "neutralize", "orthogonalize")


def method(name: str, stage: str, label: str = "", **meta) -> Callable:
    if stage not in STAGES:
        raise FactorError(f"未知预处理阶段 {stage!r}，可选: {STAGES}")
    return METHODS.register(f"{stage}.{name}",
                            {"name": name, "stage": stage, "label": label or name, **meta})


def get_method(stage: str, name: str):
    """取方法实现。未注册时报错信息列出该 stage 下所有可选方法。"""
    key = f"{stage}.{name}"
    if key not in METHODS:
        avail = [m["name"] for m in METHODS.describe() if m.get("stage") == stage]
        raise FactorError(f"预处理方法 {stage}.{name} 未注册，可选: {avail}")
    return METHODS.get(key)


def list_methods(stage: str | None = None) -> list[dict[str, Any]]:
    return [m for m in METHODS.describe() if stage is None or m.get("stage") == stage]


def default_pipeline() -> list[dict]:
    """A 股默认配方。

    注意中性化的协变量是 `factors`，不是 `by` ——
    `by` 在各方法里统一表示**分组列**（默认截面的 trade_date）。
    """
    return [
        {"op": "winsorize", "method": "mad", "n": 5},
        {"op": "standardize", "method": "zscore"},
        {"op": "neutralize", "method": "ols", "factors": ["market_cap", "industry_sw1"]},
    ]
