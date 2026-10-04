"""截面快照 ``trace``：给定「日期 + 方向 + 分箱」回看那一期的成分与收益明细。

回答的问题：**某天净值跳变，是哪几只票造成的？** 分层回测只给一条曲线，
HTML 报告只给汇总表 —— 都回不到「那一天那一箱里到底有谁、各贡献多少」。

借 AlphaPurify ``FactorAnalyzer.trace(period, date, bins, position)`` 的语义
（原生重实现，不引其运行时）。

## 与 ``quantile.py`` 同口径是硬要求

分箱编号**必须**复用 ``quantile.add_quantile``：那里的约定是
``1 = 因子值最低``、``n_groups = 最高``，且用 ``rank`` 而不是 ``qcut``
（A 股大量重复值会让 qcut 报错或分组不均）。

如果 trace 自己再写一套分箱，就会出现「曲线上第 10 组赚钱、快照里第 10 组
却是另一批票」—— 这种不一致比没有 trace 更危险。

## 无未来函数

成分筛选**只**用 ``date`` 当日的因子值。前瞻收益本身是未来信息，那是
**评价指标的正常用法**（label 必须来自未来），但绝不能反过来用未来收益
去挑成分 —— 本模块的测试专门钉这一条。
"""
from __future__ import annotations

from datetime import date as _date

import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.evaluate.quantile import add_quantile

__all__ = ["trace_snapshot", "trace_periods"]

#: 方向 → 分箱端（1 = 因子最低）
SIDES = {"long": "top", "short": "bottom", "top": "top", "bottom": "bottom"}


def _as_date(v) -> _date:
    if isinstance(v, _date):
        return v
    try:
        return _date.fromisoformat(str(v))
    except ValueError as e:
        raise FactorError(f"日期格式非法: {v!r}（期望 YYYY-MM-DD）") from e


def trace_snapshot(
    df: pl.DataFrame,
    factor: str,
    *,
    date,
    bins: int = 10,
    side: str = "long",
    horizon: int = 1,
    by: str = "trade_date",
    symbol_col: str = "symbol",
    top: int | None = None,
) -> dict:
    """某一期某一箱的成分与收益明细。

    Parameters
    ----------
    date : 目标交易日（``YYYY-MM-DD`` 或 ``date``）。
    bins : 分箱数；编号与 ``quantile.py`` 一致（1 = 因子最低）。
    side : ``long``/``top`` = 取最高箱；``short``/``bottom`` = 取最低箱。
    horizon : 前瞻收益期数；用 ``fwd_ret_{horizon}`` 列，缺失时按
        ``close`` 自行计算（见下）。
    top : 只返回前 N 个成分（按因子值降序），用于大箱的截断展示。

    返回 JSON 安全的 dict：

    ``{date, factor, bins, side, group, horizon, n_members, n_symbols,
    members: [...], mean_factor, mean_return, total_contribution,
    weight_scheme, groups: [...]}``

    权重口径：**组内等权**（``1/n``）。等权是唯一不需要额外假设的口径，
    与 ``quantile.py::group_returns`` 的默认（``weighted=False``）一致 ——
    这样 trace 的 ``mean_return`` 才能和曲线上的组收益对上。
    """
    if factor not in df.columns:
        raise FactorError(f"因子列不存在: {factor}")
    if bins < 2:
        raise FactorError(f"bins 必须 ≥ 2，收到 {bins}")
    if side not in SIDES:
        raise FactorError(f"未知方向 {side!r}，可选: {sorted(SIDES)}")
    if horizon < 1:
        raise FactorError(f"horizon 必须 ≥ 1，收到 {horizon}")
    if by not in df.columns:
        raise FactorError(f"日期列不存在: {by}")
    if symbol_col not in df.columns:
        raise FactorError(f"标的列不存在: {symbol_col}")

    target = _as_date(date)
    ret_col = f"fwd_ret_{horizon}"
    d = df
    if ret_col not in d.columns:
        d = _attach_forward_return(d, horizon, by, symbol_col)
        if ret_col not in d.columns:
            raise FactorError(
                f"缺少收益列 {ret_col}，且无法由 close 推导（需要 close 列）")

    days = d[by].unique().sort().to_list()
    if target not in days:
        near = [str(x) for x in days if x <= target][-3:] + \
               [str(x) for x in days if x > target][:3]
        raise FactorError(
            f"{target} 不在数据里（共 {len(days)} 个交易日）。"
            f"最接近的日期: {sorted(set(near))}")

    day = d.filter(pl.col(by) == target)
    if len(day) == 0:
        raise FactorError(f"{target} 当日没有截面数据")

    tagged = add_quantile(day, factor, bins, date_col=by, out="__q")
    group = bins if SIDES[side] == "top" else 1
    members_df = tagged.filter(pl.col("__q") == group).sort(
        factor, descending=(SIDES[side] == "top"))

    n_members = len(members_df)
    weight = (1.0 / n_members) if n_members else 0.0

    rows: list[dict] = []
    for r in members_df.iter_rows(named=True):
        fv = r.get(factor)
        rv = r.get(ret_col)
        fv = float(fv) if fv is not None else None
        rv = float(rv) if rv is not None else None
        rows.append({
            "symbol": r.get(symbol_col),
            "factor": fv,
            "return": rv,
            "contribution": (rv * weight) if rv is not None else None,
        })
    if top is not None and top > 0:
        rows = rows[:top]

    # 注意：这里遍历的是原始 DataFrame 的行（键是原始列名），
    # 不是上面构造的 members 字典（键是 symbol/factor/return/contribution）。
    rets = [float(row[ret_col]) for row in members_df.iter_rows(named=True)
            if row.get(ret_col) is not None]
    facs = [float(row[factor]) for row in members_df.iter_rows(named=True)
            if row.get(factor) is not None]

    # 各组规模（便于确认分箱均匀；rank 分箱下各组应几乎等大）
    group_sizes = [
        {"group": int(g), "n": int(c)}
        for g, c in tagged.group_by("__q").agg(pl.len().alias("c"))
        .sort("__q").iter_rows()
    ]

    return {
        "date": str(target),
        "factor": factor,
        "bins": int(bins),
        "side": side,
        "group": int(group),
        "horizon": int(horizon),
        "n_members": n_members,
        "n_symbols": int(len(day)),
        "members": rows,
        "returned": len(rows),
        "mean_factor": float(sum(facs) / len(facs)) if facs else None,
        "mean_return": float(sum(rets) / len(rets)) if rets else None,
        "total_contribution": float(sum(rets) / len(rets)) if rets else None,
        "weight_scheme": "equal_within_group",
        "groups": group_sizes,
    }


