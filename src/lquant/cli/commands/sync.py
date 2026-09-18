"""lq sync：系统级定时同步入口（launchd / cron 触发，不依赖服务器进程）。

服务进程内的 sync-worker 只在 uvicorn 在线时调度；机器重启、服务未起、
另一进程占锁等场景会漏采。`lq sync tick` 让 OS 级定时器可以直接驱动同一套
sync_job 调度语义（到期判断、跨天补跑、交易日过滤都在 manager 里）。
"""
from __future__ import annotations

import click

from lquant.sync import manager


@click.group()
def sync() -> None:
    """定时同步作业（tick/status/run）"""


@sync.command()
def tick() -> None:
    """跑一遍所有到期作业（launchd/cron 每日多次触发这个即可）。"""

    done = manager.tick()
    if not done:
        click.echo("no due jobs")
        return
    for r in done:
        click.echo(f"{r['sync_id']} {r['status']} rows={r['rows']}")


@sync.command()
def status() -> None:
    """列出同步作业、最近状态与**数据新鲜度**。

    作业的 last_status=ok 不等于数据是新的：无到期、断点全跳过、源零返回
    都会记 ok。所以状态视图必须同时给出「各类数据最新到哪一天」，
    否则「同步在跑」和「同步空转」看起来一模一样。
    """

    jobs = manager.list_jobs()
    if not jobs:
        click.echo("no jobs (先启动服务或调 lq sync seed)")
        return
    for j in jobs:
        click.echo(
            f"{j['sync_id']:<12} {j['schedule_time']:<22} wd={j['weekdays']:<9} "
            f"enabled={j['enabled']} last={j['last_run_at']} "
            f"status={j['last_status']} rows={j['last_rows']}")

    click.echo("")
    click.echo("数据新鲜度：")
    fresh = manager.freshness()
    lake = fresh.get("daily_lake") or "-"
    lag = fresh.get("lag_days")
    lag_s = "" if lag is None else f"（落后 {lag} 个交易日）"
    click.echo(f"  日线湖最新交易日: {lake} {lag_s}".rstrip())
    news = fresh.get("news")
    if news:
        click.echo(f"  资讯最新一条: {news['latest'] or '-'}"
                   f"（今日 {news['today_rows']} 条）")
    else:
        click.echo("  资讯最新一条: -（news_item 表未建或无数据）")
    fin = fresh.get("financial_pit")
    if fin:
        click.echo(f"  PIT 财务覆盖区间: {fin['covered_start']} ~ "
                   f"{fin['covered_end']}（{fin['symbols']} 只，记账 "
                   f"{fin['marked']} 只）")
    else:
        click.echo("  PIT 财务覆盖区间: -（无窗口记账，下次同步会按窗口重拉）")


@sync.command()
@click.argument("sync_id")
def run(sync_id: str) -> None:
    """强制执行单个作业（不看是否到期）。"""

    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    if sync_id not in jobs:
        raise click.ClickException(
            f"未知作业 {sync_id!r}，可用: {', '.join(jobs) or '（无）'}")
    res = manager.run_job(jobs[sync_id])
    click.echo(f"{sync_id} {res['status']} rows={res['rows']}")


@sync.command()
def seed() -> None:
    """写入默认作业定义（幂等）。"""

    n = manager.seed_defaults()
    click.echo(f"seeded {n}")
