"""因子定义 / 计算 / 评价 / 报告。

评价接口的约定：请求里给「因子名 + 公式」，后端用 Parquet 湖里的
日线现算因子值 → 评价 → 存 HTML 报告。这是研究态的轻量路径；
正式的批量因子计算走 CLI / 任务队列。
"""
from __future__ import annotations

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
    horizons: list[int] = Field(default=[1, 5, 10, 20])
    start: str = "2026-01-01"


@router.get("")
def list_factors(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[dict]:
    """已注册因子（factor_def 表）。offset/limit 分页，limit 缺省返回全量。"""
    suffix = f"OFFSET {offset}" + (f" LIMIT {limit}" if limit else "")
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT name, expression, description, created_at FROM factor_def "
                f"ORDER BY created_at DESC {suffix}"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
    return [{"name": r[0], "expression": r[1], "description": r[2], "created_at": str(r[3])}
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


def _evaluate_full(req: EvaluateIn) -> tuple[dict, dict]:
    """一次现算 + 评价，返回 (metrics, series)。

    /evaluate 与 /evaluate/series 共用同一份计算（缺陷 #2：原先两端点
    各自全量重算，2 倍开销且两次结果可能不一致）。
    """
    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    d = _compute_factor(df, req.formula).drop_nulls(["_factor"])
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
               "t_stat": round(ic["t_stat"], 2), "positive_rate": round(ic["positive_rate"], 4)},
        "rank_ic_mean": round(res["ic"]["rank_ic"]["mean"], 4),
        "long_short": {"annual_return": round(ls["annual_return"], 4),
                       "sharpe": round(ls["sharpe"], 2),
                       "max_drawdown": round(ls["max_drawdown"], 4)},
        "monotonicity": round(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "suggested_rebalance": res["decay"]["suggested_rebalance"],
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

    series = {
        "factor": req.factor, "formula": req.formula,
        "n_groups": req.n_groups, "n_samples": len(d),
        "ic": {"dates": ic_dates, "ic": ic_vals, "rank_ic": ic_ranks, "cum_ic": cum_ic},
        "quantile": {"dates": qdates, "curves": curves, "groups": groups,
                     "monotonicity": _jf(qsum.get("monotonicity"), 3)},
        "decay": decay, "ic_by_year": ic_year,
    }
    return metrics, series


@router.post("/evaluate")
def run_evaluate(req: EvaluateIn) -> dict:
    """现算因子 → 全套评价 → 存报告。一次计算同时返回指标与图表序列。"""
    m, s = _evaluate_full(req)
    m["series"] = s
    return m
    if ret_col not in d.columns:
        raise HTTPException(500, f"前瞻收益列缺失: {ret_col}")

    res = evaluate(d, "_factor", ret_col=ret_col, n_groups=req.n_groups,
                   horizons=req.horizons)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = save_report(res["report"], REPORT_DIR / f"{req.factor}.html")
    ic = res["ic"]["ic"]
    ls = res["quantile"]["long_short"]
    return {
        "factor": req.factor,
        "formula": req.formula,
        "n_samples": len(d),
        "ic": {"mean": round(ic["mean"], 4), "ir": round(ic["ir"], 3),
               "t_stat": round(ic["t_stat"], 2), "positive_rate": round(ic["positive_rate"], 4)},
        "rank_ic_mean": round(res["ic"]["rank_ic"]["mean"], 4),
        "long_short": {"annual_return": round(ls["annual_return"], 4),
                       "sharpe": round(ls["sharpe"], 2),
                       "max_drawdown": round(ls["max_drawdown"], 4)},
        "monotonicity": round(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "suggested_rebalance": res["decay"]["suggested_rebalance"],
        "report_url": f"/api/factors/reports/{report_path.stem}",
    }


@router.post("/evaluate/series")
def evaluate_series(req: EvaluateIn) -> dict:
    """图表数据包端点（兼容别名）：内部走同一 helper，一次计算两处复用。"""
    _, s = _evaluate_full(req)
    return s


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
    n = upsert("factor_def", pl.DataFrame([{
        "name": x["name"],
        "expression": x["formula"],
        "description": f"Qlib Alpha158 · {x['family']}",
        "created_at": now,
    } for x in items]))
    return {"seeded": len(items), "rows_written": n,
            "sample": [x["name"] for x in items[:5]]}


@router.get("/reports")
def list_reports() -> list[dict]:
    if not REPORT_DIR.exists():
        return []
    return [{"name": p.stem, "url": f"/api/factors/reports/{p.stem}",
             "size_kb": p.stat().st_size // 1024}
            for p in sorted(REPORT_DIR.glob("*.html"), key=lambda x: -x.stat().st_mtime)]


@router.get("/reports/{name}")
def get_report(name: str) -> FileResponse:
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
    report_path = save_report(res["report"], REPORT_DIR / f"{name}.html")
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
