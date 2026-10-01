"""基本面指标目录：模块划分、权重、方向。

**与 FinancialTool 的关键差异**：那个实现声明「盈利与现金流」模块权重 30，
但子项合计只有 25，五个模块上限合计 95 而非 100 —— 于是「优秀 ≥85」档位
几乎不可达，评分体系自相矛盾。本模块用 :func:`validate_modules` 在导入时
强制「子项满分之和 == 模块权重」且「模块权重之和 == 100」，把这类漂移
变成**导入即失败**，而不是运行几个月后才被发现。

``item`` 用的是 ``financial_pit`` 里的键。BaoStock 路径写入的键形如
``"{kind}.{col}"``（``profit.roeAvg`` / ``balance.currentRatio`` …），
估值类（``valuation.*``）由调用方另行落表，缺失时评分会跳过并计入覆盖率。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MODULE_LABELS",
    "MODULE_WEIGHTS",
    "METRICS",
    "RatioMetric",
    "metrics_by_module",
    "total_weight",
    "validate_modules",
]


@dataclass(frozen=True)
class RatioMetric:
    """一个按「行业内相对分位」评分的基本面比率。"""

    item: str
    label: str
    module: str
    max_score: float
    higher_better: bool = True

    @property
    def direction(self) -> str:
        return "越高越好" if self.higher_better else "越低越好"


#: 模块 → 满分。合计必须为 100
MODULE_WEIGHTS: dict[str, float] = {
    "profitability": 25.0,
    "cashflow": 20.0,
    "efficiency": 15.0,
    "solvency": 20.0,
    "valuation": 20.0,
}

MODULE_LABELS: dict[str, str] = {
    "profitability": "盈利能力",
    "cashflow": "现金质量",
    "efficiency": "营运效率",
    "solvency": "偿债能力",
    "valuation": "估值水平",
}

#: 指标目录。每个模块内 max_score 之和必须等于 MODULE_WEIGHTS[module]
METRICS: tuple[RatioMetric, ...] = (
    # 盈利能力 25
    RatioMetric("profit.roeAvg", "净资产收益率", "profitability", 8.0),
    RatioMetric("profit.npMargin", "销售净利率", "profitability", 6.0),
    RatioMetric("profit.gpMargin", "销售毛利率", "profitability", 6.0),
    RatioMetric("dupont.dupontNitogr", "净利润/营业总收入", "profitability", 5.0),
    # 现金质量 20
    RatioMetric("cashflow.CFOToNP", "经营现金流/净利润", "cashflow", 8.0),
    RatioMetric("cashflow.CFOToOR", "经营现金流/营业收入", "cashflow", 6.0),
    RatioMetric("cashflow.CFOToGr", "经营现金流/营业利润", "cashflow", 6.0),
    # 营运效率 15（周转天数越低越好）
    RatioMetric("operation.NRTurnDays", "应收周转天数", "efficiency", 5.0, higher_better=False),
    RatioMetric("operation.INVTurnDays", "存货周转天数", "efficiency", 5.0, higher_better=False),
    RatioMetric("operation.AssetTurnRatio", "总资产周转率", "efficiency", 5.0),
    # 偿债能力 20（资产负债率越低越好）
    RatioMetric("balance.currentRatio", "流动比率", "solvency", 5.0),
    RatioMetric("balance.quickRatio", "速动比率", "solvency", 5.0),
    RatioMetric("balance.liabilityToAsset", "资产负债率", "solvency", 5.0, higher_better=False),
    RatioMetric("cashflow.ebitToInterest", "利息保障倍数", "solvency", 5.0),
    # 估值 20（PE/PB 越低越好）
    RatioMetric("valuation.pe_ttm", "市盈率 TTM", "valuation", 8.0, higher_better=False),
    RatioMetric("valuation.pb", "市净率", "valuation", 6.0, higher_better=False),
    RatioMetric("valuation.dividend_yield", "股息率", "valuation", 6.0),
)


def metrics_by_module() -> dict[str, tuple[RatioMetric, ...]]:
    """模块 → 指标元组（保持目录顺序）。"""
    out: dict[str, tuple[RatioMetric, ...]] = {m: () for m in MODULE_WEIGHTS}
    for mod in MODULE_WEIGHTS:
        out[mod] = tuple(m for m in METRICS if m.module == mod)
    return out


def total_weight() -> float:
    return sum(MODULE_WEIGHTS.values())


def validate_modules() -> None:
    """校验权重自洽；不自洽直接抛错（导入时调用）。

    Raises:
        ValueError: 模块权重合计 ≠ 100，或某模块子项满分之和 ≠ 模块权重，
            或指标引用了未声明的模块。
    """
    unknown = {m.module for m in METRICS} - set(MODULE_WEIGHTS)
    if unknown:
        raise ValueError(f"指标引用了未声明的模块: {sorted(unknown)}")

    total = total_weight()
    if abs(total - 100.0) > 1e-9:
        raise ValueError(f"模块权重合计应为 100，实际 {total}")

    for mod, items in metrics_by_module().items():
        got = sum(m.max_score for m in items)
        want = MODULE_WEIGHTS[mod]
        if abs(got - want) > 1e-9:
            raise ValueError(
                f"模块 {mod} 子项满分之和 {got} ≠ 模块权重 {want}"
                f"（子项：{[(m.item, m.max_score) for m in items]}）"
            )


validate_modules()
