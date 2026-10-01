"""未来函数检测：**前缀不变性**。

判据（可执行，不靠人眼）：

    对任意 t，只用 ``df[:t+1]`` 算出的 t 时刻指标值，
    必须与用完整 ``df`` 算出的 t 时刻值**完全一致**。

一旦指标引用了未来数据，前缀不变性必然失败。最典型的反面案例是通达信
**居中对称 XMA**：``XMA(X, N)`` 在 i 处取 ``[i-h, i+h-ε]`` 的均值，
用完整数据算 i 时会看到 i 之后的 h 根 —— 回测里等于预知未来。
正确做法是右对齐截断版本（见 :mod:`lquant.indicators.tiandao`）。

同一份判据既用于单测，也可作为接入新指标时的质量门禁：

    from lquant.indicators.future import assert_no_lookahead

    assert_no_lookahead(add_my_ind, df, out_cols=["my_ind"])
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl

__all__ = ["LookaheadViolation", "check_prefix_invariance", "assert_no_lookahead",
           "DEFAULT_CHECKPOINTS"]


DEFAULT_CHECKPOINTS = 24


@dataclass(frozen=True)
class LookaheadViolation:
    """一处不一致：某行的指标值随「是否多喂了后续数据」而改变。"""

    column: str
    row: int
    key: Any
    full_value: float | None
    prefix_value: float | None

    def __str__(self) -> str:  # pragma: no cover - 仅用于报错展示
        return (f"{self.column} @ row {self.row} ({self.key}): "
                f"全量={self.full_value} 前缀={self.prefix_value}")


def _same(a: Any, b: Any, tol: float) -> bool:
    """空值语义：两边都空算一致；一边空一边不空算不一致。NaN 与 None 等价。"""
    an = a is None or (isinstance(a, float) and math.isnan(a))
    bn = b is None or (isinstance(b, float) and math.isnan(b))
    if an or bn:
        return an == bn
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def _checkpoint_rows(n: int, checkpoints: int | Sequence[int] | None,
                     start: int) -> list[int]:
    """返回待检行号。语义：至多 ``k`` 个等距点 + 强制末行，合计 ≤ k+1 行。"""
    if isinstance(checkpoints, Sequence) and not isinstance(checkpoints, int):
        return sorted({int(i) for i in checkpoints if start <= i < n})
    k = DEFAULT_CHECKPOINTS if checkpoints is None else int(checkpoints)
    if n <= start or k <= 0:
        return []
    span = n - start
    if span <= k:
        return list(range(start, n))
    step = span // k
    rows = list(range(start, n, step))[:k]
    if rows[-1] != n - 1:
        rows.append(n - 1)          # 末行必查：居中 XMA 的漂移正是出现在尾部
    return rows


def check_prefix_invariance(
    compute_fn: Callable[[pl.DataFrame], pl.DataFrame],
    df: pl.DataFrame,
    *,
    out_cols: Sequence[str],
    date_col: str = "trade_date",
    checkpoints: int | Sequence[int] | None = None,
    tol: float = 1e-9,
    start_row: int = 1,
) -> list[LookaheadViolation]:
    """比对「全量计算」与「逐前缀计算」在检查点行上的取值。

    Args:
        compute_fn: 接收 DataFrame、返回带指标列的 DataFrame（行数不变）。
        df: 完整输入（已按时间升序）。
        out_cols: 要比对的指标列。
        date_col: 用于报错定位的列名；不存在则回退成行号。
        checkpoints: 检查行数上限，或显式行号序列。缺省 24 个等距点 + 末行。
        tol: 浮点容差。
        start_row: 从第几行开始检查（首行常无意义）。

    Returns:
        不一致清单；**空列表 = 通过**。
    """
    n = len(df)
    if n == 0:
        return []
    rows = _checkpoint_rows(n, checkpoints, max(start_row, 1))
    if not rows:
        return []

    full = compute_fn(df)
    missing = [c for c in out_cols if c not in full.columns]
    if missing:
        raise KeyError(f"待检列不存在: {missing}；计算结果列为 {sorted(full.columns)}")
    keys = df[date_col].to_list() if date_col in df.columns else list(range(n))

    violations: list[LookaheadViolation] = []
    for i in rows:
        prefix = compute_fn(df.head(i + 1))
        for col in out_cols:
            fv = full[col][i]
            pv = prefix[col][i]
            if not _same(fv, pv, tol):
                violations.append(LookaheadViolation(col, i, keys[i], fv, pv))
    return violations


def assert_no_lookahead(
    compute_fn: Callable[[pl.DataFrame], pl.DataFrame],
    df: pl.DataFrame,
    *,
    out_cols: Sequence[str],
    **kw: Any,
) -> None:
    """断言无未来函数，失败时抛出带定位信息的 AssertionError。"""
    bad = check_prefix_invariance(compute_fn, df, out_cols=out_cols, **kw)
    if bad:
        head = "\n".join(f"  - {v}" for v in bad[:10])
        more = f"\n  ... 另有 {len(bad) - 10} 处" if len(bad) > 10 else ""
        raise AssertionError(
            f"检测到 {len(bad)} 处未来函数（前缀不变性失败）：\n{head}{more}\n"
            "提示：若指标使用居中窗口（如通达信 XMA），请改为右对齐截断版本。"
        )
