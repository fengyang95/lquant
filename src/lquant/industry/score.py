"""行业综合评分与结论。

聚合算法与个股分析**完全共用** :func:`lquant.core.report.composite_scores`
（权重按可用角度归一化、双覆盖度上报）—— 同一个平台里「综合分怎么算」
只能有一套口径。本模块只负责行业自己的分级措辞（强势 / 弱势）与结论生成。
"""
from __future__ import annotations

from lquant.core.report import composite_scores
from lquant.industry.contract import ANGLES, grade


def composite(angles: list[dict]) -> dict:
    """角度结果列表 → 综合评分 + 结论骨架。"""
    out = composite_scores(angles, ANGLES)
    out["grade"] = grade(out["score"]) if out["score"] is not None else "无法评分"
    return out


def build_verdict(angles: list[dict], score: dict, risk: dict,
                  meta: dict) -> dict:
    """把角度结果 + 综合分 + 风险 → 结论要点与风险清单（纯规则，不含生成式文本）。

    纪律与个股分析一致：结论里的每一句话都**可追溯到具体数字**，
    不生成「行业前景广阔」这类没有出处的判断。
    """
    points: list[str] = []
    risks: list[str] = list(risk.get("flags") or [])

    if score.get("score") is None:
        points.append("可用数据不足以形成行业判断，请先补齐数据后再看结论")
    else:
        s = score["score"]
        points.append(
            f"综合 {s:.0f} 分（{score['grade']}）· "
            f"{score['n_scored']}/{score['n_angles']} 个角度有数据"
        )
        for c in score["contributions"][:2]:
            verb = "拉高" if c["contribution"] > 0 else "拉低"
            points.append(
                f"{c['label']} {c['score']:.0f} 分（权重 {c['effective_weight'] * 100:.0f}%），"
                f"{verb}综合分 {abs(c['contribution']):.1f} 分"
            )

    # 相对强度单独点出来：这是行业分析最常被追问的一个数
    rank = meta.get("rank")
    if rank:
        points.append(
            f"全行业 {rank['n_industries']} 个行业中，20 日收益排名第 "
            f"{rank['rank']}（{rank['percentile']:.0f}% 分位）"
        )

    for a in angles:
        # 行业的五个角度只要 available 就一定有分（无信号时直接判 unavailable），
        # 所以这里没有「不评分但可用」这条分支
        if a.get("available"):
            points.append(f"{a['label']}：{a['summary']}")

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

    for note in meta.get("notes") or []:
        risks.append(note)

    seen: set[str] = set()
    uniq = [r for r in risks if not (r in seen or seen.add(r))]
    return {"points": points, "risks": uniq}
