"""lq factor：定义 / 计算 / 评价。"""
from __future__ import annotations

import click


@click.group()
def factor() -> None:
    """因子"""


@factor.command()
@click.argument("expr")
@click.option("--name", default=None)
def add(expr: str, name: str | None) -> None:
    """注册因子表达式（会做 AST 静态检查）。"""
    from lquant.factors.dsl.analyzer import check
    from lquant.factors.dsl.parser import parse

    ast = parse(expr, name or "tmp")
    check(ast)
    click.echo(f"OK {ast.name} min_window={ast.min_window} fields={sorted(ast.fields)}")


@factor.command()
@click.option("--name", required=True)
def run(name: str) -> None:
    click.echo(f"compute {name}")
