"""Rust 算子桥接。

加载失败自动降级到 Python 参考实现 —— 这条不是洁癖，
是一个人扛不住编译失败全线停摆的现实。
"""
from __future__ import annotations

from lquant.core.logging import setup_logging


def rust_available() -> bool:
    try:
        import lq_ops  # noqa: F401

        return True
    except ImportError:
        return False


def register_rust_ops() -> int:
    """若 Rust 扩展可用，用 Rust 实现覆盖同名算子（更快）。"""
    if not rust_available():
        return 0
    from lquant.factors.ops.registry import OPS

    n = 0
    try:
        import lq_ops

        for name in ("Ts_Corr", "Ts_Regbeta", "Cs_Neutralize"):
            if hasattr(lq_ops, name.lower()) and name in OPS:
                OPS._items[name] = getattr(lq_ops, name.lower())
                n += 1
    except Exception:  # noqa: BLE001
        setup_logging()
    return n
