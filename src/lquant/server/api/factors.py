"""因子定义 / 计算 / 评价 / 报告。

评价接口的约定：请求里给「因子名 + 公式」，后端用 Parquet 湖里的
日线现算因子值 → 评价 → 存 HTML 报告。这是研究态的轻量路径；
正式的批量因子计算走 CLI / 任务队列。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from lquant.core.db import reader
from lquant.data.store.catalog import upsert
from lquant.data.store.parquet import read_daily
from lquant.factors.evaluate import evaluate, forward_return, save_report
from lquant.factors.preprocess.pipeline import drop_nonfinite

router = APIRouter(prefix="/factors", tags=["factors"])

REPORT_DIR = Path("data/reports")


class FactorIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    expression: str = ""                 # DSL 表达式（可选，先存后编译）
    description: str = ""


class EvaluateIn(BaseModel):
    factor: str = "mom20"                # 报告名
    formula: str = "pct_change_20"       # 支持 pct_change_{n} / rolling_std_{n}
    n_groups: int = Field(default=5, ge=2, le=20)
    horizons: list[int] = Field(default=[1, 5, 10, 20], min_length=1, max_length=20,
                                ge=1, le=250)
    start: str = "2026-01-01"
    window: int = Field(default=60, ge=20, le=250)  # 滚动窗口（交易日）
    top_ns: list[int] = Field(default=[50, 100], min_length=1, max_length=5,
                              description="Top-N 持仓收缩测试的 N 列表")
    style_threshold: float = Field(default=0.14, ge=0.0, le=1.0,
                                   description="中性化后风格相关性阈值（max|ρ| 判定线）")


@router.get("")
def list_factors(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=500),
    source: str | None = Query(default=None, description="按来源筛选: qlib/yaml/manual"),
) -> list[dict]:
    """已注册因子（factor_def 表）。offset/limit 分页 + source 筛选。"""
    suffix = f"OFFSET {offset}" + (f" LIMIT {limit}" if limit else "")
    where = f"WHERE d.source = '{source}'" if source else ""
    order = "icn DESC NULLS LAST" if not source else "created_at DESC"
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT d.name, d.expression, d.description, d.source, d.factor_id, "
                "i.ic_neutral AS icn, d.created_at FROM factor_def d "
                "LEFT JOIN factor_ic i ON i.factor = d.name "
                f"{where} ORDER BY {order} {suffix}"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
    return [{"name": r[0], "expression": r[1], "description": r[2],
             "source": r[3], "factor_id": r[4], "ic_neutral": r[5],
             "created_at": str(r[6])}
            for r in rows]


@router.post("")
def register_factor(f: FactorIn) -> dict:
    """注册因子定义。给了 expression 就先过 DSL 语法检查。"""
    if f.expression:
        try:
            from lquant.factors.dsl.analyzer import check
            from lquant.factors.dsl.parser import parse

            check(parse(f.expression, f.name))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"DSL 校验失败: {e}") from e
    n = upsert("factor_def", pl.DataFrame([{
        "name": f.name, "expression": f.expression,
        "description": f.description, "created_at": datetime.now(),
    }]))
    return {"registered": f.name, "rows": n}


def _compute_factor(df: pl.DataFrame, formula: str) -> pl.DataFrame:
    """现算因子。优先命中 Qlib Alpha158 内置因子（白名单探测），其次研究常用形态。"""
    from lquant.factors.qlib_alpha import compute as qlib_compute
    from lquant.factors.qlib_alpha import has_factor

    if has_factor(formula):
        return qlib_compute(df, formula)
    if "$" in formula:                     # DSL 表达式 —— 统一走 FactorEngine
        from lquant.factors.analysis import compute_factor_col

        return compute_factor_col(df, formula, "_factor")
    if formula.startswith("pct_change_") and formula.rsplit("_", 1)[1].isdigit():
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change(n).over("symbol").alias("_factor"))
    if formula.startswith("rolling_std_") and formula.rsplit("_", 1)[1].isdigit():
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change().over("symbol")
                               .rolling_std(n).alias("_factor"))
    if formula == "turnover":
        return df.with_columns((pl.col("amount") / 1e8).alias("_factor"))
    raise HTTPException(422, f"暂不支持的因子公式: {formula}（内置因子见 GET /api/factors/builtin）")


def _neutral_views_for(d: pl.DataFrame, ret_col: str) -> dict:
    """收益中性化对照 + 行业内分组标注（5.1 视图）。"""
    from lquant.factors.evaluate.neutral_views import neutral_views as _nv

    try:
        cov_cols = [c for c in d.columns if c.startswith("cov_")]
        if not cov_cols:
            return {"view": "raw（未中性化 —— 协变量数据不可用）"}
        return _nv(d, "_factor", ret_col, covariates=cov_cols,
                   group_col="cov_industry_sw1" if "cov_industry_sw1" in d.columns else None)
    except Exception:  # noqa: BLE001
        return {}


def _persist_ic(name: str, ladder: list[dict]) -> None:
    """评价成功后把 IC(原始)/IC(中性化) 落 factor_ic 表 —— 列表页排序用。"""
    if not ladder:
        return
    from datetime import datetime as _dt

    from lquant.data.store.catalog import upsert

    try:
        upsert("factor_ic", pl.DataFrame([{
            "factor": name,
            "ic_raw": ladder[0]["ic_mean"],
            "ic_neutral": ladder[-1]["ic_mean"],
            "rank_ic_neutral": ladder[-1]["rank_ic_mean"],
            "n_days": ladder[-1]["n_days"],
            "updated_at": _dt.now(),
        }]))
    except Exception as e:  # noqa: BLE001
        import loguru

        loguru.logger.warning(f"factor_ic 写入失败: {e}")


def _neutral_ladder(d: pl.DataFrame, col: str, ret_col: str,
                    dd: pl.DataFrame | None = None,
                    cov_report: dict | None = None) -> list[dict]:
    """逐段叠加协变量看 IC 怎么掉：原始 → +市值 → +行业 → +换手率。

    dd/cov_report 可由调用方传入（协变量只构建一次，ladder 与 views 复用）。
    """
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    levels = [
        ("raw", []),
        ("+market_cap", ["market_cap"]),
        ("+industry", ["market_cap", "industry_sw1"]),
        ("+turnover", ["market_cap", "industry_sw1", "turnover_1m"]),
    ]
    out = []
    if dd is None:
        from lquant.factors.covariates import build_covariates

        cov_names = sorted({c for _, covs in levels for c in covs})
        try:
            dd, report = build_covariates(d, cov_names)
        except Exception:  # noqa: BLE001
            return []
        cov_report = {r["covariate"]: r["coverage"] for r in report}
    cov_report = cov_report or {}
    for label, covs in levels:
        steps = [{"op": "winsorize", "method": "mad", "n": 5},
                 {"op": "standardize", "method": "zscore"}]
        if covs:
            cols = [f"cov_{c}" for c in covs if f"cov_{c}" in dd.columns]
            if covs and not cols:
                continue
            steps.append({"op": "neutralize", "method": "ols", "factors": cols})
        try:
            r = pipeline_run(dd, col, steps)
        except Exception:  # noqa: BLE001
            continue
        r = drop_nonfinite(r, col)
        s = ic_series(r, col, ret_col)
        if not len(s):
            continue
        out.append({
            "label": label, "covs": covs,
            "ic_mean": round(float(s["ic"].mean()), 4),
            "rank_ic_mean": round(float(s["rank_ic"].mean()), 4),
            "n_days": len(s),
            "coverage": round(min((cov_report.get(c, 1.0) for c in covs), default=1.0), 4),
        })
    return out


def _evaluate_full(req: EvaluateIn) -> tuple[dict, dict]:
    """一次现算 + 评价，返回 (metrics, series)。

    /evaluate 与 /evaluate/series 共用同一份计算（缺陷 #2：原先两端点
    各自全量重算，2 倍开销且两次结果可能不一致）。
    """
    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    d = drop_nonfinite(_compute_factor(df, req.formula), "_factor")
    d = forward_return(d, "close", periods=req.horizons)
    ret_col = f"fwd_ret_{min(req.horizons)}"
    if ret_col not in d.columns:
        raise HTTPException(500, f"前瞻收益列缺失: {ret_col}")

    # ---- metrics（原 run_evaluate 计算体） ----
    res = evaluate(d, "_factor", ret_col=ret_col, n_groups=req.n_groups,
                   horizons=req.horizons)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = save_report(res["report"], REPORT_DIR / f"{req.factor}.html")
    ic = res["ic"]["ic"]
    ls = res["quantile"]["long_short"]
    metrics = {
        "factor": req.factor,
        "formula": req.formula,
        "n_samples": len(d),
        "ic": {"mean": round(ic["mean"], 4), "ir": round(ic["ir"], 3),
               "t_stat": round(ic["t_stat"], 2), "positive_rate": round(ic["positive_rate"], 4),
               "ic_gt_002_rate": round(ic["ic_gt_002_rate"], 4)},
        "rank_ic_mean": round(res["ic"]["rank_ic"]["mean"], 4),
        "long_short": {"annual_return": round(ls["annual_return"], 4),
                       "sharpe": round(ls["sharpe"], 2),
                       "max_drawdown": round(ls["max_drawdown"], 4)},
        "monotonicity": round(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "suggested_rebalance": res["decay"]["suggested_rebalance"],
        "excess": {},                    # 计算体在下方超额块完成后回填
        "annual_turnover": None,
        "top_n": [],
        "style_corr": {"max_abs": None, "passed": None},
        "report_url": f"/api/factors/reports/{report_path.stem}",
    }

    # ---- series（原 evaluate_series 计算体） ----
    import math

    import numpy as np

    from lquant.factors.evaluate import ic_by_year, ic_series, quantile_nav, quantile_summary
    from lquant.factors.evaluate.decay import decay_profile

    def _jf(v, nd=4) -> float | None:
        return round(float(v), nd) if v is not None and math.isfinite(v) else None

    s = ic_series(d, "_factor", ret_col)
    ic_dates = [str(x) for x in s["trade_date"].to_list()]
    ic_vals = [_jf(v) for v in s["ic"].to_list()]
    ic_ranks = [_jf(v) for v in s["rank_ic"].to_list()]
    cum = np.nancumsum(np.array([v if v is not None else 0.0 for v in ic_vals])) if ic_vals else []
    cum_ic = [round(float(v), 4) for v in cum]

    qnav = quantile_nav(d, "_factor", ret_col, req.n_groups)
    qdates = [str(x) for x in qnav["trade_date"].to_list()] if len(qnav) else []
    curves = {c: [_jf(v, 4) for v in qnav[c].to_list()]
              for c in qnav.columns if c != "trade_date"}

    qsum = quantile_summary(d, "_factor", ret_col, req.n_groups)
    groups = [{"q": g["q"], "annual_return": _jf(g["annual_return"]),
               "sharpe": _jf(g["sharpe"], 2), "mean_ret": _jf(g["mean_ret"], 5)}
              for g in qsum.get("groups", [])]

    prof = decay_profile(d, "_factor", req.horizons)
    decay = {"horizons": [int(h) for h in prof["horizon"].to_list()],
             "ic": [_jf(v) for v in prof["ic"].to_list()],
             "rank_ic": [_jf(v) for v in prof["rank_ic"].to_list()]}
    icy = ic_by_year(d, "_factor", ret_col)
    ic_year = [{"year": int(r["year"]), "ic_mean": _jf(r["ic_mean"]),
                "ir": _jf(r["ir"], 3), "positive_rate": _jf(r["positive_rate"], 4)}
               for r in icy.to_dicts()] if len(icy) else []

    # ---- IC 归因阶梯（方案 5.3）+ 三种中性化视图标注（方案 5.1） ----
    cov_names_all = ["market_cap", "industry_sw1", "turnover_1m", "momentum_1m"]
    try:
        with reader() as con:
            ind = con.execute("SELECT symbol, std, code, std_date FROM industry_classify").pl()
    except Exception:  # noqa: BLE001
        ind = None
    try:
        from lquant.factors.covariates import build_covariates

        d, cov_report = build_covariates(d, cov_names_all, industry_df=ind)
        cov_map = {r["covariate"]: r["coverage"] for r in cov_report}
    except Exception:  # noqa: BLE001
        d, cov_map = d, {}
    ladder = _neutral_ladder(d, "_factor", ret_col, dd=d, cov_report=cov_map)
    _persist_ic(req.factor, ladder)
    views = _neutral_views_for(d, ret_col)

    # ---- 超额收益体系 / Top-N 收缩测试 / 中性化后风格相关性（研报标准三件套） ----
    from lquant.factors.evaluate.excess import (
        benchmark_series,
        group_excess_summary,
        quantile_excess_nav,
    )

    bench = benchmark_series(d, ret_col)
    exnav = quantile_excess_nav(d, "_factor", ret_col, req.n_groups, bench)
    ex_dates = [str(x) for x in exnav["trade_date"].to_list()] if len(exnav) else []
    ex_curves = {c: [_jf(v, 4) for v in exnav[c].to_list()]
                 for c in exnav.columns if c != "trade_date"} if len(exnav) else {}
    gex = group_excess_summary(d, "_factor", ret_col, req.n_groups, bench)
    # 最高组（q=N）相对基准的绩效 —— 研报 G0 组口径
    excess_metrics = {}
    if len(gex):
        r0 = gex.filter(pl.col("q") == req.n_groups)
        if len(r0):
            row = r0.to_dicts()[0]
            excess_metrics = {"annual_excess": _jf(row["annual_excess"]),
                              "excess_sharpe": _jf(row["excess_sharpe"], 2),
                              "excess_mdd": _jf(row["excess_mdd"])}

    try:
        from lquant.factors.evaluate.top_n import top_n_summary

        tn = top_n_summary(d, "_factor", ret_col, n_list=req.top_ns, bench=bench)
        top_n_rows = [{"n": int(r["n"]), "annual_return": _jf(r["annual_return"]),
                       "annual_excess": _jf(r["annual_excess"]),
                       "excess_sharpe": _jf(r["excess_sharpe"], 2),
                       "max_drawdown": _jf(r["max_drawdown"]),
                       "annual_turnover": _jf(r["annual_turnover"], 2)}
                      for r in tn.to_dicts()] if len(tn) else []
    except Exception:  # noqa: BLE001
        top_n_rows = []

    try:
        from lquant.factors.evaluate.style_corr import style_correlation
        from lquant.factors.preprocess.pipeline import run as pipeline_run

        steps = [{"op": "winsorize", "method": "mad", "n": 5},
                 {"op": "standardize", "method": "zscore"}]
        cap_ind = [c for c in ("cov_market_cap", "cov_industry_sw1") if c in d.columns]
        if cap_ind:
            steps.append({"op": "neutralize", "method": "ols", "factors": cap_ind})
        rn = pipeline_run(d, "_factor", steps)
        rn = drop_nonfinite(rn, "_factor")
        style_cols = [c for c in ("cov_market_cap", "cov_turnover_1m", "cov_momentum_1m")
                      if c in rn.columns]
        style_corr = style_correlation(
            rn, "_factor", style_cols,
            group_col="cov_industry_sw1" if "cov_industry_sw1" in rn.columns else None,
            threshold=req.style_threshold)
    except Exception:  # noqa: BLE001
        style_corr = {}

    try:
        from lquant.factors.evaluate.costs import factor_turnover

        to_df = factor_turnover(d, "_factor", req.n_groups)
        mean_to = to_df["turnover_avg"].drop_nulls().mean() if len(to_df) else None
        annual_turnover = _jf(float(mean_to) * 252, 2) if mean_to is not None else None
    except Exception:  # noqa: BLE001
        annual_turnover = None

    # 回填 metrics（超额/TopN/风格相关在 metrics 构造后才可算，这里统一写入）
    metrics["excess"] = excess_metrics
    metrics["annual_turnover"] = annual_turnover
    metrics["top_n"] = top_n_rows
    metrics["style_corr"] = {"max_abs": style_corr.get("max_abs"),
                             "passed": style_corr.get("passed")}

    # 6) 滚动窗口 IC / RankIC / IR
    from lquant.factors.evaluate.rolling import rolling_ic
    rwin = rolling_ic(d, "_factor", ret_col, req.window)
    rolling = {
        "window": req.window,
        "dates": [str(x) for x in rwin["trade_date"].to_list()] if len(rwin) else [],
        "ic": [_jf(v) for v in rwin["ic_mean"].to_list()] if len(rwin) else [],
        "rank_ic": [_jf(v) for v in rwin["rank_ic_mean"].to_list()] if len(rwin) else [],
        "ir": [_jf(v, 3) for v in rwin["ir"].to_list()] if len(rwin) else [],
    }

    series = {
        "factor": req.factor, "formula": req.formula,
        "n_groups": req.n_groups, "n_samples": len(d),
        "ic": {"dates": ic_dates, "ic": ic_vals, "rank_ic": ic_ranks, "cum_ic": cum_ic},
        "quantile": {"dates": qdates, "curves": curves, "groups": groups,
                     "monotonicity": _jf(qsum.get("monotonicity"), 3)},
        "decay": decay, "ic_by_year": ic_year, "neutral_ladder": ladder,
        "neutral_views": views,
        "rolling": rolling,
        "excess": {"dates": ex_dates, "curves": ex_curves, "benchmark": "股票池等权"},
        "top_n": top_n_rows,
        "style_corr": style_corr,
    }
    return metrics, series


@router.post("/evaluate")
def run_evaluate(req: EvaluateIn) -> dict:
    """现算因子 → 全套评价 → 存报告。一次计算同时返回指标与图表序列。"""
    m, s = _evaluate_full(req)
    m["series"] = s
    return m


@router.post("/evaluate/series")
def evaluate_series(req: EvaluateIn) -> dict:
    """图表数据包端点（兼容别名）：内部走同一 helper，一次计算两处复用。"""
    _, s = _evaluate_full(req)
    return s


@router.post("/seed-yaml")
def seed_yaml() -> dict:
    """把 custom.yaml 因子入库（source=yaml，幂等覆盖）。"""
    from lquant.factors.sources.yaml_source import load_custom

    items = load_custom()
    if not items:
        raise HTTPException(422, "custom.yaml 无因子")
    now = datetime.now()
    n = upsert("factor_def", pl.DataFrame([{**it, "created_at": now} for it in items]))
    return {"seeded": len(items), "rows_written": n}


@router.get("/sources")
def list_factor_sources() -> list[dict]:
    """因子来源清单（M2 来源接入）。"""
    from lquant.factors.sources import list_sources

    return list_sources()


@router.get("/builtin")
def builtin_factors(
    family: str | None = Query(default=None, description="按族过滤：kbar/price/roc/ma/..."),
    q: str | None = Query(default=None, description="名称模糊搜索，如 MA"),
) -> list[dict]:
    """Qlib Alpha158 内置因子清单（158 个）。"""
    from lquant.factors.qlib_alpha import list_builtin

    items = list_builtin()
    if family:
        items = [x for x in items if x["family"] == family.lower()]
    if q:
        ql = q.upper()
        items = [x for x in items if ql in x["name"]]
    return items


class SeedBuiltinIn(BaseModel):
    families: list[str] | None = None    # None = 全部族
    names: list[str] | None = None       # 或精确指定因子名，如 ["MA20", "RSV10"]


@router.post("/seed-builtin")
def seed_builtin(req: SeedBuiltinIn) -> dict:
    """把内置因子批量登记进 factor_def（表达式存 qlib 公式，可重复调用幂等覆盖）。"""
    from lquant.factors.qlib_alpha import list_builtin

    items = list_builtin()
    if req.names:
        want = {n.upper() for n in req.names}
        items = [x for x in items if x["name"] in want]
    if req.families:
        fams = {f.lower() for f in req.families}
        items = [x for x in items if x["family"] in fams]
    if not items:
        raise HTTPException(422, "没有匹配的内置因子")
    now = datetime.now()
    from lquant.factors.sources.qlib_source import factor_id, translate

    rows = []
    for x in items:
        try:
            expr = translate(x["formula"])
        except ValueError as e:
            raise HTTPException(422, f"{x['name']} 翻译失败: {e}") from e
        rows.append({
            "name": x["name"],
            "expression": expr,          # DSL 化：单一执行语义
            "description": f"Qlib Alpha158 · {x['family']}",
            "source": "qlib",
            "source_ref": x["formula"],
            "factor_id": factor_id(x["formula"]),
            "created_at": now,
        })
    n = upsert("factor_def", pl.DataFrame(rows))
    return {"seeded": len(items), "rows_written": n,
            "sample": [x["name"] for x in items[:5]]}


@router.get("/agents")
def list_agents() -> list[dict]:
    """Agent 注册表（config/agents/*.yaml，fail-fast）。"""
    from lquant.factors.agents import load_agents

    return [a.__dict__ for a in load_agents()]


@router.get("/agents/{name}/guide")
def agent_guide(name: str) -> dict:
    """一次性接入指引（方案 6.4）：装 SKILL.md 走 CLI，lq agent test 验收。"""
    from lquant.factors.agents import find_agent

    a = find_agent(name)
    if not a:
        raise HTTPException(404, f"Agent 未注册: {name}")
    return {
        "agent": a.name, "kind": a.kind, "driver": a.driver,
        "quota_eval": a.quota_eval, "can_submit": a.can_submit,
        "steps": [
            "1. 阅读 docs/agent-skill/SKILL.md（操作手册 + 纪律）",
            "2. lq data fields —— 先看字段白名单与覆盖率",
            "3. lq factor check \"<expr>\" —— G0 静态校验，永远第一步",
            "4. lq factor eval \"<expr>\" --agent " + a.name + " —— 平台算指标",
            "5. lq factor corr \"<e1>\" \"<e2>\" —— 提交前自查相关性",
            "6. lq factor submit spec.yaml —— 唯一入库通道（服务端重验）",
        ],
        "acceptance": f"lq agent test {a.name} 必须通过",
    }


@router.get("/mine/runs")
def list_mining_runs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """挖掘会话台账（factor_mining_run）。"""
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT run_id, agent, generator, n_evaluated, n_static_fail, n_low_ic, "
                "n_redundant, n_size_proxy, n_survivors, created_at "
                "FROM factor_mining_run ORDER BY created_at DESC LIMIT ?",
                [limit]).fetchall()
        except Exception:  # noqa: BLE001
            return []
    return [{"run_id": r[0], "agent": r[1], "generator": r[2],
             "n_evaluated": r[3], "n_static_fail": r[4], "n_low_ic": r[5],
             "n_redundant": r[6], "n_size_proxy": r[7], "n_survivors": r[8],
             "created_at": str(r[9])} for r in rows]


class MineIn(BaseModel):
    agent: str = "gp-internal"
    generator: str = Field(default="gp", pattern="^(gp|random)$")
    n: int = Field(default=100, ge=1)
    sync: bool = False


def _validate_mine_req(agent_name: str, generator: str, n: int) -> None:
    """入队前的同步前置校验（404/423/422），失败给客户端明确错误。

    配额按**账本剩余**判定（与 CLI ``lq factor eval --agent`` 同一口径），
    而不是只看配置里的静态上限 —— 后者下反复调用本端点就能无限刷挖掘，
    而 agent_ledger 永远是空的。
    """
    from lquant.factors.agents import ensure_quota, find_agent

    a = find_agent(agent_name)
    if not a:
        raise HTTPException(404, f"Agent 未注册: {agent_name}")
    if not a.enabled:
        raise HTTPException(423, f"Agent 已冻结: {agent_name}")
    try:
        ensure_quota(agent_name, n)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _new_run_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _mine_placeholder(run_id: str, agent: str, generator: str) -> None:
    """入队时预落 ledger 占位行（全 0）：job 完成后 INSERT OR REPLACE 覆盖。

    这样任务中心在任务尚未跑完时也能看到这条因子挖掘任务，
    测试也能确定性断言 ledger 行存在。
    """
    import datetime as dt

    from lquant.core.db import writer

    try:
        with writer() as con:
            con.execute(
                "INSERT OR REPLACE INTO factor_mining_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, agent, generator, 0, 0, 0, 0, 0, 0, "{}", dt.datetime.now()])
    except Exception:  # noqa: BLE001
        pass


def _run_mine_job(agent_name: str, generator: str, n: int, run_id: str) -> dict:
    """挖掘会话执行体：跑会话 → 覆盖 factor_mining_run → 返回结果体。"""
    import datetime as dt
    import json

    from lquant.core.db import writer
    from lquant.factors.engine import FactorEngine
    from lquant.factors.mining.runner import run_session
    from lquant.factors.mining.submit import _panel_with_covs

    df, cov_cols = _panel_with_covs()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    eng = FactorEngine(df.lazy())
    if generator == "gp":
        from lquant.factors.mining.gp import GPGenerator

        gen = GPGenerator()
    elif generator == "random":
        from lquant.factors.mining.random_gen import make_generator

        gen = make_generator()
    else:
        raise HTTPException(422, f"未知生成器: {generator}（可选 gp/random）")
    res, survivors = run_session(eng, df, gen, agent=agent_name, n_candidates=n,
                                 covs=cov_cols)
    # 记账：与 CLI 共用同一账本，把实际评估数计入配额（此前 API 路径完全不记账）
    from lquant.factors.agents import record_eval

    record_eval(agent_name, res.n_evaluated)
    try:
        with writer() as con:
            con.execute(
                "INSERT OR REPLACE INTO factor_mining_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, agent_name, generator, res.n_evaluated, res.n_static_fail,
                 res.n_low_ic, res.n_redundant, res.n_size_proxy, res.n_survivors,
                 json.dumps(res.corrections, ensure_ascii=False)[:10000], dt.datetime.now()])
    except Exception:  # noqa: BLE001
        pass  # 记账失败不阻断结果
    return {"run_id": run_id, "agent": agent_name, "generator": generator,
            "n_evaluated": res.n_evaluated, "n_static_fail": res.n_static_fail,
            "n_low_ic": res.n_low_ic, "n_redundant": res.n_redundant,
            "n_size_proxy": res.n_size_proxy, "n_survivors": res.n_survivors,
            "survivors": survivors[:10]}


@router.post("/mine/run")
def mine_run(req: MineIn) -> dict:
    """平台驱动挖掘会话：默认异步（202 + task_id，跑完落 factor_mining_run）；
    sync=true 保留旧行为直接返回结果体。请求: {agent, generator, n, sync}。"""
    from fastapi.responses import JSONResponse

    from lquant.server.jobs import enqueue

    _validate_mine_req(req.agent, req.generator, req.n)
    if req.sync:
        return _run_mine_job(req.agent, req.generator, req.n, run_id=_new_run_id())
    run_id = _new_run_id()
    _mine_placeholder(run_id, req.agent, req.generator)
    enqueue("lquant-mining", _run_mine_job, req.agent, req.generator, req.n,
            run_id=run_id)
    return JSONResponse(status_code=202, content={
        "status": "queued", "task_id": run_id, "agent": req.agent,
        "generator": req.generator, "n": req.n})


def list_reports() -> list[dict]:
    if not REPORT_DIR.exists():
        return []
    return [{"name": p.stem, "url": f"/api/factors/reports/{p.stem}",
             "size_kb": p.stat().st_size // 1024}
            for p in sorted(REPORT_DIR.glob("*.html"), key=lambda x: -x.stat().st_mtime)]


@router.get("/reports")
def reports_list() -> list[dict]:
    return list_reports()


@router.get("/reports/{name}")
def get_report(name: str) -> FileResponse:
    # 路径白名单：name 只允许字母数字下划线连字符，挡 ../ 与隐藏字符
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise HTTPException(422, f"非法报告名: {name!r}")
    p = REPORT_DIR / f"{name}.html"
    if not p.exists():
        raise HTTPException(404, f"报告不存在: {name}")
    return FileResponse(p, media_type="text/html")


# 注意：/{name} 必须注册在 /reports* 之后，否则会吞掉 reports 路由
@router.get("/{name}")
def get_factor(name: str) -> dict:
    """因子详情：定义 + 关联报告列表。"""
    with reader() as con:
        try:
            r = con.execute(
                "SELECT name, expression, description, created_at "
                "FROM factor_def WHERE name = ?", [name]).fetchone()
        except Exception:  # noqa: BLE001
            r = None
    if not r:
        raise HTTPException(404, f"因子不存在: {name}")
    reports = []
    if REPORT_DIR.exists():
        for p in sorted(REPORT_DIR.glob(f"{name}*.html"), key=lambda x: -x.stat().st_mtime):
            reports.append({"name": p.stem, "url": f"/api/factors/reports/{p.stem}"})
    return {"name": r[0], "expression": r[1], "description": r[2],
            "created_at": str(r[3]), "reports": reports}


class AnalyzeIn(BaseModel):
    formulas: list[str] = Field(min_length=2, max_length=8)
    start: str = "2026-01-01"
    threshold: float = Field(default=0.8, ge=0.5, le=1.0)


@router.post("/analyze")
def analyze(req: AnalyzeIn) -> dict:
    """多因子相关性 / 冗余分析（F6）。冗余因子不建议同时入库。"""
    from lquant.factors.analysis import correlation

    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    try:
        return correlation(df, req.formulas, threshold=req.threshold)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


class SynthesizeIn(BaseModel):
    formulas: list[str] = Field(min_length=2, max_length=8)
    method: str = Field(default="equal", pattern="^(equal|ic_weighted)$")
    ic_horizon: int = Field(default=5, ge=1, le=20)
    n_groups: int = Field(default=5, ge=2, le=20)
    start: str = "2026-01-01"


@router.post("/synthesize")
def synthesize(req: SynthesizeIn) -> dict:
    """因子合成（F7）：等权 / IC 加权 → 全套评价 → 存报告。"""
    from lquant.factors import analysis as fa

    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    try:
        d = fa.synthesize(df, req.formulas, method=req.method,
                          ic_horizon=req.ic_horizon).drop_nulls(["_syn"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not len(d):
        raise HTTPException(422, "合成因子为空 —— 公式与数据不匹配")

    horizons = [1, 5, 10, 20]
    d = forward_return(d, "close", periods=horizons)
    res = evaluate(d, "_syn", ret_col=f"fwd_ret_{min(horizons)}",
                   n_groups=req.n_groups, horizons=horizons)
    tag = "icw" if req.method == "ic_weighted" else "eq"
    name = f"syn_{len(req.formulas)}f_{tag}"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    save_report(res["report"], REPORT_DIR / f"{name}.html")
    ic = res["ic"]["ic"]
    ls = res["quantile"]["long_short"]
    return {
        "factor": name, "formulas": req.formulas, "method": req.method,
        "n_samples": len(d),
        "ic": {"mean": round(ic["mean"], 4), "ir": round(ic["ir"], 3),
               "t_stat": round(ic["t_stat"], 2)},
        "long_short": {"annual_return": round(ls["annual_return"], 4),
                       "sharpe": round(ls["sharpe"], 2),
                       "max_drawdown": round(ls["max_drawdown"], 4)},
        "monotonicity": round(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "report_url": f"/api/factors/reports/{name}",
    }
