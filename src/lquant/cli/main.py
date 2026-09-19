"""lq 命令。"""
from __future__ import annotations

import click

from lquant.cli.commands import agent, backtest, data, factor, paper, strategy
from lquant.cli.commands import qlib as qlib_cmd
from lquant.cli.commands import sync as sync_cmd
from lquant.cli.commands import worker as worker_cmd


@click.group()
@click.version_option("0.1.0")
def cli() -> None:
    """lquant 命令行"""


cli.add_command(data.data)
cli.add_command(factor.factor)
cli.add_command(agent.agent)
cli.add_command(backtest.backtest)
cli.add_command(strategy.strategy)
cli.add_command(paper.paper)
cli.add_command(worker_cmd.worker)
cli.add_command(sync_cmd.sync)
cli.add_command(qlib_cmd.qlib)


if __name__ == "__main__":
    cli()
