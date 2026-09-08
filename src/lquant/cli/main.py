"""lq 命令。"""
from __future__ import annotations

import click

from lquant.cli.commands import backtest, data, factor, strategy


@click.group()
@click.version_option("0.1.0")
def cli() -> None:
    """lquant 命令行"""


cli.add_command(data.data)
cli.add_command(factor.factor)
cli.add_command(backtest.backtest)
cli.add_command(strategy.strategy)


if __name__ == "__main__":
    cli()
