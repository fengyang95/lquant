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


@strategy.command()
@click.argument("symbol")
@click.option("--start", default=None, help="起始日（YYYY-MM-DD），缺省取全历史")
@click.option("--end", default=None)
@click.option("--min-amount", type=float, default=2e8,
              show_default=True, help="放量上涨的成交额下限（元）")
def screen(symbol: str, start: str | None, end: str | None,
           min_amount: float) -> None:
    """对单标的跑全部选股策略（一策略一判定 + evidence）。

    数据来自本地 Parquet 湖（先 lq data sync）；读不到数据时报错退出。
    """
    import json

    from lquant.core.types import parse_symbol
    from lquant.data.store.parquet import read_daily

    sym = str(parse_symbol(symbol)) if symbol else symbol
    lf = read_daily(symbols=[sym], start=start, end=end)
    df = lf.collect()
    if df.is_empty():
        raise click.ClickException(
            f"{sym} 无本地日线 —— 先跑 lq data sync（策略库只吃本地湖，不打线上）")
    df = df.sort("trade_date")
    from lquant.portfolio.strategies import run_all
    results = run_all(df, min_amount=min_amount)
    click.echo(json.dumps(
        {"symbol": sym, "asof": str(df["trade_date"][-1]),
         "results": [r.__dict__ for r in results]},
        ensure_ascii=False, indent=1))
