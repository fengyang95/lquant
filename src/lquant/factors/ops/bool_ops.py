"""逻辑与比较算子：把「真值」显式定义成 0/1 浮点，而不是布尔列。

**为什么单独一个模块**：这几个算子的口径容易想当然，集中在一处定义便于对齐。

三条口径约定（与 qlib 的差异都写在这里）：

1. **真值 = ``x > 0``，输出 0.0/1.0 浮点**，不是布尔列。下游 TS 算子
   （``Ts_Mean``/``Ts_Sum``）要吃数值，布尔列进 ``rolling_mean`` 直接报错；
   DSL 里的中缀 ``>``/``<`` 也是 cast 成 Float64 的，这里必须一致。
2. **``Not``/``And``/``Or`` 用逻辑语义，不用 qlib 的按位语义**。qlib 直接
   ``np.bitwise_not`` / ``np.bitwise_and``，作用在 float64 上是**按位取反**：
   ``~1.0 == -2.0``、``~0.0 == -1.0``。它内部靠「输入只可能是 0/1」的隐式
   约定活着，一旦喂进 ``Ts_Rank`` 这类 0~1 连续值就产出无意义的负数。
   这边定义 ``Not(x) = 1 if x <= 0 else 0``（真值取 ``> 0``，与
   ``el_ops.If`` 的 ``c > 0`` 判据同源），语义可读、不会因输入范围失效。
3. **null 传播**：比较遇 null 得 null；``And``/``Or`` 走 Polars 的 Kleene
   逻辑（与 SQL 一致：``null & False = False``），不把 null 当 False。

不移植的 qlib 算子（``Mask`` / ``ChangeInstrument``）：
两者的语义都是「**换一个标的**再算这个表达式」（例如算个股对指数的 beta），
依赖 qlib 表达式树里「当前 instrument」这个隐藏上下文。lquant 的表达式作用在
已展开的 symbol × date 长表上，没有「当前标的」这一概念，硬套会得到每个
symbol 都读到同一条指数序列的静默错值。真要算相对指数的东西，正确做法是把
指数列 join 进面板再做 ``Ts_Corr``/``Ts_Return``（见 ``backtest/benchmark.py``），
而不是引入一个换标的算子。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.ops.registry import op


def truth(x: pl.Expr) -> pl.Expr:
    """真值判据：``x > 0``。全库唯一的真值定义，``If``/``Not``/``And``/``Or`` 共用。"""
    return x > 0


def _bit(expr: pl.Expr) -> pl.Expr:
    """布尔表达式 → 0.0/1.0 浮点（下游 TS 算子的输入口径）。"""
    return expr.cast(pl.Float64)


@op("Not", "EL", 0, "逻辑非（真值判据 x>0，输出 0/1）")
def bool_not(x: pl.Expr) -> pl.Expr:
    # `~(x > 0)` 而不是 `x <= 0`：前者保持 null 为 null，后者对 null 也会给 True
    return _bit(~truth(x))


@op("And", "EL", 0, "逻辑与（真值判据 x>0，输出 0/1）")
def bool_and(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(truth(a) & truth(b))


@op("Or", "EL", 0, "逻辑或（真值判据 x>0，输出 0/1）")
def bool_or(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(truth(a) | truth(b))


@op("Eq", "EL", 0, "逐元素相等（输出 0/1；浮点相等，慎用于连续值）")
def bool_eq(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(a == b)


@op("Ne", "EL", 0, "逐元素不等（输出 0/1）")
def bool_ne(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(a != b)


@op("Ge", "EL", 0, "逐元素大于等于（输出 0/1）")
def bool_ge(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(a >= b)


@op("Le", "EL", 0, "逐元素小于等于（输出 0/1）")
def bool_le(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return _bit(a <= b)
