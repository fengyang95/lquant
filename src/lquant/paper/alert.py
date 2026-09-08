"""偏差告警：模拟盘 vs 回测对拍。

核心思想：同一段历史，回测和模拟盘回放应该给出**接近**的结果。
偏差大 = 模拟盘执行层有 bug，或策略对执行细节（滑点/部分成交/拒单）
过度敏感 —— 这正是实盘前必须发现的。

偏差容忍度不是拍脑袋：日频策略 NAV 偏差 1% 以内正常；
越接近实盘频率（分钟/逐笔），容忍度越低、要求越严。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass
class DeviationReport:
    max_nav_dev: float          # |模拟盘 NAV - 回测 NAV| 最大值
    mean_nav_dev: float
    corr: float                 # 两条净值曲线相关性
    n_mismatch_days: int        # 偏差超过阈值的日期数
    verdict: str                # ok / warning / critical
    detail: str

    def as_dict(self) -> dict:
        return {"max_nav_dev": round(self.max_nav_dev, 6),
                "mean_nav_dev": round(self.mean_nav_dev, 6),
                "corr": round(self.corr, 4) if np.isfinite(self.corr) else None,
                "n_mismatch_days": self.n_mismatch_days,
                "verdict": self.verdict,
                "detail": self.detail}


def compare_nav(backtest_nav: pl.DataFrame, paper_nav: pl.DataFrame,
                tol_daily: float = 0.01) -> DeviationReport:
    """对拍两条净值曲线。

    backtest_nav / paper_nav: DataFrame[trade_date, nav]
    tol_daily: 日频 NAV 相对偏差容忍度（默认 1%）
    """
    if not len(backtest_nav) or not len(paper_nav):
        return DeviationReport(0, 0, float("nan"), 0, "critical", "任一侧净值为空，无法对拍")

    a = backtest_nav.select(pl.col("trade_date").cast(pl.Utf8).alias("d"), pl.col("nav").alias("bt"))
    b = paper_nav.select(pl.col("trade_date").cast(pl.Utf8).alias("d"), pl.col("nav").alias("pp"))
    j = a.join(b, on="d", how="inner").sort("d")
    if not len(j):
        return DeviationReport(0, 0, float("nan"), 0, "critical",
                               f"日期无交集: bt[{a['d'][0]}~{a['d'][-1]}] vs paper[{b['d'][0]}~{b['d'][-1]}]")

    rel = ((j["pp"] - j["bt"]).abs() / j["bt"].abs().clip(1e-9))
    max_dev, mean_dev = float(rel.max()), float(rel.mean())
    bad = int((rel > tol_daily).sum())

    if len(j) > 2:
        corr = float(np.corrcoef(j["bt"], j["pp"])[0, 1])
    else:
        corr = float("nan")

    if max_dev <= tol_daily:
        verdict, detail = "ok", f"最大偏差 {max_dev:.2%} 在容忍度内"
    elif max_dev <= tol_daily * 5:
        verdict, detail = "warning", f"最大偏差 {max_dev:.2%} 偏高，{bad} 天超阈 —— 检查滑点/费率假设"
    else:
        verdict, detail = "critical", f"最大偏差 {max_dev:.2%} 严重超阈，{bad} 天超阈 —— 模拟盘执行层可能有 bug"

    return DeviationReport(max_dev, mean_dev, corr, bad, verdict, detail)


def compare_trades(bt_orders: pl.DataFrame, paper_orders: pl.DataFrame) -> dict:
    """对比两侧成交明细：同一天同一标的是否同方向、数量差多少。

    用于定位「为什么 NAV 偏差大」——是执行价差还是信号分歧。
    """
    def norm(df: pl.DataFrame) -> pl.DataFrame:
        if not len(df):
            return pl.DataFrame(schema={"d": pl.Utf8, "symbol": pl.Utf8, "side": pl.Utf8, "qty": pl.Int64})
        return (df.with_columns(pl.col("ts").cast(pl.Utf8).str.slice(0, 10).alias("d"))
                  .group_by(["d", "symbol", "side"]).agg(pl.col("qty").sum().alias("qty")))

    a, b = norm(bt_orders), norm(paper_orders)
    j = a.join(b, on=["d", "symbol", "side"], how="full", suffix="_pp",
               coalesce=True)
    n_a, n_b = len(a), len(b)
    both = j.filter(pl.col("qty").is_not_null() & pl.col("qty_pp").is_not_null()).height
    only_bt = j.filter(pl.col("qty").is_not_null() & pl.col("qty_pp").is_null()).height
    only_pp = j.filter(pl.col("qty").is_null() & pl.col("qty_pp").is_not_null()).height
    return {"bt_trades": n_a, "paper_trades": n_b,
            "matched": both, "only_backtest": only_bt, "only_paper": only_pp,
            "match_rate": round(both / max(both + only_bt + only_pp, 1), 4)}
