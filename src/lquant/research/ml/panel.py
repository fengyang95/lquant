"""特征面板构建：把「特征名列表」解析成日线长表上的真实列。

ML 训练需要一个宽表（每行 = 某标的某日，含特征列 + 前瞻收益）。
lquant 里有三种特征来源，本模块把它们统一成一个入口，避免 API/CLI/脚本
各写一套解析逻辑（口径漂移的经典来源）：

1. **Alpha158 内置因子**（``factors/qlib_alpha.py``，如 ``MA20`` / ``KMID``）；
2. **简单公式**（``pct_change_N`` / ``rolling_std_N``，与回测 API 同口径）；
3. **湖里已有的列**（如 ``close`` / ``float_mv`` / ``turnover_rate``）。

解析顺序即优先级：已是湖列 → 直接取；否则按内置因子 → 简单公式 → 报错。
**报错时列出可选项**，绝不静默跳过某个特征 —— 少一个特征训练出来的模型
看起来一样能跑，但语义已经变了。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import MLError

__all__ = ["resolve_feature", "build_feature_panel", "available_features",
           "FEATURE_SOURCES"]

#: 特征来源标签（自省/前端展示用）
FEATURE_SOURCES = ("column", "alpha158", "formula")

_SIMPLE_FORMULAS = ("pct_change_", "rolling_std_")


def _formula_expr(name: str) -> pl.Expr | None:
    """简单公式 → 表达式；不认识返回 None。

    与 ``server/api/backtests.py::_compute_factor`` 同一口径（那里是回测，
    这里是 ML）—— 两处若漂移，同一个 ``pct_change_20`` 在回测与训练里
    会变成两个不同的东西。
    """
    for prefix, fn in (("pct_change_", lambda n: pl.col("close").pct_change(n)),
                       ("rolling_std_", lambda n: pl.col("close").pct_change()
                        .rolling_std(n))):
        if name.startswith(prefix):
            try:
                n = int(name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                return None
            if n <= 0:
                return None
            return fn(n).over("symbol")
    return None


def resolve_feature(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """把单个特征名解析成 df 上的一个同名列。"""
    if name in df.columns:
        return df
    from lquant.factors.qlib_alpha import compute as alpha_compute
    from lquant.factors.qlib_alpha import has_factor

    if has_factor(name):
        out = alpha_compute(df, name.upper())
        return out.with_columns(pl.col("_factor").alias(name)).drop("_factor")
    expr = _formula_expr(name)
    if expr is not None:
        return df.with_columns(expr.alias(name))
    raise MLError(
        f"无法解析特征 {name!r}：既不是日线湖的列，也不是 Alpha158 内置因子，"
        f"也不是简单公式（{_SIMPLE_FORMULAS}）。"
        f"可用内置因子见 GET /api/factors/builtin；湖列示例: "
        f"{[c for c in ('close', 'volume', 'amount', 'turnover_rate', 'float_mv') if c in df.columns]}")


def build_feature_panel(df: pl.DataFrame, features: list[str]) -> pl.DataFrame:
    """按顺序解析所有特征，返回带这些列的面板。

    ``features`` 为空 → 报错（训练一个没有特征的模型没有意义，
    而"跑通了但什么都没学"是最难发现的失败）。
    """
    if not features:
        raise MLError("features 不能为空（没有特征的训练等于随机数生成器）")
    out = df
    for f in features:
        out = resolve_feature(out, f)
    return out


def available_features(df: pl.DataFrame | None = None, *,
                       limit: int = 200) -> dict:
    """可用特征清单：湖列 + Alpha158 内置 + 简单公式模板。

    供前端/CLI 做选择器，避免用户靠猜名字试错。
    """
    from lquant.factors.qlib_alpha import list_builtin

    cols: list[str] = []
    if df is not None:
        cols = [c for c in df.columns
                if c not in ("trade_date", "symbol", "sec_type", "source",
                             "ingested_at", "data_version", "quality_flags")]
    builtin = [b["name"] for b in list_builtin()]
    return {
        "columns": sorted(cols)[:limit],
        "alpha158": builtin,
        "alpha158_count": len(builtin),
        "formulas": [f"pct_change_{n}" for n in (5, 10, 20, 60)]
        + [f"rolling_std_{n}" for n in (5, 10, 20, 60)],
    }
