"""lq notify —— 通知通道配置自检与手动发送。

典型用法::

    lq notify status          # 看当前 env 配置出了哪些通道（不真发）
    lq notify test            # 向全部已配置通道发一条测试消息
    lq notify send --title "复现完成" --text "因子 mom_20 复现 IC=0.041"
"""
from __future__ import annotations

import json

import click


@click.group()
def notify() -> None:
    """多渠道 webhook 通知（企微/飞书/钉钉/Telegram/通用）"""


@notify.command("status")
def status_cmd() -> None:
    """列出 env 已配置的通道（连通性不发消息）"""
    from lquant.notify import build_chain
    chain = build_chain()
    if not chain:
        click.echo("未配置 LQ_NOTIFY_CHANNELS —— 通知旁路关闭（主链路不受影响）")
        return
    click.echo(f"已配置 {len(chain)} 个通道：")
    for ch in chain:
        click.echo(f"  - {ch.name}")


@notify.command()
@click.option("--title", default="lquant notify test")
@click.option("--text", default="这是一条 lquant 通知通道测试消息")
def test(title: str, text: str) -> None:
    """向全部已配置通道发一条测试消息，逐通道回报结果"""
    from lquant.notify import build_chain, format_results
    chain = build_chain()
    if not chain:
        click.echo("未配置 LQ_NOTIFY_CHANNELS —— 先设 env 再试")
        return
    results = [ch.send(title, text) for ch in chain]
    click.echo(format_results(results))


@notify.command()
@click.option("--title", required=True)
@click.option("--text", required=True)
def send(title: str, text: str) -> None:
    """向全部已配置通道发送自定义消息（脚本/CI 里用）"""
    from lquant.notify import format_results, notify
    results = notify(title, text)
    click.echo(json.dumps({"summary": format_results(results),
                           "results": [r.__dict__ for r in results]},
                          ensure_ascii=False, indent=1))
