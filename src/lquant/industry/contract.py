"""行业分析报告契约：角度注册表 + 结果构造。

与 :mod:`lquant.security`（个股分析）**对称**的一份契约，回答的是另一个问题：

- 个股分析问「**这只票**现在怎么样」——输入一个 symbol；
- 行业分析问「**这个行业**现在怎么样、在全行业里排第几」——输入一个行业。

两条共用的纪律直接沿用 ``lquant.core.report`` 的原语，不另起炉灶：

1. **只报告算得出来的东西**。某个角度取不到数 → ``available=False`` + ``hint``，
   绝不用「中性 50 分」把空缺填平 —— 那会让覆盖度失去意义。
2. **评分 0–100，50 为中性**，综合分按可用角度重新归一化权重，并如实报告
   分析面覆盖度（``angle_coverage``）与数据面覆盖度（``data_coverage``）。

措辞与个股分析区分：行业说的是「强势 / 弱势」（相对全行业的位置），
不是说「偏多 / 偏空」（那是单票的多空判断）。
"""
from __future__ import annotations

from lquant.core.report import (
    BEARISH,
    BULLISH,
    NEUTRAL,
    NEUTRAL_SIGNAL,
    AngleSpec,
    json_safe,
    metric,
    percentile_rank,
    stance_of,
    to_score,
)
from lquant.core.report import num as _num

__all__ = [
    "ANGLE_BY_ID",
    "ANGLES",
    "BEARISH",
    "BULLISH",
    "NEUTRAL",
    "NEUTRAL_SIGNAL",
    "SCHEMA_VERSION",
    "AngleSpec",
    "angle",
    "grade",
    "json_safe",
    "metric",
    "percentile_rank",
    "stance_of",
    "to_score",
    "unavailable",
]

#: 报告结构版本。契约一变就 +1，前端可据此判断字段兼容性。
SCHEMA_VERSION = "1.0"

#: 参与综合评分的角度。**非零权重之和必须为 1.0**（有测试守着）。
#:
#: 权重分配的依据（行业轮动的实证共识 + 平台能拿到的数据）：
#:
#: - ``trend`` 权重最高：行业轮动（sector momentum）本身就是行业分析最主要的
#:   可交易信号，且数据最全（只要日线）；
#: - ``prosperity`` 次之：景气度是行业比较的基本面锚，但受 PIT 财务覆盖度限制
#:   （季度披露 + 部分标的缺公告日），不该盖过趋势；
#: - ``valuation`` / ``capital`` / ``breadth`` 作辅助确认：分别管「贵不贵」
#:   「钱在不在」「面宽不宽」。
ANGLES: tuple[AngleSpec, ...] = (
    AngleSpec("trend", "趋势与轮动", 0.28,
              "行业合成指数收益 / 相对基准超额 / 全行业相对强度排名"),
    AngleSpec("prosperity", "景气度", 0.24,
              "成分股 PIT 财务聚合：营收与净利同比、ROE 及环比动能"),
    AngleSpec("valuation", "估值", 0.18,
              "行业 PE / PB 中位数的自身历史分位与全市场横向分位"),
    AngleSpec("capital", "资金与拥挤度", 0.16,
              "主力净流入 / 成交额占比及其历史分位（拥挤度反向计分）"),
    AngleSpec("breadth", "宽度与情绪", 0.14,
              "上涨家数占比 / 涨停家数 / 创 60 日新高占比 / 分化度"),
)

ANGLE_BY_ID: dict[str, AngleSpec] = {a.id: a for a in ANGLES}

#: 分级阈值：综合分 → 文字结论（行业口径：**强势 / 弱势**，不是多空）
_GRADES: tuple[tuple[float, str], ...] = (
    (75.0, "显著强势"),
    (60.0, "强势"),
    (55.0, "中性偏强"),
    (45.0, "中性"),
    (40.0, "中性偏弱"),
    (25.0, "弱势"),
    (0.0, "显著弱势"),
)


def grade(score: float) -> str:
    """综合分 → 中文结论。"""
    for lo, label in _GRADES:
        if score >= lo:
            return label
    return _GRADES[-1][1]


def angle(
    angle_id: str,
    *,
    available: bool,
    summary: str,
    metrics: list[dict] | None = None,
    score: float | None = None,
    coverage: float = 0.0,
    hint: str | None = None,
    extra: dict | None = None,
) -> dict:
    """构造一个角度结果。

    ``summary`` 无论可用与否都必须有 —— 不可用时它就是「为什么没有」的说明。
    """
    spec = ANGLE_BY_ID[angle_id]
    return {
        "id": spec.id,
        "label": spec.label,
        "weight": spec.weight,
        "desc": spec.desc,
        "available": bool(available),
        "score": _num(score),
        "stance": stance_of(score),
        "coverage": round(float(coverage), 4),
        "summary": summary,
        "metrics": metrics or [],
        "hint": hint,
        "extra": extra or {},
    }


def unavailable(angle_id: str, hint: str, *, summary: str | None = None) -> dict:
    """取不到数时的角度占位（保留 label/weight，前端布局不塌）。"""
    return angle(angle_id, available=False, summary=summary or hint, hint=hint)
