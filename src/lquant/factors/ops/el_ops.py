"""逐元素算子（EL）：逐行计算，无窗口、无截面语义。"""
from __future__ import annotations

import polars as pl

from lquant.factors.ops.bool_ops import truth
from lquant.factors.ops.registry import op


@op("Abs", "EL", 0, "绝对值")
def el_abs(x: pl.Expr) -> pl.Expr:
    return x.abs()


@op("Log", "EL", 1, "自然对数 ln(x)（qlib 口径 Log($v)=ln(v)；非正值置 null。ln(1+x) 请用 Log1p）")
def el_log(x: pl.Expr) -> pl.Expr:
    # qlib 的 Log 是普通 ln（qlib_source _PASSTHROUGH 一致）。此前误用 log1p，
    # Alpha158 的 Log($volume+1) 经两层叠加会算成 ln(v+2)——量纲系统性偏移
    return pl.when(x > 0).then(x.log()).otherwise(None)


@op("Log1p", "EL", 1, "ln(1+x)（收益率/比率类安全取对数；等价手写 Log(x+1) 的数值）")
def el_log1p(x: pl.Expr) -> pl.Expr:
    return x.log1p()


@op("Sign", "EL", 0, "符号函数")
def el_sign(x: pl.Expr) -> pl.Expr:
    return x.sign()


@op("Sqrt", "EL", 0, "平方根（负值置 null）")
def el_sqrt(x: pl.Expr) -> pl.Expr:
    return pl.when(x >= 0).then(x.sqrt()).otherwise(None)


@op("Power", "EL", 1, "幂运算 Power(x, p)")
def el_power(x: pl.Expr, p: pl.Expr) -> pl.Expr:
    return x.pow(p)


@op("Greater", "EL", 0, "逐元素取大")
def el_greater(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.max_horizontal(a, b)


@op("Less", "EL", 0, "逐元素取小")
def el_less(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.min_horizontal(a, b)


@op("SignedPower", "EL", 0, "带符号幂 sign(x)*|x|^p")
def el_signed_power(x: pl.Expr, p: pl.Expr) -> pl.Expr:
    return x.sign() * x.abs().pow(p)


@op("If", "EL", 0, "逐元素条件 If(cond, a, b)（真值判据 cond>0，与 Not/And/Or 同源）")
def el_if(c: pl.Expr, a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when(truth(c)).then(a).otherwise(b)