def trace_periods(
    df: pl.DataFrame,
    factor: str,
    *,
    side: str = "long",
    bins: int = 10,
    horizon: int = 1,
    limit: int = 10,
    by: str = "trade_date",
    symbol_col: str = "symbol",
) -> list[dict]:
    """按组收益的绝对值挑「最值得看」的若干期，返回它们的摘要。

    净值跳变排查的入口：先看哪几期贡献最大，再对具体日期调
    :func:`trace_snapshot` 看成分。
    """
    if factor not in df.columns:
        raise FactorError(f"因子列不存在: {factor}")
    ret_col = f"fwd_ret_{horizon}"
    d = df if ret_col in df.columns else _attach_forward_return(
        df, horizon, by, symbol_col)
    if ret_col not in d.columns:
        raise FactorError(f"缺少收益列 {ret_col}（需要 close 列推导）")

    tagged = add_quantile(d, factor, bins, date_col=by, out="__q")
    group = bins if SIDES[side] == "top" else 1
    per = (tagged.filter(pl.col("__q") == group)
           .group_by(by)
           .agg([pl.col(ret_col).mean().alias("ret"), pl.len().alias("n")])
           .drop_nulls("ret")
           .sort(pl.col("ret").abs(), descending=True)
           .head(max(1, limit)))
    return [{"date": str(r[by]), "group": int(group), "return": float(r["ret"]),
             "n": int(r["n"])} for r in per.iter_rows(named=True)]


def _attach_forward_return(df: pl.DataFrame, horizon: int, by: str,
                           symbol_col: str) -> pl.DataFrame:
    """没有 fwd_ret_N 时按 ``close`` 现算（与 ``evaluate/returns.py`` 同口径）。"""
    if "close" not in df.columns:
        return df
    from lquant.factors.evaluate.returns import forward_return

    return forward_return(df, price_col="close", periods=[horizon],
                          by=symbol_col, date_col=by)
