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

from lquant.security.contract import ANGLES, NEUTRAL, grade, stance_of


def composite(angles: list[dict]) -> dict:
    """角度结果列表 → 综合评分 + 结论骨架。"""
    scored = [a for a in angles
              if a.get("score") is not None and (a.get("weight") or 0) > 0]

    total_weight = sum(a.weight for a in ANGLES if a.weight > 0)
    scored_weight = sum(a["weight"] for a in scored)

    if not scored or scored_weight <= 0:
        return {
            "score": None,
            "grade": "无法评分",
            "stance": None,
            "n_scored": 0,
            "n_angles": len(ANGLES),
            "angle_coverage": 0.0,
            "data_coverage": 0.0,
            "weights": {},
            "contributions": [],
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
    # 内部数据覆盖度：按归一化权重加权（只统计参与评分的角度）
    data_cov = sum(
        (a["weight"] / scored_weight) * float(a.get("coverage") or 0.0) for a in scored
    )
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    return {
        "score": final,
        "grade": grade(final),
        "stance": stance_of(final, band=2.0),
        "n_scored": len(scored),
        "n_angles": len(ANGLES),
        "angle_coverage": round(scored_weight / total_weight, 4) if total_weight else 0.0,
        "data_coverage": round(data_cov, 4),
        "weights": {a.id: a.weight for a in ANGLES if a.weight > 0},
        "contributions": contributions,
        "unscored": [a["id"] for a in angles
                     if a.get("score") is None or (a.get("weight") or 0) <= 0],
    }


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
