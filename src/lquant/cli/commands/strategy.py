"""lq strategy：方言导入与检查。"""
from __future__ import annotations

import click


@click.group()
def strategy() -> None:
    """策略"""


@strategy.command()
@click.argument("path")
@click.option("--dialect", default="joinquant")
def check(path: str, dialect: str) -> None:
    """导入即失败：不支持的 API 列出行号。"""
    from lquant.research.dialect.jq_import import scan

    probs = scan(path)
    for p in probs:
        click.echo(p)
    if not probs:
        click.echo("OK 可直接运行")
