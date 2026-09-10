"""AST → Polars LazyExpr。

关键坑（Polars issue #25691）：嵌套 .over() 被当作层级分区，
而因子的时序与截面是正交的。TS 与 CS 算子嵌套时必须先物化 ——
症状是不报错、结果错，最难排查。
所以执行器一开始就要有 plan() 拆步骤。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp
from lquant.factors.ops.registry import OPS


def _category(node: Node) -> str:
    return OPS.meta(node.name).get("category", "EL") if isinstance(node, Call) else "EL"


def _has_kind(node: Node, kind: str) -> bool:
    """子树中是否含某类窗口算子（跨过四则运算包装也要查到）。"""
    if isinstance(node, Call):
        if _category(node) == kind:
            return True
        return any(_has_kind(a, kind) for a in node.args)
    if isinstance(node, UnaryOp):
        return _has_kind(node.arg, kind)
    if isinstance(node, BinaryOp):
        return _has_kind(node.left, kind) or _has_kind(node.right, kind)
    return False


def has_nested_ts_cs(node: Node) -> bool:
    """检测 TS/CS 嵌套 —— 有则需要分步物化。

    判据：Call 的参数子树内含异类窗口算子（TS/CS 任意深度混排）。
    原判据只看参数是否直接是异类 Call，四则运算包裹的混合子树漏检 →
    单表达式双 over 静默产出全 null（Polars #25691 坑）。
    """
    if isinstance(node, Call):
        cat = _category(node)
        for a in node.args:
            other = "CS" if cat == "TS" else "TS"
            if _has_kind(a, other):
                return True
            if has_nested_ts_cs(a):
                return True
    elif isinstance(node, (UnaryOp, BinaryOp)):
        kids = [node.arg] if isinstance(node, UnaryOp) else [node.left, node.right]
        return any(has_nested_ts_cs(k) for k in kids)
    return False


def plan(node: Node) -> list[Node]:
    """把 TS/CS 嵌套拆成有序步骤，每步物化后再进入下一步。

    通用后序拆步：Call 的任意参数（含四则运算包裹的 TS/CS 混合子树）
    只要含 TS/CS 嵌套，就整体物化为一步、原位替换成 __step{i} 引用；
    根节点永远作为最后一步。
    """
    if not has_nested_ts_cs(node):
        return [node]
    steps: list[Node] = []
    root = _split(node, steps)
    steps.append(root)
    return steps


def _split(node: Node, steps: list[Node]) -> Node:
    """后序拆步：先把参数子树拆净，再在当前节点处把含异类算子的参数整体物化。"""
    if isinstance(node, BinaryOp):
        return BinaryOp(node.op, _split(node.left, steps), _split(node.right, steps))
    if isinstance(node, UnaryOp):
        return UnaryOp(node.op, _split(node.arg, steps))
    if isinstance(node, Call):
        cat = _category(node)
        other = "CS" if cat == "TS" else "TS"
        rebuilt = []
        for a in node.args:
            rebuilt.append(_split(a, steps))
        node = Call(node.name, rebuilt)
        for i, a in enumerate(node.args):
            if _has_kind(a, other) or has_nested_ts_cs(a):
                steps.append(a)
                node.args[i] = Field(f"__step{len(steps)}")
        return node
    return node
def _int_window(v: float) -> int | float:
    """整值数字 → int（rolling 窗口要 int）；真小数保持原样。"""
    return int(v) if float(v).is_integer() else v


def compile_expr(node: Node) -> pl.Expr:
    if isinstance(node, Field):
        return pl.col(node.name)
    if isinstance(node, Num):
        return pl.lit(node.value)
    if isinstance(node, UnaryOp):
        inner = compile_expr(node.arg)
        return -inner if node.op == "-" else inner
    if isinstance(node, BinaryOp):
        l, r = compile_expr(node.left), compile_expr(node.right)
        return {
            "+": l + r, "-": l - r, "*": l * r, "/": l / r,
            ">": (l > r).cast(pl.Float64), "<": (l < r).cast(pl.Float64),
        }[node.op]
    if isinstance(node, Call):
        fn = OPS.get(node.name)
        # 数字实参（窗口等）直接传 Python 数，不能编译成 pl.lit ——
        # 否则 Ts_Mean(close,5) 给 rolling_mean 一个 Expr 会崩。
        # 非数字实参（字段/子表达式）才编译成 Expr。整值窗口 5.0 → 5（rolling 要 int）。
        args = [
            _int_window(a.value) if isinstance(a, Num) else compile_expr(a)
            for a in node.args
        ]
        return fn(*args)
    raise FactorError(f"无法编译节点: {type(node)}")
