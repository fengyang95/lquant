"""个股分析报告契约：角度注册表 + 结果构造 + JSON 安全。

**一条纪律：平台只报告算得出来的东西。** 任何一个角度取不到数，就返回
``available=False`` + ``hint``（告诉用户缺什么、怎么补），绝不用「中性 50 分」
或占位数字把空缺填平 —— 那会让「覆盖度」失去意义，读者无从分辨一份报告是
基于 6 个角度还是 1 个角度给出的结论。

评分口径统一为 **0–100，50 为中性**：

- ``> 50`` 偏多，``< 50`` 偏空，越远离 50 信号越强；
- 每个角度另带 ``coverage``（该角度内部有多少子项真的算出来了），
  综合分按 **可用角度** 重新归一化权重，并如实报告总覆盖度。

与 :mod:`lquant.industry`（行业分析）共用同一套**通用原语**：评分映射、
指标构造、分位口径、NaN/Inf 清洗都在 :mod:`lquant.core.report`，本模块把
它们 re-export，老调用方 import 路径不变。这里只留个股分析**自己的业务语义**：
角度注册表与权重、「偏多/偏空」的分级措辞。
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
#: ``news`` 权重刻意给 0：只有新闻条数、没有情感模型时，把「新闻多」折算成
#: 多空分就是编造信号。它仍然出现在报告里（读者需要知道有没有消息面扰动），
#: 但 ``score=None`` 不参与综合分，权重按可用角度重新归一。
ANGLES: tuple[AngleSpec, ...] = (
    AngleSpec("technical", "技术面", 0.24, "趋势 / 动量 / 波动 / 量能"),
    AngleSpec("fundamental", "基本面", 0.26, "盈利 / 成长 / 偿债（行业相对分位 · PIT）"),
    AngleSpec("valuation", "估值", 0.20, "PE / PB / PS / 股息率 的历史与行业分位"),
    AngleSpec("capital", "资金面", 0.15, "主力 / 超大单净流入"),
    AngleSpec("relative", "行业与相对强度", 0.15, "相对基准超额 · 行业中地位"),
    AngleSpec("news", "消息面", 0.00, "近期新闻热度（仅信息，不参与评分）"),
)

ANGLE_BY_ID: dict[str, AngleSpec] = {a.id: a for a in ANGLES}

#: 分级阈值：综合分 → 文字结论
_GRADES: tuple[tuple[float, str], ...] = (
    (75.0, "显著偏多"),
    (60.0, "偏多"),
    (55.0, "中性偏多"),
    (45.0, "中性"),
    (40.0, "中性偏空"),
    (25.0, "偏空"),
    (0.0, "显著偏空"),
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
