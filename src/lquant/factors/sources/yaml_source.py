"""custom.yaml 来源 adapter：把 config/factors/custom.yaml 里的因子接进统一管线。

config/factors/custom.yaml 此前是死配置（缺陷 #5）—— 本 adapter 让它活过来：
读取 name/expr/category，expr 必须是 lquant DSL（过 parse+check 校验）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.parser import parse

CUSTOM_YAML = Path("config/factors/custom.yaml")


def load_custom() -> list[dict]:
    """读取 custom.yaml 因子清单，逐条过 DSL 校验，失败 fail-fast。"""
    if not CUSTOM_YAML.exists():
        return []
    raw = yaml.safe_load(CUSTOM_YAML.read_text()) or {}
    out = []
    for it in raw.get("factors") or []:  # `factors:` 空值 → None，按空清单处理
        ast = parse(it["expr"], it["name"])
        check(ast)
        out.append({
            "name": it["name"],
            "expression": it["expr"],
            "description": it.get("category", ""),
            "source": "yaml",
            "source_ref": str(CUSTOM_YAML),
            "factor_id": None,
            "category": it.get("category", ""),
        })
    return out


def factor_id(expr: str) -> str:
    from lquant.factors.sources.qlib_source import factor_id as _fid

    return _fid(expr)
