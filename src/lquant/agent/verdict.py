"""Agent 结论的结构化契约：**数字字段没有 source/as_of 就拒收**。

自由文本回答里的数字是不可回溯的：「净利润增长 25%」——哪来的、到今天为止吗、
是不是上一版报告？本模块把结论框成一个可校验的对象：

- `Claim`（一条量化论断）**强制**带 `source` + `as_of`，缺一即拒收；
- `Evidence`（引文）同样强制 `source` + `as_of`；
- 不下结论必须走 `abstain=True`，并写明 `withheld` 理由（为什么放弃），
  而不是给一个含糊的「中性」；
- 历史时点查不到 → 工具返回 `unavailable`，**禁止用当前数据顶替**（见
  `market/collectors` 之上 MCP 工具的 as_of 语义）。

校验失败抛 `VerdictRejected`，MCP 侧会把消息回给模型让它重交 —— 拒收必须
**可见且可重试**，不能静默降级成自由文本。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

__all__ = [
    "AgentVerdict",
    "Claim",
    "Evidence",
    "VerdictRejected",
    "parse_verdict",
    "verdict_payload",
]

Direction = Literal["看多", "看空", "中性", "abstain"]


class VerdictRejected(ValueError):
    """结论不符合契约。消息面向模型，要能读懂并据此重交。"""


class Claim(BaseModel):
    """一条量化论断。`source` / `as_of` 是必填 —— 这是本契约的核心约束。"""

    metric: str = Field(min_length=1, description="指标名，如 北向成交额")
    value: float = Field(description="数值")
    unit: str = Field(default="", description="单位，如 亿元")
    source: str = Field(min_length=1, description="数据来源，如 northbound_flow")
    as_of: str = Field(min_length=1, description="数据时点，如 2026-10-08")


class Evidence(BaseModel):
    source: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    quote: str = Field(default="")


class AgentVerdict(BaseModel):
    ticker: str = Field(default="", description="标的代码；abstain 时可为空")
    as_of: str = Field(default="", description="结论依据的数据时点")
    direction: Direction = "abstain"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    abstain: bool = False
    withheld: list[str] = Field(
        default_factory=list,
        description="放弃下结论的原因（数据缺/口径不明/样本不足…）")

    @model_validator(mode="after")
    def _enforce(self) -> AgentVerdict:
        if self.abstain:
            if self.direction != "abstain":
                raise ValueError("abstain=true 时 direction 必须是 abstain")
            if not self.withheld:
                raise ValueError(
                    "abstain=true 必须给出 withheld 理由（数据缺/口径不明/样本不足…），"
                    "不允许给一个无理由的弃权")
            return self
        if self.direction == "abstain":
            raise ValueError("direction=abstain 必须同时设 abstain=true")
        missing = [n for n in ("ticker", "as_of") if not str(getattr(self, n)).strip()]
        if missing:
            raise ValueError(f"下结论必须给出 {'/'.join(missing)}")
        if not self.evidence:
            raise ValueError("下结论必须至少给一条 evidence（含 source 与 as_of）")
        if self.confidence <= 0:
            raise ValueError("下结论必须给出大于 0 的 confidence")
        return self


def parse_verdict(raw: Any) -> AgentVerdict:
    """从 dict / JSON 文本构造并校验；失败统一抛 VerdictRejected。"""
    if isinstance(raw, AgentVerdict):
        return raw
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise VerdictRejected(f"结论不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise VerdictRejected(f"结论必须是 JSON 对象，收到 {type(data).__name__}")
    # MCP 客户端有时会把整个对象塞进一个包装键里
    for wrapper in ("verdict", "arguments", "input"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and ("direction" in inner or "abstain" in inner):
            data = inner
            break
    try:
        return AgentVerdict.model_validate(data)
    except ValidationError as e:
        raise VerdictRejected(f"结论不符合契约：{_brief(e)}") from e


def _brief(e: ValidationError) -> str:
    """把 pydantic 的报错压成模型能读懂的一行（含定位路径）。"""
    parts = []
    for err in e.errors()[:6]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg')}")
    return "；".join(parts)


def verdict_payload(v: AgentVerdict) -> dict:
    """落库用的一行（不依赖 pydantic 的序列化细节）。"""
    return {
        "ticker": v.ticker.strip(),
        "as_of": v.as_of.strip(),
        "direction": v.direction,
        "confidence": float(v.confidence),
        "abstain": 1 if v.abstain else 0,
        "summary": v.summary.strip(),
        "payload_json": json.dumps(v.model_dump(), ensure_ascii=False, default=str),
        "withheld_json": json.dumps(v.withheld, ensure_ascii=False),
    }
