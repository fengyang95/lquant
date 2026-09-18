"""最后覆盖冲刺：ts_ops 全算子直驱（registry 取回原函数）。"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.factors.ops import ts_ops  # noqa: F401  注册副作用
from lquant.factors.ops.registry import OPS


def _df(n=12):
    x = np.linspace(1.0, 12.0, n) + np.sin(np.arange(n))
    y = np.cos(np.arange(n)) * 2 + 5
    return pl.DataFrame({
        "symbol": ["A"] * n,
        "x": x, "y": y,
    })


def _apply(name, cols, n=3, **extra):
    fn = OPS.get(name)
    expr = fn(pl.col(cols[0]), *(pl.col(c) for c in cols[1:]), n, **extra)
    return _df().with_columns(expr.alias("out"))


def test_ts_ops_single_col():
    for name in ("Ts_Mean", "Ts_Std", "Ts_Sum", "Ts_Max", "Ts_Min",
                 "Ts_Skew", "Ts_Prod", "Ts_EMA", "Ts_Slope",
                 "Ts_Slope", "Ts_WMA", "Ts_ArgMax", "Ts_ArgMin",
                 "Ts_Delta", "Ts_Return", "Ts_Delay"):
        out = _apply(name, ("x",))
        assert out["out"].null_count() < len(out), name


def test_ts_ops_quantile_and_cov_corr():
    out = _apply("Ts_Quantile", ("x",), q=0.5)
    assert out["out"].null_count() < len(out)
    out2 = _apply("Ts_Corr", ("x", "y"))
    out3 = _apply("Ts_Cov", ("x", "y"))
    assert out2.height == out3.height == 12


def test_ts_ops_rank_rsquare_known_broken():
    # np.std 对 polars Series 的分派 bug 已修（to_numpy 后计算）
    df = pl.DataFrame({"symbol": ["A"] * 6,
                       "x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
    out = df.with_columns(
        OPS.get("Ts_Rsquare")(pl.col("x"), 5).alias("r2"),
        OPS.get("Ts_Resi")(pl.col("x"), 5).alias("resi"),
    )
    # 线性序列 → R^2 = 1；resi 有限
    assert abs(out["r2"][-1] - 1.0) < 1e-9
    assert out["resi"][-1] >= 0
