"""DSL printer: unparse AST to string + canonical form for dedup.

- unparse(node) -> str: round-trip (parse(unparse(a)) keeps semantics)
- canonical(node) -> str: normalized form (commutative args sorted,
  UnaryOp(-,Num) folded, numbers normalized) -- the basis of
  cross-source dedup (M2) and GP/LLM mining dedup (M3).
"""
from __future__ import annotations

from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp

_PREC = {">": 0, "<": 0, "+": 1, "-": 1, "*": 2, "/": 2}
_COMMUTATIVE = {"+", "*"}


def _fmt_num(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return repr(float(v))


def unparse(node: Node) -> str:
    """AST -> expression string（可被 parse 复原）。"""
    return _u(node, 0)


def _u(node: Node, parent: int) -> str:
    if isinstance(node, Field):
        return f"${node.name}"
    if isinstance(node, Num):
        return _fmt_num(node.value)
    if isinstance(node, UnaryOp):
        return "-" + _u(node.arg, 2)
    if isinstance(node, BinaryOp):
        p = _PREC[node.op]
        left = _u(node.left, p)
        right = _u(node.right, p + 1)
        if p < parent:
            return f"({left}{node.op}{right})"
        return f"{left}{node.op}{right}"
    if isinstance(node, Call):
        return f"{node.name}(" + ",".join(_u(a, 0) for a in node.args) + ")"
    raise TypeError(f"unknown node {type(node)}")


def canonical(node: Node) -> str:
    """归一化表达式串：交换律排序 + 数字归一。"""
    return _c(node, 0)


def _c(node: Node, parent: int) -> str:
    if isinstance(node, Field):
        return f"${node.name.lower()}"
    if isinstance(node, Num):
        return _fmt_num(node.value)
    if isinstance(node, UnaryOp):
        return "-" + _c(node.arg, 2)
    if isinstance(node, BinaryOp):
        op = node.op
        l = _c(node.left, 0)
        r = _c(node.right, 0)
        if op in _COMMUTATIVE:
            l, r = sorted([l, r])
        body = f"{l}{op}{r}"
        return f"({body})" if _PREC[op] < parent else body
    if isinstance(node, Call):
        return f"{node.name}(" + ",".join(_c(a, 0) for a in node.args) + ")"
    raise TypeError(f"unknown node {type(node)}")


def canonical_id(expr: str) -> str:
    """lquant DSL 串 -> canonical 串 sha1 前 16 位（入库 factor_id 统一口径）。"""
    import hashlib

    ast = parse(expr, "cid")
    c = canonical(ast.root)
    return hashlib.sha1(c.encode("utf-8")).hexdigest()[:16]

from lquant.factors.dsl.parser import parse  # noqa: E402  (循环导入规避)
