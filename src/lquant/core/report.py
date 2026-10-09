"""分析报告契约的**通用原语**（与具体分析域无关）。

个股分析（:mod:`lquant.security`）与行业分析（:mod:`lquant.industry`）产出的是
同一种东西：一份「0–100 打分 + 分角度可解释指标」的报告。这份报告里有几条
口径**必须全平台唯一**，各写一份迟早会漂移 —— 比如「无数据返回 None 而不是
50 分中性」，一处写错就变成用假中性把空缺填平，覆盖度失去意义：

- :data:`NEUTRAL` / :func:`to_score` / :func:`stance_of` —— 方向 → 分数映射
- :func:`metric` —— 一条指标的构造（``value`` 为 None 表示「没取到」）
- :func:`percentile_rank` —— 分位口径（「小于等于 x 的占比」，平局不去重）
- :func:`num` / :func:`json_safe` —— NaN/Inf 清洗（非法 JSON 会让前端白屏）

**不放在这里的东西**：角度注册表与权重、分级措辞、结论生成 —— 那些是各分析域
自己的业务语义（个股说「偏多」，行业说「强势」），由各域的 ``contract`` 定义。

本模块是 2026-10 从 ``lquant.security.contract`` 原样抽出：行业分析要复用同一
套原语，复制粘贴等于给自己埋第二个口径。``security.contract`` 仍然 re-export
这些名字，老调用方与测试的 import 路径不变。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: 中性分。所有角度的 score 都以此为原点。
NEUTRAL = 50.0

#: 信号方向词
BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL_SIGNAL = "neutral"


@dataclass(frozen=True)
class AngleSpec:
    """一个分析角度的元信息。

    ``weight`` 是综合分的相对权重；各域自身**非零权重之和必须为 1.0**
    （有测试守着），因为综合分按可用角度重新归一化后直接用它。
    """

    id: str
    label: str
    weight: float
    desc: str


def stance_of(score: float | None, *, band: float = 5.0) -> str | None:
    """分数 → 方向标签；``band`` 以内算中性。``score`` 为 None 时返回 None。"""
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
        "value": num(value),
        "display": display,
        "unit": unit,
        "percentile": num(percentile),
        "signal": signal,
        "note": note,
    }


def num(v: Any) -> Any:
    """数值清洗：NaN/Inf → None，numpy/polars 标量 → Python 标量。

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
            return num(v.item())
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
    return num(obj)


def safe_float(v: Any) -> float | None:
    """安全转 float：None / NaN / Inf / 非数 → None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def tanh_norm(x: float | None, scale: float) -> float | None:
    """把无界量（收益、比率）压到 -1..1。``scale`` 是「算作满分」的量级。"""
    if x is None or scale <= 0:
        return None
    return math.tanh(x / scale)


def pct_text(v: float | None, digits: int = 2, suffix: str = "%") -> str | None:
    """百分比展示文案（``None`` 原样返回，前端显示「—」）。"""
    if v is None:
        return None
    return f"{v:.{digits}f}{suffix}"


def signal_of(direction: float, signal: str | None = None,
              *, band: float = 0.15) -> str:
    """方向值 → 信号标签；``signal`` 显式给定时直接用它（覆盖）。"""
    if signal:
        return signal
    if direction > band:
        return BULLISH
    if direction < -band:
        return BEARISH
    return NEUTRAL_SIGNAL


def percentile_rank(values: list[float], x: float) -> float | None:
    """``x`` 在 ``values`` 中的百分位（0–100，越大越高）。

    用「小于等于 x 的占比」定义，平局不去重 —— 与 ``lquant.fundamental``
    的分位口径保持一致。样本为空或 x 非有限值时返回 None。
    """
    xs = [v for v in values
          if v is not None and not (isinstance(v, float) and math.isnan(v))]
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


def composite_scores(angle_results: list[dict],
                     specs: tuple[AngleSpec, ...],
                     *, stance_band: float = 2.0) -> dict:
    """角度结果列表 → 综合评分骨架（个股 / 行业分析共用同一套算法）。

    **算法必须可解释**，因为它会被用来做决策：

    1. 只有 ``score is not None`` 且 ``weight > 0`` 的角度参与；
    2. 参与角度的权重**重新归一化**到和为 1（否则一个角度缺数据就会把总分
       整体拉向 0，看起来像「全票看空」）；
    3. 同时报告两个覆盖度 —— 读者必须能分辨「6 个角度都算出来了」和
       「只有 2 个角度有数据」给出的 70 分不是一回事：

       - ``angle_coverage``：有分的角度占**应有权重**的比例（分析面覆盖度）
       - ``data_coverage``：有分角度的**内部数据覆盖度**加权平均（数据面覆盖度）

    返回体不含 ``grade``（分级措辞是个股/行业各自的业务语义），由调用方补。
    """
    scored = [a for a in angle_results
              if a.get("score") is not None and (a.get("weight") or 0) > 0]

    total_weight = sum(a.weight for a in specs if a.weight > 0)
    scored_weight = sum(a["weight"] for a in scored)

    if not scored or scored_weight <= 0:
        return {
            "score": None,
            "stance": None,
            "n_scored": 0,
            "n_angles": len(specs),
            "angle_coverage": 0.0,
            "data_coverage": 0.0,
            "weights": {},
            "contributions": [],
            "unscored": [a["id"] for a in angle_results],
        }

    contributions = []
    total = 0.0
    for a in scored:
        eff = a["weight"] / scored_weight          # 归一化后的实际权重
        contrib = eff * (a["score"] - NEUTRAL)     # 相对中性的贡献
        total += contrib
        contributions.append({
            "id": a["id"],
            "label": a["label"],
            "score": a["score"],
            "stance": a["stance"],
            "weight": a["weight"],
            "effective_weight": round(eff, 4),
            "contribution": round(contrib, 2),
        })

    final = round(NEUTRAL + total, 2)
    final = max(0.0, min(100.0, final))
    data_cov = sum(
        (a["weight"] / scored_weight) * float(a.get("coverage") or 0.0) for a in scored
    )
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    return {
        "score": final,
        "stance": stance_of(final, band=stance_band),
        "n_scored": len(scored),
        "n_angles": len(specs),
        "angle_coverage": round(scored_weight / total_weight, 4) if total_weight else 0.0,
        "data_coverage": round(data_cov, 4),
        "weights": {a.id: a.weight for a in specs if a.weight > 0},
        "contributions": contributions,
        "unscored": [a["id"] for a in angle_results
                     if a.get("score") is None or (a.get("weight") or 0) <= 0],
    }
