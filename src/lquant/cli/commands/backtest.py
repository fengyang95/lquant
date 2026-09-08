"""lq backtest。"""
from __future__ import annotations

import click


@click.group()
def backtest() -> None:
    """回测"""


@backtest.command()
@click.argument("strategy")
@click.option("--start", required=True)
@click.option("--end", required=True)
def run(strategy: str, start: str, end: str) -> None:
    click.echo(f"run {strategy} {start}~{end}")
