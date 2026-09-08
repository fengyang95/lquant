"""AST 节点。

因子定义为字符串带来四个收益：
1. DAG 调度与两级缓存的 key
2. LLM 可直接生成（RD-Agent / QuantaAlpha 那类项目的本质）
3. 可静态遍历禁止未来算子 —— 黑盒 Python 函数做不到
4. 前端编辑器可自省
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    pass


@dataclass
class Field(Node):
    name: str          # close / open / volume / amount / float_mv ...


@dataclass
class Num(Node):
    value: float


@dataclass
class UnaryOp(Node):
    op: str
    arg: Node


@dataclass
class BinaryOp(Node):
    op: str
    left: Node
    right: Node


@dataclass
class Call(Node):
    """算子调用，如 Ts_Mean($close, 5)。"""

    name: str
    args: list[Node] = field(default_factory=list)
    # 由 analyzer 填充
    min_window: int = 0
    category: str = "EL"     # TS(时序) / CS(截面) / EL(逐元素)


@dataclass
class FactorExpr:
    name: str
    root: Node
    expr: str
    min_window: int = 0
    fields: set[str] = field(default_factory=set)
