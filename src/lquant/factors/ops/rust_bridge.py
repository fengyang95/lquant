"""Rust 算子桥接。

加载失败自动降级到 Python 参考实现 —— 这条不是洁癖，
是一个人扛不住编译失败全线停摆的现实。

注意：lq-ops 当前以 `#[pyfunction]` 暴露 **数组接口**（`Vec<Option<f64>>`，
与 `lquant/_rust/ops_ref.py` 同形、供对拍逐位比较），不是 polars **表达式**
接口。因子引擎的 `OPS` 注册表存的是 polars Expr 构建函数 —— 拿数组函数去
覆盖 `OPS["Ts_Corr"]` 会让因子引擎拿到 list 而非 Expr，属于静默破坏。
所以 register_rust_ops **不再覆盖 OPS**；Rust 算子的价值验证走对拍测试
（tests/unit/test_rust_alignment.py），跑到表达式级覆盖的门槛被
「Rust polars 0.49 与 Python polars 1.44 插架 FFI 版本错配」拦着，延后。
"""
from __future__ import annotations

from lquant.core.logging import setup_logging


def rust_available() -> bool:
    try:
        import lq_ops  # noqa: PLC0415, F401 惰性导入，仅探测可用性

        return True
    except ImportError:
        return False


def register_rust_ops() -> int:
    """检测 Rust 扩展；当前不改写 OPS 表达式注册表（见模块 docstring）。"""
    if not rust_available():
        return 0
    try:
        import lq_ops  # noqa: PLC0415 惰性导入，无 Rust 时不碰包

        _ = lq_ops.__version__
    except Exception:  # noqa: BLE001
        setup_logging()
    # 返回 0：不覆盖 OPS 表达式；Rust 算子正确性由对拍测试背书。
    return 0