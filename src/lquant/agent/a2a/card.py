"""Agent Card 构建：把 lquant 的能力（skills / MCP 工具）发布成 A2A 发现文档。

``skills[]`` **从 ``config/skills/*/SKILL.md`` 的 frontmatter 派生** ——
新增一个 skill 目录就自动出现在卡片里，不用改代码，也不会出现
「卡片写了但实际不存在」的漂移。缺 frontmatter / 缺 name / 缺 description
的目录被**跳过并记 warning**，不让一个坏 skill 拖垮整个发现端点。
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from lquant.agent.a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)

_LOG = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"
AGENT_VERSION = "0.1.0"
AGENT_NAME = "lquant A股量化研究助手"

#: provider → 卡片描述里用的执行体名。卡片是给**外部调用方**看的发现文档，
#: 写死某一个 provider 会在换后端后变成假信息（本仓 provider 已有两个真后端）。
_PROVIDER_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "mock": "Mock（脚本化演示，非 LLM）",
}


def default_description(provider: str) -> str:
    """按当前 provider 生成卡片描述（未知取值直接原样展示，别吞掉）。"""
    label = _PROVIDER_LABELS.get(provider, provider)
    return (
        f"基于 lquant 数据湖回答 A 股行情的 AI 助手，内置 {label} 执行体："
        "大盘概览、涨跌家数、板块、资金流、涨停池、龙虎榜、指数与 ETF 数据查询，"
        "以及因子挖掘与评估。回答会标注数据时点。"
    )


def parse_frontmatter(text: str) -> dict | None:
    """抽取 SKILL.md 顶部的 YAML frontmatter（``---`` 包裹）。无则 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                return None
            return data if isinstance(data, dict) else None
    return None


def load_skills(skills_dir: Path) -> list[AgentSkill]:
    """扫描 ``<skills_dir>/*/SKILL.md``，按 frontmatter 生成 AgentSkill 列表。"""
    if not skills_dir.is_dir():
        return []
    out: list[AgentSkill] = []
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        if not meta:
            _LOG.warning("skill %s 缺 frontmatter，已跳过（不进 Agent Card）", child.name)
            continue
        name = str(meta.get("name") or "").strip()
        desc = str(meta.get("description") or "").strip()
        if not name or not desc:
            _LOG.warning("skill %s 的 frontmatter 缺 name/description，已跳过", child.name)
            continue
        tags = meta.get("tags") or []
        out.append(AgentSkill(
            id=child.name, name=name, description=desc,
            tags=[str(t) for t in tags] if isinstance(tags, list) else [str(tags)]))
    return out


def build_agent_card(
    *,
    base_url: str,
    skills: list[AgentSkill],
    description: str,
    with_bearer_auth: bool = False,
) -> AgentCard:
    """组装 Agent Card。

    ``description`` 是必填而不是给个写死的默认值：它要随 provider 变
    （见 :func:`default_description`），留默认值就等于留一处会过期的假描述。
    本函数保持纯函数（不读 settings），provider 由调用方传入。
    """
    card = AgentCard(
        name=AGENT_NAME,
        description=description,
        version=AGENT_VERSION,
        supportedInterfaces=[AgentInterface(
            url=f"{base_url}/a2a",
            protocolBinding="JSONRPC",
            protocolVersion=PROTOCOL_VERSION,
        )],
        capabilities=AgentCapabilities(streaming=True),
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        skills=skills,
        provider=AgentProvider(organization="lquant"),
        documentationUrl=f"{base_url}/docs",
    )
    if with_bearer_auth:
        card.security_schemes = {
            "bearer": {"type": "http", "scheme": "bearer",
                       "description": "LQ_A2A_TOKEN（服务端设置后必填）"},
        }
        card.security_requirements = [{"bearer": []}]
    return card
