"""lq worker：拉起 worker 进程组（1 通用 + K 回测）。"""
from __future__ import annotations

import click

from lquant.core.config import BACKTEST_WORKERS_MAX, clamp_backtest_workers


def _resolve_counts(general: int, backtest: int | None) -> tuple[int, int]:
    """CLI > env/config > 默认；clamp 0..4。"""
    if backtest is None:
        from lquant.core.config import get_settings

        backtest = get_settings().backtest_workers
    return max(0, general), clamp_backtest_workers(backtest)


@click.command()
@click.option("--general", default=1, type=int,
              help="通用 worker 数（订阅 default+ingest 队列）；0 关闭")
@click.option("--backtest", default=None, type=int,
              help=f"回测 worker 数（默认取配置，上限 {BACKTEST_WORKERS_MAX}）")
def worker(general: int, backtest: int | None) -> None:
    """启动 worker 进程组（需 Redis）。"""
    g, b = _resolve_counts(general, backtest)
    if g == 0 and b == 0:
        click.echo("错误: general 与 backtest 均为 0，无进程可拉起", err=True)
        raise SystemExit(1)
    from lquant.monitor.worker import run_supervisor

    run_supervisor(general=g, backtest=b)
