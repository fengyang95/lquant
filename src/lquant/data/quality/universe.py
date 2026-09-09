"""时点股票池（PIT universe）检查：抓幸存者偏差。

回测/因子计算用的合并宽表里，股票池随时间「越用越小」通常是幸存者偏差：
退市股的历史行被清洗掉了，收益率截面只剩活股票。这类问题无法从单日
数据看出，必须看「每日股票数序列」的形状 —— 骤降即风险信号。
"""
from __future__ import annotations

import polars as pl

__all__ = ["check_point_in_time"]

# 每日股票数低于此前最大值此比例 → 判定骤降（幸存者偏差风险）
_DROP_RATIO = 0.9


def check_point_in_time(df: pl.DataFrame, *, date_col: str = "trade_date",
                        symbol_col: str = "symbol") -> dict:
    """检查股票池的时点一致性，返回

    {"daily_counts": pl.DataFrame, "survivorship_risk": bool, "reasons": list[str]}

    - daily_counts：每日股票数序列
    - survivorship_risk：首日之后每日股票数低于此前最大值 × 0.9（骤降），
      或带 delist_date 列时存在「退市后仍出现」的行
    - reasons：人话描述的风险原因（无风险为空列表）
    """
    reasons: list[str] = []
    if not len(df):
        return {"daily_counts": pl.DataFrame(),
                "survivorship_risk": False, "reasons": reasons}
    counts = (
        df.group_by(date_col)
        .agg(pl.col(symbol_col).n_unique().alias("n_symbols"))
        .sort(date_col)
    )
    if not len(counts):
        return {"daily_counts": counts,
                "survivorship_risk": False, "reasons": reasons}
    peak = counts["n_symbols"].cum_max()
    drop_days = counts.filter(
        (pl.arange(0, pl.len()) > 0)
        & (pl.col("n_symbols") < peak * _DROP_RATIO)
    )
    if len(drop_days):
        reasons.append(
            f"每日股票数骤降：{drop_days[date_col].head(3).to_list()} 等日"
            f"不足此前最大值（{counts['n_symbols'].max()}）的 {_DROP_RATIO:.0%}，"
            f"疑似退市股历史行被清洗（幸存者偏差）"
        )
    if "delist_date" in df.columns:
        dead = df.filter(
            pl.col("delist_date").is_not_null()
            & (pl.col(date_col) > pl.col("delist_date"))
        )
        if len(dead):
            reasons.append(
                f"delist_date 检查：退市后仍出现 {len(dead)} 行（涉及 "
                f"{dead[symbol_col].n_unique()} 只标的）：疑似退市日写错或"
                f"池内混入已退市标的"
            )
    return {
        "daily_counts": counts,
        "survivorship_risk": bool(reasons),
        "reasons": reasons,
    }
