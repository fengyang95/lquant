"""个股分析报告契约：角度注册表 + 结果构造 + JSON 安全。

**一条纪律：平台只报告算得出来的东西。** 任何一个角度取不到数，就返回
``available=False`` + ``hint``（告诉用户缺什么、怎么补），绝不用「中性 50 分」
或占位数字把空缺填平 —— 那会让「覆盖度」失去意义，读者无从分辨一份报告是
基于 6 个角度还是 1 个角度给出的结论。

评分口径统一为 **0–100，50 为中性**：

- ``> 50`` 偏多，``< 50`` 偏空，越远离 50 信号越强；
- 每个角度另带 ``coverage``（该角度内部有多少子项真的算出来了），
  综合分按 **可用角度** 重新归一化权重，并如实报告总覆盖度。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: 报告结构版本。契约一变就 +1，前端可据此判断字段兼容性。
SCHEMA_VERSION = "1.0"

#: 中性分。所有角度的 score 都以此为原点。
NEUTRAL = 50.0


@dataclass(frozen=True)
class AngleSpec:
    """一个分析角度的元信息。"""

    id: str
    label: str
    weight: float
    desc: str


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

#: 信号方向词
BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL_SIGNAL = "neutral"

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


def stance_of(score: float | None, *, band: float = 5.0) -> str | None:
    """分数 → 方向标签；``band`` 以内算中性。"""
    if score is None:
        return None
    if score > NEUTRAL + band:
        return BULLISH
    if score < NEUTRAL - band:
        return BEARISH
    return NEUTRAL_SIGNAL


def to_score(signals: list[tuple[float, float]]) -> float | None:
    """``[(方向, 权重)]`` → 0–100 分。

    方向取值 -1（空）~ +1（多）；按权重取加权均值后线性映射到 50±50。
    没有任何信号时返回 ``None``（**不是 50** —— 无数据 ≠ 中性）。
    """
    total_w = sum(w for _, w in signals if w > 0)
    if total_w <= 0:
        return None
    mean = sum(d * w for d, w in signals if w > 0) / total_w
    mean = max(-1.0, min(1.0, mean))
    return round(NEUTRAL + NEUTRAL * mean, 2)


def metric(
    key: str,
    label: str,
    value: Any = None,
    *,
    display: str | None = None,
    unit: str | None = None,
    percentile: float | None = None,
    signal: str | None = None,
    note: str | None = None,
) -> dict:
    """构造一条指标。``value`` 为 None 表示该指标没取到（前端显示「—」）。"""
    return {
        "key": key,
        "label": label,
        "value": _num(value),
        "display": display,
        "unit": unit,
        "percentile": _num(percentile),
        "signal": signal,
        "note": note,
    }


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


def _num(v: Any) -> Any:
    """数值清洗：NaN/Inf → None，numpy 标量 → Python 标量。

    契约要求：JSON 里绝不出现 ``NaN`` / ``Infinity`` 字面量 —— 前端
    ``JSON.parse`` 会直接抛错，整个页面白屏。**不能**用 ``json.dumps``
    默认行为兜底（它默认输出 ``NaN``，是非法 JSON）。
    """
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, int):
        return v
    if hasattr(v, "item"):  # numpy / polars 标量
        try:
            return _num(v.item())
        except (ValueError, AttributeError):
            return str(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return None if (math.isnan(f) or math.isinf(f)) else f


def json_safe(obj: Any) -> Any:
    """递归清洗任意响应体（NaN/Inf → None）。"""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    return _num(obj)


def percentile_rank(values: list[float], x: float) -> float | None:
    """``x`` 在 ``values`` 中的百分位（0–100，越大越高）。

    用「小于等于 x 的占比」定义，平局不去重 —— 与 ``lquant.fundamental``
    的分位口径保持一致。样本为空或 x 非有限值时返回 None。
    """
    xs = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not xs or x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(xf) or math.isinf(xf):
        return None
    n = len(xs)
    le = sum(1 for v in xs if float(v) <= xf)
    return round(le / n * 100.0, 2)
