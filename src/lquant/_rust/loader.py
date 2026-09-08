"""Rust 扩展加载 + 自动降级。

ABI 强绑定：pyo3 / pyo3-polars / polars 三者版本错配要到运行时才炸，
所以 CI 必须跑 `python -c "import lq_ops"` 冒烟。
"""
from __future__ import annotations

import importlib

_MODULES = ("lq_ops", "lq_backtest", "lq_metrics")


def status() -> dict[str, str]:
    out = {}
    for m in _MODULES:
        try:
            importlib.import_module(m)
            out[m] = "rust"
        except ImportError:
            out[m] = "python-fallback"
    return out


def has_rust() -> bool:
    return all(v == "rust" for v in status().values())


def print_status() -> None:
    import polars as pl

    print(f"polars {pl.__version__}")
    for k, v in status().items():
        print(f"  {k:<12} {v}")
