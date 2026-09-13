"""lq paper —— 模拟盘：账户 / 人工下单 / 盘中 tick / 日终对账。

典型节奏（日频策略）：
    lq paper create demo --cash 1000000 --universe 600000.SH,510300.SH
    lq paper order demo --symbol 600000.SH --side buy --qty 1000   # 人工建仓
    lq paper tick demo        # 盘中跑几次：拉快照→撮合→盯市→intraday 净值
    lq paper close demo       # 收盘后：解冻 T+N + 官方日线对账 → official 净值
    lq paper status demo && lq paper nav demo
"""
from __future__ import annotations

import json

import click


@click.group()
def paper() -> None:
    """模拟盘（paper trading）"""


@paper.command()
@click.argument("name")
@click.option("--cash", "initial_cash", default=1_000_000.0, show_default=True)
@click.option("--strategy", default="manual", show_default=True,
              help="'manual' 或 module:Attr（实现 signals(broker, quote)）")
@click.option("--universe", default="", help="逗号分隔的候选池，如 600000.SH,510300.SH")
def create(name: str, initial_cash: float, strategy: str, universe: str) -> None:
    """创建模拟盘账户"""
    from lquant.paper import service
    acct = service.create_account(name, initial_cash, strategy,
                                  [s for s in universe.split(",") if s.strip()])
    click.echo(_dump(acct))


@paper.command()
@click.argument("name")
@click.option("--symbol", required=True)
@click.option("--side", required=True, type=click.Choice(["buy", "sell"]))
@click.option("--qty", required=True, type=int)
@click.option("--price", type=float, default=None,
              help="限价；缺省取实时最新价（停牌时必须显式指定）")
def order(name: str, symbol: str, side: str, qty: int, price: float | None) -> None:
    """人工提交委托"""
    from lquant.paper import service
    click.echo(_dump(service.submit_order(name, symbol, side, qty, price)))


@paper.command("tick")
@click.argument("name")
def tick_cmd(name: str) -> None:
    """盘中推进一次：拉实时快照 → 策略信号 → 撮合 → 盯市 → intraday 净值"""
    from lquant.paper import service
    click.echo(_dump(service.tick(name)))


@paper.command()
@click.argument("name")
@click.option("--date", "d", default=None, help="缺省为今天（北京时间）")
def close(name: str, d: str | None) -> None:
    """日终结算：解冻 T+N + 官方日线对账重算 official 净值"""
    from lquant.paper import service
    click.echo(_dump(service.day_close(name, d)))


@paper.command()
@click.argument("name")
def status(name: str) -> None:
    """账户状态：净值/持仓/挂单/最近委托"""
    from lquant.paper import service
    click.echo(_dump(service.status(name)))


@paper.command()
@click.argument("name")
@click.option("--source", type=click.Choice(["intraday", "official"]),
              default=None, help="缺省全部来源（对账用 official）")
def nav(name: str, source: str | None) -> None:
    """净值曲线"""
    from lquant.paper import service
    click.echo(_dump(service.nav_history(name, source)))


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
