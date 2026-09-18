"""ast_nodes 数据类单元测试。"""

from __future__ import annotations

from lquant.factors.dsl.ast_nodes import (
    BinaryOp,
    Call,
    FactorExpr,
    Field,
    Node,
    Num,
    UnaryOp,
)


def test_field_and_num():
    f = Field(name="close")
    n = Num(value=3.0)
    assert isinstance(f, Node) and isinstance(n, Node)
    assert f.name == "close"
    assert n.value == 3.0


def test_unary_and_binary_ops():
    u = UnaryOp(op="-", arg=Field(name="close"))
    b = BinaryOp(op="+", left=Field(name="open"), right=Num(value=1.0))
    assert u.op == "-" and isinstance(u.arg, Node)
    assert b.op == "+" and b.right.value == 1.0


def test_call_defaults():
    c = Call(name="Ts_Mean", args=[Field(name="close"), Num(value=5)])
    assert c.min_window == 0
    assert c.category == "EL"
    # 显式覆盖
    c2 = Call(name="Rank", args=[], min_window=3, category="CS")
    assert c2.min_window == 3 and c2.category == "CS"


def test_factor_expr_defaults():
    fe = FactorExpr(name="f1", root=Field(name="close"), expr="$close")
    assert fe.min_window == 0
    assert fe.fields == set()
    fe2 = FactorExpr(
        name="f2",
        root=Num(value=1),
        expr="1",
        min_window=2,
        fields={"close"},
    )
    assert fe2.min_window == 2 and fe2.fields == {"close"}


def test_dataclass_equality():
    assert Field(name="close") == Field(name="close")
    assert Field(name="close") != Field(name="open")
    assert Call(name="Ts_Mean") != Call(name="Rank")
