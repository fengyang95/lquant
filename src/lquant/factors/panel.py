"""因子列统一计算：内置 qlib 因子名 → qlib_alpha.compute；其余 → 因子 DSL。

与 GET /api/factors/builtin 同一套公式体系；策略回测（JQRunner）与因子页共用。
"""

from __future__ import annotations

import polars as pl

from lquant.factors.engine import FactorEngine
from lquant.factors.qlib_alpha import compute as _qlib_compute
from lquant.factors.qlib_alpha import resolve_name


def compute_factor_columns(
    df: pl.DataFrame, formulas: list[str]
) -> tuple[pl.DataFrame, dict[str, str]]:
    """按公式算因子列。返回 (新 df, {formula: 列名})；非法公式 raise ValueError。

    内置因子名（如 ROC5/MA20/KMID）走 qlib_alpha.compute（重命名 _factor 列）；
    其余按因子 DSL（FactorEngine）。两条路任一失败都转成 ValueError。
    """
    out = df.sort(["symbol", "trade_date"])
    colmap: dict[str, str] = {}
    for f in formulas:
        col = f"_f_{f}"
        try:
            resolve_name(f)  # 内置名合法才走 qlib_alpha
        except (KeyError, ValueError):
            out = _dsl_compute(out, f, col)
            colmap[f] = col
            continue
        try:
            out = _qlib_compute(out, f).rename({"_factor": col})
            colmap[f] = col
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"因子公式 {f} 计算失败: {e}") from e
    return out, colmap


def _dsl_compute(df: pl.DataFrame, expr: str, col: str) -> pl.DataFrame:
    """DSL 路径：解析/静态检查失败、运行期缺列等一律转 ValueError。"""
    try:
        return FactorEngine(df.lazy()).compute(expr, col)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"非法因子公式 {expr!r}: {e}") from e
