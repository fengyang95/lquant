"""lq agent：统一 Agent 注册与探针验证（方案 6.4）。lq agent test 即接入验收。"""
from __future__ import annotations

import click


@click.group()
def agent() -> None:
    """Agent 注册与探针验证"""


@agent.command("list")
def list_agents() -> None:
    """列出全部注册 Agent（config/agents/*.yaml）。"""
    from lquant.factors.agents import load_agents

    agents = load_agents()
    if not agents:
        click.echo("（config/agents/ 下没有注册 Agent）")
        return
    for a in agents:
        click.echo(f"{a.name:<16} kind={a.kind:<9} driver={a.driver:<8} "
                   f"quota={a.quota_eval:<6} submit={'Y' if a.can_submit else 'N'} "
                   f"{'enabled' if a.enabled else 'disabled'}")


@agent.command()
@click.argument("name")
def show(name: str) -> None:
    """Agent 详情（kind/driver/权限）。"""
    from lquant.factors.agents import find_agent

    a = find_agent(name)
    if not a:
        raise click.ClickException(f"Agent 未注册: {name}")
    click.echo(f"name:      {a.name}")
    click.echo(f"kind:      {a.kind}")
    click.echo(f"driver:    {a.driver}")
    click.echo(f"quota_eval: {a.quota_eval}")
    click.echo(f"can_submit: {a.can_submit}")
    click.echo(f"can_export_data: {a.can_export_data}")
    click.echo(f"enabled:   {a.enabled}")


@agent.command()
@click.argument("name")
def test(name: str) -> None:
    """接入验收：探针表达式走 G0 → eval 链路，三类 Agent 同一条链路。"""
    import json

    from lquant.factors.agents import find_agent
    from lquant.factors.mining.gates import g0_static

    a = find_agent(name)
    if not a:
        raise click.ClickException(f"Agent 未注册: {name}")
    probe = "Rank(Ts_Mean($close,5)/$close-1)"
    r = g0_static(probe)
    click.echo(json.dumps({"agent": a.name, "kind": a.kind, "driver": a.driver,
                           "probe_expr": probe, "g0": r.passed,
                           "reason_code": r.reason_code}, ensure_ascii=False))
    if not r.passed:
        raise click.ClickException("探针校验失败 —— 接入链路有问题")


@agent.command()
@click.argument("name")
@click.option("--generator", default="gp", type=click.Choice(["gp", "random", "proposals"]))
@click.option("--n", default=100, help="候选数量（预算）")
@click.option("--proposals", default=None, help="JSONL 提案文件（generator=proposals）")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
def run(name: str, generator: str, n: int, proposals: str | None,
        start: str | None) -> None:
    """平台驱动跑批：以该 Agent 身份跑一次挖掘会话（等价 `lq factor mine --agent`）。

    与 `lq factor mine` 共用同一套配额账、G0-G3 门禁与台账 ——
    「谁按回车」不改变契约（方案 6.4 公共不变量）。
    """
    import json

    from lquant.cli.commands.factor import _mine_session

    payload, warning = _mine_session(name, generator, n, proposals, start)
    if warning:
        click.echo(f"[warn] {warning}")
    click.echo(json.dumps(payload, ensure_ascii=False))


@agent.command()
@click.argument("name")
@click.option("--enable/--disable", default=None)
def freeze(name: str, enable: bool | None) -> None:
    """冻结/解冻 Agent（enabled 开关写回 yaml）。"""
    import hashlib

    import yaml

    from lquant.core.config import find_root
    from lquant.factors.agents import find_agent

    a = find_agent(name)
    if not a:
        raise click.ClickException(f"Agent 未注册: {name}")

    root = find_root()
    fp = next(iter((root / "config/agents").glob(f"{name}.yaml")))
    raw = yaml.safe_load(fp.read_text()) or {}
    new_enabled = (not a.enabled) if enable is None else enable
    raw["enabled"] = new_enabled
    # 冻结快照含 SKILL.md hash（方案 6.4）：Agent 手册被篡改即可发现
    skill_fp = root / "docs/agent-skill/SKILL.md"
    if skill_fp.exists():
        raw["skill_sha256"] = hashlib.sha256(skill_fp.read_bytes()).hexdigest()[:16]
    fp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    click.echo(f"{name}: enabled={new_enabled} "
               f"skill_sha256={raw.get('skill_sha256', '-')}")
