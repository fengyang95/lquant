"""三表勾稽校验：用报表之间的**内部一致性**发现数据源错误与财务异常。

思路借鉴 FinancialTool 的「三表勾稽」模块，但修正了它两处会导致线上报错的实现：

1. 原实现在 ``NI is None`` 时直接做除法 → ``TypeError``。本实现所有入口
   对缺失值返回 ``None``（不计分），调用方按覆盖率处理。
2. 原实现的判定散落在各分支里，阈值与得分的对应关系不显式。本实现把
   「阈值 → 系数」抽成 :func:`banded_score`，可配置、可单测。

三个勾稽口径：

- **留存收益勾稽**：``|NI + OCI − ΔRE| / |NI|`` —— 利润表的净利润加其他综合
  收益，应当能解释资产负债表留存收益的变动。差额大 = 报表拼接错误或有
  直接计入权益的项目未被覆盖。
- **现金变动勾稽**：``|现金流量表净变动 − 资产负债表货币资金变动|`` ——
  两张表对同一件事的说法必须一致。
- **利润质量**：``|NI − 扣非净利润| / |NI|`` —— 非经常性损益占比过高时，
  当期利润不具备可持续性。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_FULL_SCORES",
    "DEFAULT_TIERS",
    "PASS_TIER",
    "ReconciliationItem",
    "ReconciliationReport",
    "banded_score",
    "cash_change_gap",
    "earnings_quality_gap",
    "ocf_to_ni_ratio",
    "reconcile",
    "retained_earnings_gap",
]

#: 各勾稽项的「满分」权重（与 FinancialTool 的 5/5/5/4 对齐，但独立于基本面总分）
DEFAULT_FULL_SCORES: dict[str, float] = {
    "retained_earnings": 5.0,
    "cash_change": 5.0,
    "earnings_quality": 4.0,
}

#: 相对差额 → 系数（按阈值升序；超出最后一档得 0）
DEFAULT_TIERS: tuple[tuple[float, float], ...] = ((0.05, 1.0), (0.15, 0.8), (0.30, 0.4))

#: 相对差额低于该值视为通过
PASS_TIER = 0.15


def _ratio(numerator: float, denominator: float) -> float | None:
    """安全比值：分母为 0/None 或分子为 None 时返回 None。"""
    if numerator is None or denominator is None:
        return None
    if abs(denominator) < 1e-12:
        return None
    return numerator / denominator


def banded_score(value: float | None,
                 tiers: Sequence[tuple[float, float]] = DEFAULT_TIERS,
                 full_score: float = 1.0) -> float:
    """按阈值分档给分；``None`` 得 0（不计分）。"""
    if value is None:
        return 0.0
    v = abs(float(value))
    for upper, factor in tiers:
        if v < upper:
            return factor * full_score
    return 0.0


def retained_earnings_gap(net_income: float | None, other_comprehensive: float | None,
                          delta_retained: float | None) -> float | None:
    """``|NI + OCI − ΔRE| / |NI|``。"""
    if net_income is None or delta_retained is None:
        return None
    oci = 0.0 if other_comprehensive is None else float(other_comprehensive)
    diff = abs(float(net_income) + oci - float(delta_retained))
    return _ratio(diff, abs(float(net_income)))


def cash_change_gap(cashflow_net_change: float | None,
                    balance_cash_change: float | None) -> float | None:
    """``|现金流量表净变动 − 货币资金变动| / max(|两者|)``。"""
    if cashflow_net_change is None or balance_cash_change is None:
        return None
    a, b = float(cashflow_net_change), float(balance_cash_change)
    denom = max(abs(a), abs(b))
    if denom < 1e-12:
        return 0.0
    return abs(a - b) / denom


def earnings_quality_gap(net_income: float | None,
                         deducted_net_income: float | None) -> float | None:
    """``|NI − 扣非净利润| / |NI|``。"""
    if net_income is None or deducted_net_income is None:
        return None
    return _ratio(abs(float(net_income) - float(deducted_net_income)),
                  abs(float(net_income)))


def ocf_to_ni_ratio(operating_cashflow: float | None,
                    net_income: float | None) -> float | None:
    """经营现金流 / 净利润。净利润为正而该比值长期 <1，说明利润没有现金支撑。"""
    if operating_cashflow is None or net_income is None:
        return None
    if float(net_income) <= 0:
        return None                      # 亏损时该比值没有解释力
    return float(operating_cashflow) / float(net_income)


@dataclass(frozen=True)
class ReconciliationItem:
    """单项勾稽结果。``gap``/``score`` 为 ``None`` 表示数据不足，且**不计入通过判定**。"""

    name: str
    label: str
    gap: float | None
    score: float
    full_score: float
    note: str = ""

    @property
    def checked(self) -> bool:
        return self.gap is not None

    @property
    def passed(self) -> bool:
        return self.gap is not None and abs(self.gap) < PASS_TIER


@dataclass(frozen=True)
class ReconciliationReport:
    symbol: str
    items: tuple[ReconciliationItem, ...] = field(default_factory=tuple)

    @property
    def checked_items(self) -> tuple[ReconciliationItem, ...]:
        return tuple(i for i in self.items if i.checked)

    @property
    def failed(self) -> tuple[ReconciliationItem, ...]:
        return tuple(i for i in self.checked_items if not i.passed)

    @property
    def passed(self) -> bool:
        """**全部已检查项都通过**才算通过；一项都没查到不算通过。"""
        checked = self.checked_items
        return bool(checked) and not self.failed

    @property
    def score(self) -> float:
        return sum(i.score for i in self.items)

    @property
    def available_score(self) -> float:
        return sum(i.full_score for i in self.checked_items)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "passed": self.passed,
            "score": self.score,
            "available_score": self.available_score,
            "failed": [i.name for i in self.failed],
            "items": [
                {"name": i.name, "gap": i.gap, "passed": i.passed if i.checked else None,
                 "score": i.score, "note": i.note}
                for i in self.items
            ],
        }


def reconcile(symbol: str, *,
              net_income: float | None = None,
              other_comprehensive: float | None = None,
              delta_retained: float | None = None,
              operating_cashflow: float | None = None,
              cashflow_net_change: float | None = None,
              balance_cash_change: float | None = None,
              deducted_net_income: float | None = None,
              tiers: Sequence[tuple[float, float]] = DEFAULT_TIERS
              ) -> ReconciliationReport:
    """对单只股票做三表勾稽。缺失的输入项会记为「未检查」而不是失败。"""
    items: list[ReconciliationItem] = []

    g1 = retained_earnings_gap(net_income, other_comprehensive, delta_retained)
    items.append(ReconciliationItem(
        "retained_earnings", "留存收益勾稽", g1,
        banded_score(g1, tiers, DEFAULT_FULL_SCORES["retained_earnings"]),
        DEFAULT_FULL_SCORES["retained_earnings"],
        "|NI+OCI−ΔRE|/|NI|"))

    g2 = cash_change_gap(cashflow_net_change, balance_cash_change)
    items.append(ReconciliationItem(
        "cash_change", "现金变动勾稽", g2,
        banded_score(g2, tiers, DEFAULT_FULL_SCORES["cash_change"]),
        DEFAULT_FULL_SCORES["cash_change"],
        "|CF净变动−货币资金变动|/max(|·|)"))

    g3 = earnings_quality_gap(net_income, deducted_net_income)
    items.append(ReconciliationItem(
        "earnings_quality", "利润质量", g3,
        banded_score(g3, tiers, DEFAULT_FULL_SCORES["earnings_quality"]),
        DEFAULT_FULL_SCORES["earnings_quality"],
        "|NI−扣非|/|NI|"))

    return ReconciliationReport(symbol=symbol, items=tuple(items))
