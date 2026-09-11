"""统一 Agent 注册表（方案 6.4）：一个注册表，差异只在 driver。

config/agents/*.yaml，fail-fast 校验。kind x driver 正交：
  builtin + platform   内置算法（gp/random/llm-proposals）——平台循环驱动
  skill + agent        Claude Code / WorkBuddy / 人类 —— 装 SKILL.md 转cli
  external + platform  远程挖掘器 —— HTTP / 子进程 JSON stdio / MCP
公共不变量：同一门禁、同一口径、同一份记账（factor_mining_run.agent 记名）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml

_LOG = logging.getLogger(__name__)

AGENT_KINDS = {"builtin", "skill", "external"}
DRIVERS = {"platform", "agent"}


@dataclass(frozen=True)
class AgentProfile:
    name: str
    kind: str
    driver: str

    quota_eval: int = 200          # eval 配额（按 Agent 记账）
    can_submit: bool = False
    can_export_data: bool = False
    enabled: bool = True


def load_agents(directory: str = "config/agents") -> list[AgentProfile]:
    """读 config/agents/*.yaml，fail-fast：格式错/未知 kind/driver 直接抛。"""
    from pathlib import Path

    d = Path(directory)
    if not d.exists():
        return []
    out = []
    for fp in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(fp.read_text()) or {}
        name = raw.get("name") or fp.stem
        kind = raw.get("kind", "skill")
        driver = raw.get("driver", "agent")
        if kind not in AGENT_KINDS:
            raise ValueError(f"{fp}: 未知 kind {kind!r}，可选 {sorted(AGENT_KINDS)}")
        if driver not in DRIVERS:
            raise ValueError(f"{fp}: 未知 driver {driver!r}，可选 {sorted(DRIVERS)}")
        perms = raw.get("permissions", {})
        out.append(AgentProfile(
            name=name, kind=kind, driver=driver,
            quota_eval=int(perms.get("quota_eval", 200)),
            can_submit=bool(perms.get("can_submit", False)),
            can_export_data=bool(perms.get("can_export_data", False)),
            enabled=bool(raw.get("enabled", True)),
        ))
    return out


def find_agent(name: str) -> AgentProfile | None:
    agents = {a.name: a for a in load_agents()}
    return agents.get(name)


def record_eval(agent: str, n: int = 1) -> int:
    """eval 记账（方案 6.3 硬护栏 2）：按 Agent **原子**累加，返回累计值。

    用单条 UPSERT 而不是「先读后写」：后者在并发下会丢更新 —— 两个请求各自读到
    同一个旧值、各自写回 old+n，实际只加了一次。而这是配额护栏的计数底座，
    少记就是护栏失效。顺带也不再在 writer 事务里另开 reader 连接去读同一个库。
    """
    import datetime as dt

    from lquant.core.db import writer

    n = max(0, int(n))
    now = dt.datetime.now()
    try:
        with writer() as con:
            row = con.execute(
                "INSERT INTO agent_ledger (agent, eval_count, eval_last, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (agent) DO UPDATE SET "
                "  eval_count = agent_ledger.eval_count + excluded.eval_count, "
                "  eval_last = excluded.eval_last, updated_at = excluded.updated_at "
                "RETURNING eval_count",
                [agent, n, now, now]).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - 无库/无表时不该炸调用方
        _LOG.warning("agent_ledger 记账失败（agent=%s, n=%d），本次不计入配额",
                     agent, n, exc_info=True)
        return eval_usage(agent)


def eval_usage(agent: str) -> int:
    try:
        from lquant.core.db import reader

        with reader() as con:
            row = con.execute("SELECT eval_count FROM agent_ledger WHERE agent = ?",
                              [agent]).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - 无库环境优雅退化为 0
        return 0


def quota_remaining(agent: str) -> int:
    a = find_agent(agent)
    if not a:
        raise ValueError(f"Agent 未注册: {agent}")
    return max(0, a.quota_eval - eval_usage(agent))


def ensure_quota(agent: str, n: int) -> int:
    """配额前置检查：够就返回剩余额度，不够抛 ValueError（调用方转 422/ClickException）。

    与 CLI 的 ``lq factor eval --agent`` 共用同一个账本与同一条判定，
    避免「CLI 记账、API 不记账」两条路径口径不一。
    """
    remaining = quota_remaining(agent)
    if remaining < n:
        raise ValueError(f"配额不足: 需要 {n} 次，剩余 {remaining} 次（agent={agent}）")
    return remaining
