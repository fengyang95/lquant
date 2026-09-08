"""算子注册表。

元数据必须包含 category(TS/CS/EL) 与 min_window：
- category 决定是否需要分步物化（Polars 嵌套 over 的坑）
- min_window 是 AST 未来函数检测与预热期计算的依据
"""
from __future__ import annotations

from lquant.core.registry import Registry

OPS: Registry = Registry("factor_ops")


def op(name: str, category: str = "EL", min_window: int = 0, label: str = ""):
    return OPS.register(
        name,
        {"category": category, "min_window": min_window, "label": label or name},
    )
