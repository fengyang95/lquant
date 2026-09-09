"""llm-explorer 两段式（方案 M5）：LLM 提案 -> GP 种群初始化 -> GP 精炼。

第一段 LLM 只产字符串提案（JSONL），第二段 GP 以提案为初始种群精炼 ——
两段共用同一门禁与记账（能吐字符串就能接）。
"""
from __future__ import annotations

from lquant.factors.mining.gp import GPGenerator
from lquant.factors.mining.llm import load_proposals


def make_explorer(proposal_path: str, seed: int | None = None) -> GPGenerator:
    """LLM 提案作为 GP 初始种群的生成器闭包。"""
    props = load_proposals(proposal_path)
    gp = GPGenerator(seed=seed)
    for p in props:
        try:
            from lquant.factors.dsl.parser import parse
            from lquant.factors.dsl.printer import unparse

            gp.pop.append((unparse(parse(p["expr"]).root), 0.0))
        except Exception:  # noqa: BLE001
            continue
    return gp
