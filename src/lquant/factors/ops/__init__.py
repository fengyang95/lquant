"""算子库：注册表驱动，新增算子不改上层、前端自动感知。

导入本包即自动注册所有内置算子 —— 避免「忘了 import 导致 OPS 为空」
这类只在运行时才暴露的问题。
"""
from __future__ import annotations

from lquant.factors.ops import cs_ops, ts_ops  # noqa: F401  触发注册
from lquant.factors.ops.registry import OPS, op

try:  # Rust 算子可用时覆盖同名实现，失败则静默降级
    from lquant.factors.ops.rust_bridge import register_rust_ops

    register_rust_ops()
except Exception:  # noqa: BLE001
    pass

__all__ = ["OPS", "op", "ts_ops", "cs_ops"]
