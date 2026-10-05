"""样本过滤：ST / 停牌等可选剔除。

**默认全部关闭。** 打开任何一个都会改变 IC / 分层 / 换手数字，所以它必须是
显式的，而且报告里要写清楚「这一次到底剔没剔」—— 数据湖里有 ``is_st`` /
``is_suspended`` 两列（``data/schema.py``），但因子评价链路此前一次都没引用过，
既没剔除也没声明。

本模块只做两件事：

- :func:`apply_sample_filters` 按开关真正剔除；
- :func:`describe_sample_filters` 逐项说明实际状态，供报告披露 —— 包括
  「数据里没有这一列」这种「想剔也剔不了」的情况。
"""
from __future__ import annotations

import polars as pl

__all__ = ["FILTER_KEYS", "apply_sample_filters", "describe_sample_filters"]

FILTER_KEYS = ("exclude_st", "exclude_suspended")

_LABELS = {
    "exclude_st": "剔除 ST/*ST",
    "exclude_suspended": "剔除停牌",
}

_COLUMNS = {
    "exclude_st": "is_st",
    "exclude_suspended": "is_suspended",
}

# describe 的 status 取值：
#   applied                  已开启且列存在 → 真的剔了
#   unavailable              已开启但数据里没这一列 → 想剔也剔不了（必须让人看见）
#   available_not_applied    列存在但未开启 → 本次口径包含它们
#   not_applied              未开启且列不存在
STATUS_APPLIED = "applied"
STATUS_UNAVAILABLE = "unavailable"
STATUS_AVAILABLE_NOT_APPLIED = "available_not_applied"
STATUS_NOT_APPLIED = "not_applied"


def apply_sample_filters(df: pl.DataFrame, *, exclude_st: bool = False,
                         exclude_suspended: bool = False,
                         st_col: str = "is_st",
                         suspended_col: str = "is_suspended") -> pl.DataFrame:
    """按开关剔除样本。

    列不存在时该开关不生效（不报错）—— 由 :func:`describe_sample_filters`
    把「没剔成」这件事披露出去，而不是在这里静默假装剔过。
    """
    cols = {"exclude_st": st_col, "exclude_suspended": suspended_col}
    flags = {"exclude_st": exclude_st, "exclude_suspended": exclude_suspended}
    out = df
    for key in FILTER_KEYS:
        col = cols[key]
        if flags[key] and col in out.columns:
            out = out.filter(~pl.col(col).fill_null(False))
    return out


def describe_sample_filters(df: pl.DataFrame, *, exclude_st: bool = False,
                            exclude_suspended: bool = False,
                            st_col: str = "is_st",
                            suspended_col: str = "is_suspended") -> list[dict]:
    """逐项说明过滤器的实际状态，供报告 / API 披露。"""
    cols = {"exclude_st": st_col, "exclude_suspended": suspended_col}
    flags = {"exclude_st": exclude_st, "exclude_suspended": exclude_suspended}
    out: list[dict] = []
    for key in FILTER_KEYS:
        col = cols[key]
        available = col in df.columns
        enabled = bool(flags[key])
        if enabled and available:
            status = STATUS_APPLIED
        elif enabled and not available:
            status = STATUS_UNAVAILABLE
        elif available:
            status = STATUS_AVAILABLE_NOT_APPLIED
        else:
            status = STATUS_NOT_APPLIED
        out.append({
            "key": key,
            "label": _LABELS[key],
            "column": col,
            "available": available,
            "enabled": enabled,
            "status": status,
        })
    return out
