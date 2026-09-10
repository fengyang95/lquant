"""因子来源接入层：Adapter 只做翻译，绝不做执行（方案第二节硬规则）。

每个来源 adapter 把外部公式翻译成 lquant DSL，统一过 analyzer/compiler，
单一执行语义 —— IC 对不上无从排查的问题从根上不存在。
"""
from __future__ import annotations

from lquant.core.registry import Registry

SOURCES: Registry = Registry("factor_sources")


def register_source(name: str, label: str):
    return SOURCES.register(name, {"label": label})


def list_sources() -> list[dict]:
    return [{"name": k, **SOURCES.meta(k)} for k in SOURCES]
