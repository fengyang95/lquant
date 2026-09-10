"""研报复现（方案第七节）：FactorSpec + 归因五类。

只有 1) 对话式确认 assumptions 与 neutral_spec 留 null 不代猜、
2) 落地 FactorSpec 是 LLM 的活；3) 计算 4) 比对归因必须平台算。
"""
from __future__ import annotations

from dataclasses import dataclass, field

ATTRIBUTION_CODES = {
    "EXPR_MISREAD": "表达式误读 —— 研报公式与实现口径差",
    "PARAM_ASSUMED": "参数是猜的 —— 研报没写、由复现者假设（assumptions 非空）",
    "DATA_CALIBER": "数据口径差 —— vwap/市值/复权方式等与研报不一致",
    "WINDOW_MISMATCH": "窗口不一致 —— 时间段/调仓频率不同",
    "REPORT_SUSPECT": "研报本身存疑 —— 方向反或显著性依赖未来函数/幸存者偏差（D 级）",
}


@dataclass(frozen=True)
class FactorSpec:
    name: str
    expr: str
    claimed: dict | None = None
    assumptions: list[str] = field(default_factory=list)
    neutral_spec: dict | None = None
    window: dict | None = None
    universe: str | None = None
    rationale: str = ""


def load_spec(path: str):
    """yaml -> FactorSpec（语法 fail-fast）。"""
    import yaml

    with open(path) as fh:
        raw = yaml.safe_load(fh)
    from lquant.factors.dsl.parser import parse

    parse(raw["expr"], raw.get("name", "spec"))
    return FactorSpec(
        name=raw["name"], expr=raw["expr"],
        claimed=raw.get("claimed"),
        assumptions=list(raw.get("assumptions", [])),
        neutral_spec=raw.get("neutral_spec"),
        window=raw.get("window"),
        universe=raw.get("universe"),
        rationale=raw.get("rationale", ""))


def attribute(claimed: dict | None, recomputed: dict, grade: str,
              assumptions: list[str] | None = None) -> list[str]:
    """比对归因：claimed vs 平台重算值 -> 归因码列表。"""
    codes = []
    if grade == "D":
        codes.append("REPORT_SUSPECT")
    elif grade in ("C", "X"):
        if assumptions:
            codes.append("PARAM_ASSUMED")
        codes.extend(["EXPR_MISREAD", "DATA_CALIBER", "WINDOW_MISMATCH"])
    return codes
