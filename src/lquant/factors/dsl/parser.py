"""递归下降解析：表达式 → AST。"""
from __future__ import annotations

from lquant.core.errors import DSLParseError
from lquant.factors.dsl.ast_nodes import BinaryOp, Call, FactorExpr, Field, Node, Num, UnaryOp
from lquant.factors.dsl.lexer import tokenize


class Parser:
    def __init__(self, src: str) -> None:
        self.tokens = tokenize(src)
        self.pos = 0
        self.src = src

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise DSLParseError("表达式意外结束")
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, text: str) -> None:
        kind, val = self.next()
        if val != text:
            raise DSLParseError(f"期望 {text!r}，得到 {val!r}")

    # expr := term (('+'|'-') term)*
    def parse_expr(self) -> Node:
        node = self.parse_term()
        while (t := self.peek()) and t[1] in ("+", "-"):
            self.next()
            node = BinaryOp(t[1], node, self.parse_term())
        return node

    def parse_term(self) -> Node:
        node = self.parse_unary()
        while (t := self.peek()) and t[1] in ("*", "/"):
            self.next()
            node = BinaryOp(t[1], node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        if (t := self.peek()) and t[1] == "-":
            self.next()
            return UnaryOp("-", self.parse_unary())
        return self.parse_atom()

    def parse_atom(self) -> Node:
        kind, val = self.next()
        if kind == "field":
            return Field(val[1:])
        if kind == "number":
            return Num(float(val))
        if val == "(":
            node = self.parse_expr()
            self.expect(")")
            return node
        if kind == "name":
            if self.peek() and self.peek()[1] == "(":
                self.next()
                args: list[Node] = []
                if self.peek() and self.peek()[1] != ")":
                    args.append(self.parse_expr())
                    while self.peek() and self.peek()[1] == ",":
                        self.next()
                        args.append(self.parse_expr())
                self.expect(")")
                return Call(val, args)
            return Field(val)
        raise DSLParseError(f"意外的 token: {val!r}")


def parse(expr: str, name: str = "unnamed") -> FactorExpr:
    p = Parser(expr)
    root = p.parse_expr()
    if p.pos != len(p.tokens):
        raise DSLParseError(f"表达式尾部有多余内容: {p.tokens[p.pos:]}")
    return FactorExpr(name=name, root=root, expr=expr)
