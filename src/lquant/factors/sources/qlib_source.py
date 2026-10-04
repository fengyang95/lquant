"""Qlib Alpha158 adapter: qlib formula -> lquant DSL translator."""

from __future__ import annotations

import hashlib

from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp
from lquant.factors.dsl.parser import parse
from lquant.factors.dsl.printer import canonical, unparse

_OP_MAP = {
    "Mean": "Ts_Mean", "Sum": "Ts_Sum", "Std": "Ts_Std", "Corr": "Ts_Corr",
    "Ref": "Ts_Delay", "Max": "Ts_Max", "Min": "Ts_Min", "Rank": "Ts_Rank",
    "Quantile": "Ts_Quantile", "IdxMax": "Ts_ArgMax", "IdxMin": "Ts_ArgMin",
    "Slope": "Ts_Slope", "Rsquare": "Ts_Rsquare", "Resi": "Ts_Resi", "WMA": "Ts_WMA",
}

_PASSTHROUGH = ("Greater", "Less", "Abs", "Log", "Sign", "Power", "SignedPower", "If")

_LOOKUP = {k.lower(): v for k, v in _OP_MAP.items()}
_LOOKUP.update({k.lower(): k for k in _PASSTHROUGH})


def _translate_call(node: Call) -> Call:
    """Qlib 算子名 → lquant DSL 算子名。

    **不要按参数个数区分 `Max`/`Min` 与 `Greater`/`Less`。** Qlib 里
    `Max($high, 20)` 是**滚动** 20 期最高价，`Greater($open, $close)` 才是
    逐元素取大 —— 两者参数个数都是 2。曾有一版按 `len(args) == 2` 把
    `Max`/`Min` 改写成 `Greater`/`Less`，于是 25 处滚动算子被静默译成
    「与常数 20 逐元素取大」（`max(最高价, 常数20)` = 最高价本身），
    MAX/MIN/RSV 整族因子退化成错误值。一律走 _OP_MAP。
    """
    name = node.name
    low = name.lower()
    if low == "quantile" and len(node.args) == 3:
        return Call("Ts_Quantile", node.args)
    key = _LOOKUP.get(low)
    if key:
        return Call(key, node.args)
    raise ValueError(f"不可翻译的 qlib 算子: {name}")


def _rename_ops(node: Node) -> Node:
    if isinstance(node, (Field, Num)):
        return node
    if isinstance(node, UnaryOp):
        return UnaryOp(node.op, _rename_ops(node.arg))
    if isinstance(node, BinaryOp):
        return BinaryOp(node.op, _rename_ops(node.left), _rename_ops(node.right))
    if isinstance(node, Call):
        return _translate_call(Call(node.name, [_rename_ops(a) for a in node.args]))
    raise TypeError(type(node))


def _preprocess(src: str) -> str:
    """$vwap 代理替换（qlib 的 $vwap = amount/volume 口径）+ 注释剥离。"""
    s = src.split("#", maxsplit=1)[0].strip()
    s = s.replace("$vwap", "($amount/$volume)")
    return s


def translate(formula: str) -> str:
    ast = parse(_preprocess(formula), "qlib_src")
    out = unparse(_rename_ops(ast.root))
    parse(out, "verify")
    return out


def factor_id(formula: str) -> str:
    ast = parse(formula, "fid")
    c = canonical(_rename_ops(ast.root))
    return hashlib.sha1(c.encode("utf-8")).hexdigest()[:16]
