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
        fund_type_keywords=raw.get("fund_type_keywords", {}) or {},
    )


def default_slippage(ruleset: RuleSet | None = None) -> tuple[str, dict]:
    """规则表里配置的默认滑点 (mode, params)。

    引擎不再把滑点写死在代码里 —— cn_a_share.yaml 的 `default.slippage`
    是唯一真源，改成本假设不需要改代码。
    """
    rs = ruleset or load_ruleset()
    cfg = (rs.default or {}).get("slippage") or {}
    mode = str(cfg.get("mode", "pct"))
    params = {k: v for k, v in cfg.items() if k != "mode"}
    return mode, params
