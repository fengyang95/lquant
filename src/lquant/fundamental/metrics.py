"""基本面指标目录：模块划分、权重、方向、**取数来源**。

**与 FinancialTool 的关键差异**：那个实现声明「盈利与现金流」模块权重 30，
但子项合计只有 25，五个模块上限合计 95 而非 100 —— 于是「优秀 ≥85」档位
几乎不可达，评分体系自相矛盾。本模块用 :func:`validate_modules` 在导入时
强制「子项满分之和 == 模块权重」且「模块权重之和 == 100」，把这类漂移
变成**导入即失败**，而不是运行几个月后才被发现。

**取数来源（``source``）** —— 一个指标的值可以从三个地方来，必须显式声明：

``pit``
    直接读 ``financial_pit`` 的物理键（``indicator.roe`` / ``indicator.current_ratio`` …）。
    键名以**真实库 DISTINCT 勘察**为准，与
    :mod:`lquant.research.dialect.fundamentals` 的 ``FIELD_MAP`` 同一口径。
``derived``
    ``financial_pit`` 里没有可直接读的比率，由原始科目现算
    （实现见 :mod:`lquant.fundamental.derive`，键名前缀 ``derived.``）。
``valuation``
    日线湖的估值列（``pe_ttm`` / ``pb_mrq`` / ``dv_ttm``），
    **不在** ``financial_pit`` 里；实现见 :mod:`lquant.fundamental.valuation`。

历史教训（本模块的一等约束）：目录里的 ``item`` 曾经整体沿用 BaoStock/同花顺的
遗留键名（``profit.roeAvg`` / ``balance.currentRatio`` / ``dupont.dupontNitogr``），
而库里真实数据是 Tushare 的 ``indicator.*`` —— 17 个指标**全部**取不到值，
页面恒显示「财务数据为空」，用户会误以为没同步数据而去反复重跑回填。
因此 :func:`pit_items` 只暴露真正要读的物理键，且有
``tests/unit/test_fundamental_registry_contract.py`` 用固定字典锁死契约。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "MODULE_LABELS",
    "MODULE_WEIGHTS",
    "METRICS",
    "SOURCES",
    "RatioMetric",
    "metrics_by_module",
    "metrics_by_module_of",
    "pit_items",
    "total_weight",
    "validate_modules",
]

#: 合法取数来源
SOURCES: frozenset[str] = frozenset({"pit", "derived", "valuation"})


@dataclass(frozen=True)
class RatioMetric:
    """一个按「行业内相对分位」评分的基本面比率。

    Attributes:
        item: 逻辑键，同时是评分明细里的 ``item``。``pit`` 源下它就是
            ``financial_pit.item``；``derived`` / ``valuation`` 源下是本层
            自造的稳定键（不会被误当成库里的键）。
        label: 中文名。
        module: 所属模块（必须在 :data:`MODULE_WEIGHTS` 里）。
        max_score: 该指标满分；同模块内子项之和必须等于模块权重。
        higher_better: 方向。``False`` 表示越小越好（周转天数、PE、资产负债率）。
        source: ``pit`` | ``derived`` | ``valuation``。
    """

    item: str
    label: str
    module: str
    max_score: float
    higher_better: bool = True
    source: str = "pit"

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(
                f"{self.item}: 未知取数来源 {self.source!r}，可选 {sorted(SOURCES)}"
            )

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

#: 指标目录。每个模块内 max_score 之和必须等于 MODULE_WEIGHTS[module]。
#: ``pit`` 键名经真实库（data/duckdb/lquant.duckdb）DISTINCT 勘察确认。
METRICS: tuple[RatioMetric, ...] = (
    # --- 盈利能力 25 ---
    RatioMetric("indicator.roe", "净资产收益率", "profitability", 8.0),
    RatioMetric("indicator.netprofit_margin", "销售净利率", "profitability", 6.0),
    RatioMetric("indicator.grossprofit_margin", "销售毛利率", "profitability", 6.0),
    RatioMetric("indicator.profit_to_gr", "净利润/营业总收入", "profitability", 5.0),
    # --- 现金质量 20（库内无直接比率，全部现算）---
    RatioMetric("derived.cfo_to_np", "经营现金流/净利润", "cashflow", 8.0, source="derived"),
    RatioMetric("derived.cfo_to_or", "经营现金流/营业收入", "cashflow", 6.0, source="derived"),
    RatioMetric("derived.cfo_to_op", "经营现金流/营业利润", "cashflow", 6.0, source="derived"),
    # --- 营运效率 15（周转天数越低越好）---
    RatioMetric("derived.ar_turn_days", "应收周转天数", "efficiency", 5.0,
                higher_better=False, source="derived"),
    RatioMetric("derived.inv_turn_days", "存货周转天数", "efficiency", 5.0,
                higher_better=False, source="derived"),
    RatioMetric("indicator.assets_turn", "总资产周转率", "efficiency", 5.0),
    # --- 偿债能力 20（资产负债率越低越好）---
    RatioMetric("indicator.current_ratio", "流动比率", "solvency", 5.0),
    RatioMetric("indicator.quick_ratio", "速动比率", "solvency", 5.0),
    RatioMetric("indicator.debt_to_assets", "资产负债率", "solvency", 5.0,
                higher_better=False),
    RatioMetric("derived.ebit_to_interest", "利息保障倍数", "solvency", 5.0,
                source="derived"),
    # --- 估值 20（来自日线湖估值列，而不是 financial_pit）---
    RatioMetric("valuation.pe_ttm", "市盈率 TTM", "valuation", 8.0,
                higher_better=False, source="valuation"),
    RatioMetric("valuation.pb", "市净率", "valuation", 6.0,
                higher_better=False, source="valuation"),
    RatioMetric("valuation.dividend_yield", "股息率", "valuation", 6.0,
                source="valuation"),
)


def metrics_by_module_of(
    metrics: Sequence[RatioMetric],
) -> dict[str, tuple[RatioMetric, ...]]:
    """把**任意**指标序列按 ``module`` 分组（保持给定顺序）。

    与 :func:`metrics_by_module` 的区别：这里不做「模块必须已声明」的过滤，
    目录未声明的模块也会出现 —— 调用方（如 :func:`module_coverage`）需要
    看见它们，否则明细里的行会被静默漏掉。
    """
    out: dict[str, list[RatioMetric]] = {}
    for m in metrics:
        out.setdefault(m.module, []).append(m)
    return {k: tuple(v) for k, v in out.items()}


def metrics_by_module() -> dict[str, tuple[RatioMetric, ...]]:
    """目录（全局 :data:`METRICS`）按模块分组；结果键恒为已声明的模块。"""
    grouped = metrics_by_module_of(METRICS)
    return {mod: grouped.get(mod, ()) for mod in MODULE_WEIGHTS}


def pit_items() -> tuple[str, ...]:
    """需要从 ``financial_pit`` 直接读的物理键（``source == "pit"``）。"""
    return tuple(m.item for m in METRICS if m.source == "pit")


def total_weight() -> float:
    return sum(MODULE_WEIGHTS.values())


def validate_modules() -> None:
    """校验权重自洽；不自洽直接抛错（导入时调用）。

    Raises:
        ValueError: 模块权重合计 ≠ 100，或某模块子项满分之和 ≠ 模块权重，
            或指标引用了未声明的模块，或 ``item`` 重复。
    """
    unknown = {m.module for m in METRICS} - set(MODULE_WEIGHTS)
    if unknown:
        raise ValueError(f"指标引用了未声明的模块: {sorted(unknown)}")

    names = [m.item for m in METRICS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"指标 item 重复（评分明细会串行）: {dupes}")

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

    # 物理键格式在**目录**这一层才强制（RatioMetric 作为通用值类型时允许
    # "a"/"b" 这类简写，便于单测构造最小目录）。放在权重校验之后，
    # 让「权重不自洽」这个更根本的错误优先暴露。
    bad = [m.item for m in METRICS if m.source == "pit" and "." not in m.item]
    if bad:
        raise ValueError(f"pit 源的 item 必须是 '<前缀>.<字段>' 形式: {sorted(bad)}")


validate_modules()
