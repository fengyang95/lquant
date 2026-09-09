"""统一 Agent 注册表（方案 6.4）：一个注册表，差异只在 driver。

config/agents/*.yaml，fail-fast 校验。kind x driver 正交：
  builtin + platform   内置算法（gp/random/llm-proposals）——平台循环驱动
  skill + agent        Claude Code / WorkBuddy / 人类 —— 装 SKILL.md 转cli
  external + platform  远程挖掘器 —— HTTP / 子进程 JSON stdio / MCP
公共不变量：同一门禁、同一口径、同一份记账（factor_mining_run.agent 记名）。
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

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
