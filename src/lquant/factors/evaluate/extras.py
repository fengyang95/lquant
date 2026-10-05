"""报告 extras 编排：把「研报三件套 + 归因阶梯 + 分组 IC」算成报告要的内容块。

**为什么单独一个模块**：这段编排原先长在 ``server/api/factors.py`` 里，
于是只有 ``/factors/evaluate`` 一条路径拿得到这些内容块 —— CLI ``lq factor report``
与 ``/factors/synthesize`` 生成的报告永远比它薄一截（同一个生成器，三种报告，
正是评审 R12 的形态）。抽到这里之后，三个入口共用同一份实现，报告内容只由
「引擎能力」决定，不再由「谁调用的」决定。

约定：

- 返回 dict 的键就是 ``factor_report(extras=...)`` 认识的键
  （``excess`` / ``top_n`` / ``style_corr`` / ``neutral_ladder`` /
  ``neutral_views`` / ``group_ic_size``）；另有 ``group_ic``（含行业 + 市值两段）
  供 API 的 series 复用。
- 任何一段算炸了都写进 ``errors``（键名与前端故障横幅的标签表对应），
  **不抛异常、也不静默** —— 报告缺一节，读者必须能看出是「算炸了」。
- ``pre_recipe_df``：IC 归因阶梯的基线必须是**未套配方**的因子帧
  （``raw`` → ``+market_cap`` → ``+industry`` → ``+turnover``）。
  调用方已经套过配方时把原始帧传进来，否则「中性化吃掉了多少 IC」无从回答。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.factors.evaluate.defaults import DEFAULT_N_GROUPS

__all__ = [
    "benchmark_and_excess",
    "build_report_extras",
    "neutral_ladder",
    "neutral_views_for",
    "size_group_ic",
    "style_block",
    "top_n_block",
]

#: Top-N 持仓收缩测试默认 N 列表（与 API 默认一致）。
DEFAULT_TOP_NS: tuple[int, ...] = (50, 100)

#: 中性化后风格相关性的 |ρ| 判定线。
DEFAULT_STYLE_THRESHOLD = 0.14

#: 归因阶梯逐级叠加的协变量。
_LADDER_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("raw", ()),
    ("+market_cap", ("market_cap",)),
    ("+industry", ("market_cap", "industry_sw1")),
    ("+turnover", ("market_cap", "industry_sw1", "turnover_1m")),
)


def _r(v, nd: int = 4):
    """非有限值统一转 None：NaN/Inf 进了 JSON 就是非法响应。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _note(errors: dict | None, key: str, e: BaseException) -> None:
    if errors is not None:
        errors[key] = f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# IC 归因阶梯（方案 §5.3 的「页面核心」）
# --------------------------------------------------------------------------- #
def neutral_ladder(d: pl.DataFrame, col: str, ret_col: str, *,
                   dd: pl.DataFrame | None = None,
                   cov_report: dict | None = None,
                   errors: dict | None = None,
                   date_col: str = "trade_date") -> list[dict]:
    """逐段叠加协变量看 IC 怎么掉：原始 → +市值 → +行业 → +换手率。

    ``dd`` / ``cov_report`` 可由调用方传入（协变量只构建一次，阶梯与视图复用）；
    ``errors`` 传入时记录失败原因 —— 「阶梯缺一段」和「这一段算不出来」必须能区分。
    """
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    out: list[dict] = []
    if dd is None:
        from lquant.factors.covariates import build_covariates

        cov_names = sorted({c for _, covs in _LADDER_LEVELS for c in covs})
        try:
            dd, report = build_covariates(d, cov_names)
        except Exception as e:  # noqa: BLE001
            _note(errors, "neutral_ladder:covariates", e)
            return []
        cov_report = {r["covariate"]: r["coverage"] for r in report}
    cov_report = cov_report or {}

    for label, covs in _LADDER_LEVELS:
        steps = [{"op": "winsorize", "method": "mad", "n": 5},
                 {"op": "standardize", "method": "zscore"}]
        if covs:
            cols = [f"cov_{c}" for c in covs if f"cov_{c}" in dd.columns]
            if not cols:
                _note(errors, f"neutral_ladder:{label}",
                      RuntimeError("协变量列全缺失"))
                continue
            steps.append({"op": "neutralize", "method": "ols", "factors": cols})
        try:
            r = pipeline_run(dd, col, steps)
        except Exception as e:  # noqa: BLE001
            _note(errors, f"neutral_ladder:{label}", e)
            continue
        r = drop_nonfinite(r, col)
        s = ic_series(r, col, ret_col, date_col=date_col)
        if not len(s):
            continue
        out.append({
            "label": label, "covs": list(covs),
            "ic_mean": _r(s["ic"].mean()),
            "rank_ic_mean": _r(s["rank_ic"].mean()),
            "n_days": len(s),
            "coverage": round(min((cov_report.get(c, 1.0) for c in covs), default=1.0), 4),
        })
    return out


