"""AST → JSON 可序列化字典。

供前端因子编辑画布把已有因子的 DSL 表达式反解析成 DAG：前端不重写
词法/语法分析器（两份解析器必然漂移），只消费这里产出的树做形状映射。

形状约定（前端 `factor-canvas/decompile.ts` 依赖）：
    {"kind": "field",  "name": "close"}
    {"kind": "number", "value": 5.0}
    {"kind": "unary",  "op": "-",      "arg": <node>}
    {"kind": "binary", "op": "/",      "left": <node>, "right": <node>}
    {"kind": "call",   "name": "Ts_Mean", "args": [<node>, ...]}
"""
from __future__ import annotations

from typing import Any

from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Node, Num, UnaryOp


def to_dict(node: Node) -> dict[str, Any]:
    """AST 节点 → JSON 友好的 dict。未知节点抛 TypeError（不静默吞）。"""
    if isinstance(node, Field):
        return {"kind": "field", "name": node.name}
    if isinstance(node, Num):
        return {"kind": "number", "value": float(node.value)}
    if isinstance(node, UnaryOp):
        return {"kind": "unary", "op": node.op, "arg": to_dict(node.arg)}
    if isinstance(node, BinaryOp):
        return {
            "kind": "binary",
            "op": node.op,
            "left": to_dict(node.left),
            "right": to_dict(node.right),
        }
    if isinstance(node, Call):
        return {
            "kind": "call",
            "name": node.name,
            "args": [to_dict(a) for a in node.args],
        }
    raise TypeError(f"未知 AST 节点: {type(node).__name__}")
