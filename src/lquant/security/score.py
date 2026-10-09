"""综合评分与结论。

**综合分的算法必须可解释**，因为它会被用来做决策：

1. 只有 ``score is not None`` 且 ``weight > 0`` 的角度参与；
2. 参与角度的权重**重新归一化**到和为 1（否则一个角度缺数据就会把总分
   整体拉向 0，看起来像「全票看空」）；
3. 同时报告两个覆盖度 —— 读者必须能分辨「6 个角度都算出来了」和
   「只有 2 个角度有数据」给出的 70 分不是一回事：

   - ``angle_coverage``：有分的角度占**应有权重**的比例（分析面覆盖度）
   - ``data_coverage``：有分角度的**内部数据覆盖度**加权平均（数据面覆盖度）
"""
from __future__ import annotations

from lquant.core.report import composite_scores
from lquant.security.contract import ANGLES, grade


def composite(angles: list[dict]) -> dict:
    """角度结果列表 → 综合评分 + 结论骨架。

    聚合算法（权重归一化、双覆盖度）在 :func:`lquant.core.report.composite_scores`
    —— 个股分析与行业分析共用同一份实现，避免「两套口径算同一个综合分」。
    这里只补个股自己的分级措辞。
    """
    out = composite_scores(angles, ANGLES)
    out["grade"] = grade(out["score"]) if out["score"] is not None else "无法评分"
    return out


def build_verdict(angles: list[dict], score: dict, risk: dict,
                  meta: dict) -> dict:
    """把角度结果 + 综合分 + 风险 → 结论要点与风险清单（纯规则，不含生成式文本）。"""
    points: list[str] = []
    risks: list[str] = list(risk.get("flags") or [])

    if score.get("score") is None:
        points.append("可用数据不足以形成综合判断，请先补齐数据后再看结论")
    else:
        s = score["score"]
        points.append(
            f"综合 {s:.0f} 分（{score['grade']}）· "
            f"{score['n_scored']}/{score['n_angles']} 个角度有数据"
        )
        # 贡献最大的两个角度 —— 让读者知道结论由谁主导
        for c in score["contributions"][:2]:
            verb = "拉高" if c["contribution"] > 0 else "拉低"
            points.append(
                f"{c['label']} {c['score']:.0f} 分（权重 {c['effective_weight'] * 100:.0f}%），"
                f"{verb}综合分 {abs(c['contribution']):.1f} 分"
            )

    # 分角度一句话
    for a in angles:
        if a.get("available") and a.get("score") is not None:
            points.append(f"{a['label']}：{a['summary']}")
        elif a.get("available") and a.get("score") is None:
            points.append(f"{a['label']}（不评分）：{a['summary']}")

    # 覆盖度警示：分析面覆盖不足时，结论可信度要打折
    if score.get("score") is not None:
        if score["angle_coverage"] < 0.6:
            risks.append(
                f"仅 {score['n_scored']}/{score['n_angles']} 个角度有数据"
                f"（分析面覆盖 {score['angle_coverage'] * 100:.0f}%），"
                "结论稳健性有限"
            )
        elif score["data_coverage"] < 0.5:
            risks.append(
                f"角度内部数据覆盖仅 {score['data_coverage'] * 100:.0f}%，"
                "部分子项缺失"
            )
    for a in angles:
        if not a.get("available") and a.get("hint"):
            risks.append(f"{a['label']}缺失：{a['hint']}")

    if meta.get("notes"):
        risks.extend(meta["notes"])

    # 去重但保序
    seen: set[str] = set()
    uniq = [r for r in risks if not (r in seen or seen.add(r))]
    return {"points": points, "risks": uniq}
