"""从 YAML 加载 RuleSet。"""
from __future__ import annotations

from functools import lru_cache

from lquant.backtest.rules.model import RuleSet
from lquant.core.config import load_yaml


@lru_cache(maxsize=8)
def load_ruleset(name: str = "cn_a_share") -> RuleSet:
    raw = load_yaml(f"rules/{name}.yaml")
    return RuleSet(
        market=raw.get("market", "CN"),
        currency=raw.get("currency", "CNY"),
        default=raw.get("default", {}),
        etf=raw.get("etf", {}),
        exceptions=raw.get("exceptions", {}),
    )
