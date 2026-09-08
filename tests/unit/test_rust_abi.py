"""ABI 冒烟：pyo3 / pyo3-polars / polars 版本错配会在运行时才炸，CI 要拦住。"""
from lquant._rust.loader import status


def test_loader_never_raises():
    st = status()
    assert set(st) == {"lq_ops", "lq_backtest", "lq_metrics"}
    assert all(v in ("rust", "python-fallback") for v in st.values())
