"""复权一致性对账：复权价必须能被「原始价 × 复权因子」解释。

除权日价格跳变有两种合法解释：分红送转（adj_factor 同步跳变）或数据错误。
两者区分不开的复权价不能进因子计算 —— 宁可打标。
"""
from __future__ import annotations

import polars as pl

from lquant.data.quality.flags import ADJ_ANOMALY, hit, or_flags
from lquant.data.quality.issues import Issue

__all__ = ["check_adjustment"]

# 单日 |ret| 超过此值且 adj_factor 未变 → 视为复权异常候选（A 股涨跌停 ±10%/20%）
RET_LIMIT = 0.21

# 因子跳变判定容差：低于此值视为因子未变
_FACTOR_TOL = 1e-9


def check_adjustment(df: pl.DataFrame,
                     *,
                     ret_limit: float = RET_LIMIT,
                     factor_tol: float = _FACTOR_TOL) -> tuple[pl.DataFrame, list[Issue]]:
    """复权一致性检查：打标 ADJ_ANOMALY 并聚合一条 Issue。

    有 adj_factor 列时，价格跳变必须能被因子跳变解释：
    - 因子跳变日：复权收益（close×adj_factor 的收益）应仍在涨跌停带宽内
    - 因子未变日：原始收益应仍在带宽内
    无 adj_factor 列时退化为：单日 |ret| 超带宽即打标。
    """
    issues: list[Issue] = []
    if not len(df):
        return df, issues
    d = df.sort(["symbol", "trade_date"])
    ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
    if "adj_factor" in d.columns:
        af = pl.col("adj_factor")
        af_jump = (af / af.shift(1).over("symbol") - 1.0).abs()
        # 合法除权：价格跳变幅度 ≈ 因子跳变幅度（复权收益 ≈ 0 带宽内）
        adj_ret = ((pl.col("close") * af)
                   / (pl.col("close") * af).shift(1).over("symbol") - 1.0)
        bad = ((af_jump > factor_tol) & (adj_ret.abs() > ret_limit)) | \
              ((af_jump <= factor_tol) & (ret.abs() > ret_limit))
    else:
        bad = ret.abs() > ret_limit
    out = or_flags(d, hit(ADJ_ANOMALY, bad.fill_null(False)))
    bad_rows = out.filter(pl.col("quality_flags") & ADJ_ANOMALY != 0)
    if len(bad_rows):
        symbols = bad_rows["symbol"].unique().to_list()
        issues.append(Issue(
            rule="ADJ_ANOMALY",
            severity="warn",
            detail=f"{len(bad_rows)} 行复权收益与复权因子不一致"
                   f"（超过带宽 {ret_limit:.0%}），疑似除权未同步或复权错误",
            count=len(bad_rows),
            extra={"symbols": symbols[:20]},
        ))
    return out, issues