def neutral_views_for(d: pl.DataFrame, ret_col: str, *,
                      factor: str = "_factor",
                      n_groups: int = DEFAULT_N_GROUPS,
                      errors: dict | None = None) -> dict:
    """收益中性化对照 + 行业内分组分层（方案 §5.1 三视图）。

    ``factor`` 是帧里因子列的名字（API 用 ``_factor``，CLI 用 ``f``）；
    ``n_groups`` 必须透传用户选择 —— 曾经恒用默认 5，与页面上的分层组数不一致。
    """
    from lquant.factors.evaluate.neutral_views import neutral_views as _nv

    try:
        cov_cols = [c for c in d.columns if c.startswith("cov_")]
        if not cov_cols:
            return {"view": "raw（未中性化 —— 协变量数据不可用）"}
        return _nv(d, factor, ret_col, covariates=cov_cols,
                   group_col="cov_industry_sw1" if "cov_industry_sw1" in d.columns else None,
                   n_groups=n_groups)
    except Exception as e:  # noqa: BLE001
        # 不能静默成 {}：页面会把「算炸了」显示成「协变量数据不足」，
        # 两种情况的处置完全不同（一个是修数据，一个是修代码）
        _note(errors, "neutral_views", e)
        return {"view": "error", "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 分组 IC：行业组 + 市值组
# --------------------------------------------------------------------------- #
def size_group_ic(d: pl.DataFrame, factor: str, ret_col: str, *,
                  date_col: str = "trade_date",
                  errors: dict | None = None) -> dict:
    """按市值分 3 组算 IC：识破「信号只来自小市值」。

    返回 ``{"size_col": str|None, "rows": [...]}``。
    """
    from lquant.factors.evaluate.group_ic import ic_by_group, size_group

    col = next((c for c in ("cov_market_cap", "float_mv", "amount")
                if c in d.columns), None)
    if not col:
        return {"size_col": None, "rows": []}
    try:
        ds = size_group(d, mcap_col=col, n_groups=3)
        gs = ic_by_group(ds, factor, ret_col, "size_q", date_col=date_col)
    except Exception as e:  # noqa: BLE001
        _note(errors, "group_ic", e)
        return {"size_col": col, "rows": [], "error": f"{type(e).__name__}: {e}"}
    return {
        "size_col": col,
        "rows": [{"group": f"size_q{int(r['group'])}", "ic_mean": _r(r["ic_mean"]),
                  "rank_ic_mean": _r(r["rank_ic_mean"]), "ir": _r(r["ir"], 3),
                  "n_days": int(r["n_days"])} for r in gs.to_dicts()] if len(gs) else [],
    }


def industry_group_ic(d: pl.DataFrame, factor: str, ret_col: str, group_col: str, *,
                      date_col: str = "trade_date") -> list[dict]:
    """按分类维度（行业）分组算 IC。"""
    from lquant.factors.evaluate.group_ic import ic_by_group

    gi = ic_by_group(d, factor, ret_col, group_col, date_col=date_col)
    if not len(gi):
        return []
    return [{"group": str(r["group"]), "ic_mean": _r(r["ic_mean"]),
             "rank_ic_mean": _r(r["rank_ic_mean"]), "ir": _r(r["ir"], 3),
             "n_days": int(r["n_days"])} for r in gi.to_dicts()]


# --------------------------------------------------------------------------- #
# 超额收益体系 / Top-N 收缩 / 风格相关性（研报标准三件套）
# --------------------------------------------------------------------------- #
def benchmark_and_excess(d: pl.DataFrame, factor: str, ret_col: str, n_groups: int, *,
                         date_col: str = "trade_date") -> tuple[pl.DataFrame, dict]:
    """基准序列 + 分组超额净值/绩效。返回 ``(bench, excess_block)``。

    ``bench`` 还要喂给 Top-N，所以一并返回，避免重复构造。
    """
    from lquant.factors.evaluate.excess import (
        benchmark_series,
        group_excess_summary,
        quantile_excess_nav,
    )

    bench = benchmark_series(d, ret_col, date_col=date_col)
    exnav = quantile_excess_nav(d, factor, ret_col, n_groups, bench, date_col=date_col)
    dates = [str(x) for x in exnav["trade_date"].to_list()] if len(exnav) else []
    curves = {c: [_r(v, 4) for v in exnav[c].to_list()]
              for c in exnav.columns if c != "trade_date"} if len(exnav) else {}
    gex = group_excess_summary(d, factor, ret_col, n_groups, bench, date_col=date_col)
    # 最高组（q=N）相对基准的绩效 —— 研报 G0 组口径
    metrics: dict = {}
    if len(gex):
        r0 = gex.filter(pl.col("q") == n_groups)
        if len(r0):
            row = r0.to_dicts()[0]
            metrics = {"annual_excess": _r(row["annual_excess"]),
                       "excess_sharpe": _r(row["excess_sharpe"], 2),
                       "excess_mdd": _r(row["excess_mdd"])}
    return bench, {"dates": dates, "curves": curves,
                   "benchmark": "股票池等权", "metrics": metrics}


def top_n_block(d: pl.DataFrame, factor: str, ret_col: str, bench: pl.DataFrame | None,
                *, n_list=None, date_col: str = "trade_date",
                errors: dict | None = None) -> list[dict]:
    """Top-N 持仓收缩：集中到前 N 只后收益/超额/换手怎么变。"""
    from lquant.factors.evaluate.top_n import top_n_summary

    try:
        tn = top_n_summary(d, factor, ret_col,
                           n_list=list(n_list or DEFAULT_TOP_NS), bench=bench,
                           date_col=date_col)
    except Exception as e:  # noqa: BLE001
        _note(errors, "top_n", e)
        return []
    return [{"n": int(r["n"]), "annual_return": _r(r["annual_return"]),
             "annual_excess": _r(r["annual_excess"]),
             "excess_sharpe": _r(r["excess_sharpe"], 2),
             "max_drawdown": _r(r["max_drawdown"]),
             "annual_turnover": _r(r["annual_turnover"], 2)}
            for r in tn.to_dicts()] if len(tn) else []


def style_block(d: pl.DataFrame, factor: str, *, threshold: float = DEFAULT_STYLE_THRESHOLD,
                cov_cap: str = "cov_market_cap", cov_ind: str = "cov_industry_sw1",
                date_col: str = "trade_date",
                errors: dict | None = None) -> dict:
    """中性化后再体检一次风格相关性：去极值 → 标准化 → 市值/行业中性化。"""
    from lquant.factors.evaluate.style_corr import style_correlation
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    try:
        steps = [{"op": "winsorize", "method": "mad", "n": 5},
                 {"op": "standardize", "method": "zscore"}]
        cap_ind = [c for c in (cov_cap, cov_ind) if c in d.columns]
        if cap_ind:
            steps.append({"op": "neutralize", "method": "ols", "factors": cap_ind})
        rn = pipeline_run(d, factor, steps)
        rn = drop_nonfinite(rn, factor)
        style_cols = [c for c in ("cov_market_cap", "cov_turnover_1m", "cov_momentum_1m")
                      if c in rn.columns]
        return style_correlation(
            rn, factor, style_cols,
            group_col=cov_ind if cov_ind in rn.columns else None,
            threshold=threshold, date_col=date_col)
    except Exception as e:  # noqa: BLE001
        _note(errors, "style_corr", e)
        return {}


# --------------------------------------------------------------------------- #
# 一次算齐
# --------------------------------------------------------------------------- #
def build_report_extras(d: pl.DataFrame, factor: str, ret_col: str, *,
                        n_groups: int = DEFAULT_N_GROUPS,
                        top_ns=None, style_threshold: float = DEFAULT_STYLE_THRESHOLD,
                        pre_recipe_df: pl.DataFrame | None = None,
                        cov_report: dict | None = None,
                        group_col: str | None = None,
                        errors: dict | None = None,
                        date_col: str = "trade_date") -> dict:
    """算齐报告的 extras。``errors`` 里会记下每一段的失败原因。

    ``group_col`` 是「分类维度」的**调用方决定**：只有调用方知道该列有没有覆盖率
    （覆盖率 0 的协变量列虽然存在，但全 null，拿它分组等于又一种空输出）。
    缺省时才按列名回退推断。
    """
    ladder = neutral_ladder(pre_recipe_df if pre_recipe_df is not None else d,
                            factor, ret_col, dd=pre_recipe_df, cov_report=cov_report,
                            errors=errors, date_col=date_col)
    views = neutral_views_for(d, ret_col, factor=factor, n_groups=n_groups,
                              errors=errors)

    industry_ic: list[dict] = []
    size_ic = size_group_ic(d, factor, ret_col, date_col=date_col, errors=errors)
    if group_col is None:
        group_col = next((c for c in ("cov_industry_sw1", "industry_sw1")
                          if c in d.columns), None)
    if group_col and group_col in d.columns:
        try:
            industry_ic = industry_group_ic(d, factor, ret_col, group_col,
                                            date_col=date_col)
        except Exception as e:  # noqa: BLE001
            _note(errors, "group_ic", e)

    bench, excess = benchmark_and_excess(d, factor, ret_col, n_groups,
                                         date_col=date_col)
    top_n = top_n_block(d, factor, ret_col, bench, n_list=top_ns,
                        date_col=date_col, errors=errors)
    style = style_block(d, factor, threshold=style_threshold,
                        date_col=date_col, errors=errors)

    if not ladder and errors is not None:
        errors.setdefault("neutral_ladder", "归因阶梯为空（协变量不可用或 IC 序列不足）")

    return {
        "excess": excess,
        "top_n": top_n,
        "style_corr": style,
        "neutral_ladder": ladder,
        "neutral_views": views,
        "group_ic_size": {"size_col": size_ic.get("size_col"),
                          "rows": size_ic.get("rows") or []},
        # 供 API series 复用的明细（报告不直接用）
        "group_ic": {"by": group_col, "industry": industry_ic,
                     "size": size_ic.get("rows") or [],
                     "size_col": size_ic.get("size_col"),
                     "error": size_ic.get("error")},
    }
