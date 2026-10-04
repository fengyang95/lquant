"""因子定义 / 计算 / 评价 / 报告。

评价接口的约定：请求里给「因子名 + 公式」，后端用 Parquet 湖里的
日线现算因子值 → 评价 → 存 HTML 报告。这是研究态的轻量路径；
正式的批量因子计算走 CLI / 任务队列。
"""
from __future__ import annotations

import contextlib
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from lquant.core.db import reader
from lquant.core.errors import FactorError
from lquant.data.store.catalog import IndexConsRepo, upsert
from lquant.data.store.parquet import read_daily
from lquant.factors.evaluate import evaluate, forward_return
from lquant.factors.preprocess.pipeline import drop_nonfinite

router = APIRouter(prefix="/factors", tags=["factors"])

_MAX_EVENT_WINDOW = 250  # 与 event_study._MAX_WINDOW 同源；0 允许（只看前窗）


def _save_report_atomic(html_str: str, path: Path) -> Path:
    """原子写报告：同因子名并发 evaluate 时不会互相截断损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(html_str, encoding="utf-8")
    os.replace(tmp, path)  # 同文件系统原子替换
    return path

def _jf(v, nd=4) -> float | None:
    """非有限值统一转 null：metrics/series 混入 NaN/Inf 时 json.dumps 会输出
    裸 NaN 字面量，前端 JSON.parse 直接炸（响应体是非法 JSON）。"""
    return round(float(v), nd) if v is not None and math.isfinite(float(v)) else None


REPORT_DIR = Path("data/reports")


class FactorIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    expression: str = ""                 # DSL 表达式（可选，先存后编译）
    description: str = ""


def _check_universe(v: str) -> str:
    """universe 共享校验：all 放行，其余必须是可解析的池名/指数代码。"""
    from lquant.factors.universe import resolve_index_code

    if v != "all":
        resolve_index_code(v)   # 未知池名 → 直接 ValueError
    return v


def _check_date(v: str | None) -> str | None:
    """end 共享校验：None 放行；非空必须是 ISO 日期。"""
    if v is None:
        return v
    try:
        date.fromisoformat(v)
    except (TypeError, ValueError):
        raise ValueError("需为 YYYY-MM-DD 格式") from None
    return v


class EvaluateIn(BaseModel):
    factor: str = Field(default="mom20", max_length=64,
                        pattern=r"^[A-Za-z0-9_-]+")  # 报告名（防路径穿越）
    formula: str = "pct_change_20"       # 支持 pct_change_{n} / rolling_std_{n}
    n_groups: int = Field(default=5, ge=2, le=20)
    horizons: list[int] = Field(default=[1, 5, 10, 20], min_length=1, max_length=20)
    start: str = "2026-01-01"
    window: int = Field(default=60, ge=20, le=250)  # 滚动窗口（交易日）
    top_ns: list[int] = Field(default=[50, 100], min_length=1, max_length=5,
                              description="Top-N 持仓收缩测试的 N 列表")
    style_threshold: float = Field(default=0.14, ge=0.0, le=1.0,
                                   description="中性化后风格相关性阈值（max|ρ| 判定线）")
    filter_zscore: float | None = Field(default=None, ge=1.0, le=100.0,
                                        description="截面异常收益过滤阈值（|z| 上限，口径同 alphalens；None 不过滤）")
    event_window: list[int] = Field(default=[10, 15], min_length=2, max_length=2,
                                    description="事件式分层收益窗口 [before, after]（交易日）")
    end: str | None = Field(default=None,
                            description="评价区间终点 YYYY-MM-DD；None = 数据末端")
    universe: str = Field(default="all",
                          description="股票池：all = 全市场，或指数代码/别名（hs300/zz500/zz800/zz1000）")
    steps: list[dict] | None = Field(
        default=None, max_length=8,
        description="预处理配方（winsorize/standardize/neutralize/orthogonalize 有序列表）；"
                    "None = 内置默认配方（mad 去极值 → zscore → 市值行业中性化）")
    with_robustness: bool = Field(
        default=False,
        description="是否跑 L3 稳健性（窗口扰动/分段稳定/起点敏感/月度剔除）—— 需重算因子多遍，默认关")

    @field_validator("steps")
    @classmethod
    def _validate_steps(cls, v: list[dict] | None) -> list[dict] | None:
        """配方 fail-fast：未知 stage/method 直接 422，而不是跑出一个空配方。

        静默忽略坏步骤 = 「改了参数没反应」，是这套流水线最贵的坑。
        """
        if v is None:
            return None
        from lquant.factors.preprocess.pipeline import validate_steps

        return validate_steps(v)

    @field_validator("universe")
    @classmethod
    def _validate_universe(cls, v: str) -> str:
        return _check_universe(v)

    @field_validator("end")
    @classmethod
    def _validate_end(cls, v: str | None) -> str | None:
        return _check_date(v)

    @field_validator("event_window")
    @classmethod
    def _validate_event_window(cls, v: list[int]) -> list[int]:
        if any(n < 0 or n > _MAX_EVENT_WINDOW for n in v):
            raise ValueError(f"event_window 各元素须在 0~{_MAX_EVENT_WINDOW}（交易日）")
        return v

    @field_validator("start")
    @classmethod
    def _validate_start(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except (TypeError, ValueError):
            raise ValueError("start 需为 YYYY-MM-DD 格式") from None
        return v

    @field_validator("horizons")
    @classmethod
    def _validate_horizons(cls, v: list[int]) -> list[int]:
        if any(h < 1 or h > 250 for h in v):
            raise ValueError("horizons 元素需在 1..250 内")
        return v

    @field_validator("top_ns")
    @classmethod
    def _validate_top_ns(cls, v: list[int]) -> list[int]:
        if any(n < 1 or n > 3000 for n in v):
            raise ValueError("top_ns 元素需在 1..3000 内")
        return sorted(set(v))


@router.get("")
def list_factors(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=500),
    source: str | None = Query(default=None, description="按来源筛选: qlib/yaml/manual"),
) -> list[dict]:
    """已注册因子（factor_def 表）。offset/limit 分页 + source 筛选。

    category 输出口径：库里存了 category 用库里的；否则 qlib 内置因子
    按 builtin family 推导（「alpha158·kbar」），其余归「自定义」。
    """
    suffix = f"OFFSET {offset}" + (f" LIMIT {limit}" if limit else "")
    where = "WHERE d.source = ?" if source else ""
    order = "icn DESC NULLS LAST" if not source else "created_at DESC"
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT d.name, d.expression, d.description, d.source, d.factor_id, "
                "i.ic_neutral AS icn, d.created_at, d.category FROM factor_def d "
                "LEFT JOIN factor_ic i ON i.factor = d.name "
                f"{where} ORDER BY {order} {suffix}",
                [source] if source else [],
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            # 不能返回 []：库读不出来和「一个因子都没注册」在 UI 上长得一样，
            # 用户会以为因子全丢了
            raise HTTPException(503, f"因子列表读取失败: {e}") from e
    builtin_family = {}
    try:
        from lquant.factors.qlib_alpha import list_builtin

        builtin_family = {x["name"]: x["family"] for x in list_builtin()}
    except Exception:  # noqa: BLE001   builtin 枚举失败不挡列表
        pass
    from lquant.factors.dsl.normalize import normalize_soft

    out = []
    for r in rows:
        cat = r[7] or None
        if not cat and r[3] == "qlib" and r[0] in builtin_family:
            cat = f"alpha158·{builtin_family[r[0]]}"
        # 历史遗留的 qlib 写法（Slope/Mean/Ref…）在这里就翻译成 DSL：
        # 画布预览与「打开已有因子」拿到的必须是引擎真能解析的表达式。
        out.append({"name": r[0], "expression": normalize_soft(r[1]), "description": r[2],
                    "source": r[3], "factor_id": r[4], "ic_neutral": r[5],
                    "created_at": str(r[6]), "category": cat or "自定义"})
    return out


@router.post("")
def register_factor(f: FactorIn) -> dict:
    """注册因子定义。给了 expression 就先归一 + 过 DSL 语法检查。"""
    from lquant.factors.dsl.normalize import normalize

    expr = (f.expression or "").strip()
    if expr:
        try:
            expr = normalize(expr)     # 兼容历史 qlib 写法，落库统一存 DSL
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"DSL 校验失败: {e}") from e
    n = upsert("factor_def", pl.DataFrame([{
        "name": f.name, "expression": expr,
        "description": f.description, "created_at": datetime.now(),
    }]))
    return {"registered": f.name, "rows": n}


class _ValidateIn(BaseModel):
    expression: str


@router.post("/validate")
def validate_expression(v: _ValidateIn) -> dict:
    """DSL 表达式 AST 校验（不落库），供前端注册 / 编辑表单实时校验。

    兼容历史 qlib 写法：归一通过即 ok。响应保持既有 ``{ok, error}`` 形状，
    调用方要 DSL 文本走 ``/ast``（它返回归一后的 ``expression``）。
    """
    expr = (v.expression or "").strip()
    if not expr:
        return {"ok": False, "error": "表达式为空"}
    try:
        from lquant.factors.dsl.normalize import normalize

        normalize(expr)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "error": None}


@router.get("/ops")
def list_ops() -> dict:
    """因子 DSL 算子目录 —— 因子编辑画布据此动态生成积木（唯一真相源）。

    画布不自持算子定义：元数、参数、中文语义全部从这里来，前端与引擎
    就不会出现两套口径（Greater 取大 vs 比较、Ts_ArgMax 的 0/1 基准
    这类分歧无从漂移）。

    返回两部分：`ops` 是注册表里的函数式算子；`infix` 是语法内建的中缀
    算子（`+ - * / < >`，由 parser 直接处理，不在 OPS 注册表里）。
    """
    from lquant.factors.ops.catalog import infix_catalog, op_catalog

    return {"ops": op_catalog(), "infix": infix_catalog()}


@router.get("/fields")
def list_fields() -> list[dict]:
    """因子可用字段清单 —— 与静态校验白名单同源（factors/fields.py）。"""
    from lquant.factors.fields import field_catalog

    return field_catalog()


class _AstIn(BaseModel):
    expression: str


@router.post("/ast")
def expression_ast(v: _AstIn) -> dict:
    """DSL 表达式 → AST JSON，供画布把已有因子反解析成 DAG。

    前端不重写词法/语法分析器（两份解析器必然漂移），只消费这里产出的树
    做形状映射。解析或静态检查失败返回 422 + 原因原文。

    兼容历史 qlib 写法（``Slope($close,10)/$close``）：先经统一翻译器归一成
    lquant DSL 再出 AST，否则老因子在画布上永远打不开。响应里 ``translated``
    标明是否发生过翻译，前端据此提示用户「打开的是兼容翻译后的表达式」。
    """
    raw = (v.expression or "").strip()
    if not raw:
        raise HTTPException(422, "表达式为空")
    try:
        from lquant.factors.dsl.analyzer import check
        from lquant.factors.dsl.json_ast import to_dict
        from lquant.factors.dsl.normalize import normalize
        from lquant.factors.dsl.parser import parse

        expr = normalize(raw)
        ast = parse(expr, "ast")
        check(ast)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"DSL 解析失败: {e}") from e
    return {"expression": expr, "ast": to_dict(ast.root), "translated": expr != raw}


class FactorUpdateIn(BaseModel):
    """部分更新：None 字段保持原值。expression 传空串表示清空 DSL。"""
    expression: str | None = None
    description: str | None = None
    category: str | None = Field(default=None, max_length=64)


@router.put("/{name}")
def update_factor(name: str, f: FactorUpdateIn) -> dict:
    """编辑因子定义（表达式 / 描述 / 类别）。

    口径：只允许改 manual / mined 来源的因子 —— qlib / yaml 因子是种子
    批量灌入的，改了会在下次 seed 时被覆盖，属假编辑。
    """
    with reader() as con:
        try:
            r = con.execute(
                "SELECT name, expression, description, enabled, created_at, "
                "source, source_ref, factor_id, category "
                "FROM factor_def WHERE name = ?", [name]).fetchone()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"因子读取失败: {e}") from e
    if not r:
        raise HTTPException(404, f"因子不存在: {name}")
    cur = {"name": r[0], "expression": r[1] or "", "description": r[2] or "",
           "enabled": r[3], "created_at": r[4], "source": r[5] or "manual",
           "source_ref": r[6], "factor_id": r[7], "category": r[8] or ""}
    if (cur["source"] or "manual") not in ("manual", "mined"):
        raise HTTPException(422, f"{cur['source']} 来源因子为种子灌入，不可编辑（可复制为新因子）")
    if f.expression is None:
        new_expr = cur["expression"]
    else:
        new_expr = f.expression.strip()
        if new_expr:
            try:
                from lquant.factors.dsl.normalize import normalize

                new_expr = normalize(new_expr)   # 兼容 qlib 写法，落库统一存 DSL
            except Exception as e:  # noqa: BLE001
                raise HTTPException(422, f"DSL 校验失败: {e}") from e
    row = {**cur,
           "expression": new_expr,
           "description": cur["description"] if f.description is None else f.description,
           "category": cur["category"] if f.category is None else f.category.strip()}
    n = upsert("factor_def", pl.DataFrame([row]))
    return {"updated": name, "rows": n}


@router.delete("/{name}")
def delete_factor(name: str) -> dict:
    """删除因子定义，并连带清掉 factor_ic 指标缓存（派生数据）。

    挖掘台账 / 历史报告不删 —— 它们是研究留痕。
    """
    from lquant.core.db import writer

    with reader() as con:
        try:
            exists = con.execute(
                "SELECT 1 FROM factor_def WHERE name = ?", [name]).fetchone()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"因子读取失败: {e}") from e
    if not exists:
        raise HTTPException(404, f"因子不存在: {name}")
    with writer() as con:
        con.execute("DELETE FROM factor_def WHERE name = ?", [name])
        with contextlib.suppress(Exception):
            con.execute("DELETE FROM factor_ic WHERE factor = ?", [name])  # 指标缓存清理失败不阻断删除
    return {"deleted": name}


def _universe_symbols(universe: str | None) -> list[str] | None:
    """股票池 → 成分股清单；all/None → 不过滤。

    研究态口径：取该指数最新一次成分快照（IndexConsRepo 的当下口径，
    与 get_index_stocks 一致）；成分表未同步时 503 提示先同步。
    """
    if not universe or universe == "all":
        return None
    from lquant.factors.universe import resolve_index_code

    code = resolve_index_code(universe)
    symbols = IndexConsRepo().latest_symbols(code)
    if not symbols:
        raise HTTPException(
            503, f"指数 {code} 成分股为空，先在数据页同步指数成分（index_cons）")
    return symbols


def _compute_factor(df: pl.DataFrame, formula: str) -> pl.DataFrame:
    """现算因子。优先命中 Qlib Alpha158 内置因子（白名单探测），其次研究常用形态。

    入口先按 (symbol, trade_date) 排序：pct_change/rolling 的 .over("symbol")
    依赖组内行序为日期升序，read_daily 本身不保证顺序（当前只是湖写入时
    排过序）—— 不防的话未来写入顺序一变，因子值静默错位。
    """
    from lquant.factors.qlib_alpha import compute as qlib_compute
    from lquant.factors.qlib_alpha import has_factor

    df = df.sort(["symbol", "trade_date"])
    if has_factor(formula):
        return qlib_compute(df, formula)
    if "$" in formula:                     # DSL 表达式 —— 统一走 FactorEngine
        from lquant.core.errors import FactorError
        from lquant.factors.analysis import compute_factor_col

        try:
            return compute_factor_col(df, formula, "_factor")
        except FactorError as e:  # DSL 语法/算子/字段错误 → 客户端 422，而非 500
            raise HTTPException(422, f"DSL 计算失败: {e}") from e
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


def _neutral_views_for(d: pl.DataFrame, ret_col: str, n_groups: int = 5) -> dict:
    """收益中性化对照 + 行业内分组分层（§5.1 三视图）。

    n_groups 必须透传用户选择 —— 此前恒用默认 5，与页面上的分层组数不一致。
    """
    from lquant.factors.evaluate.neutral_views import neutral_views as _nv

    try:
        cov_cols = [c for c in d.columns if c.startswith("cov_")]
        if not cov_cols:
            return {"view": "raw（未中性化 —— 协变量数据不可用）"}
        return _nv(d, "_factor", ret_col, covariates=cov_cols,
                   group_col="cov_industry_sw1" if "cov_industry_sw1" in d.columns else None,
                   n_groups=n_groups)
    except Exception as e:  # noqa: BLE001
        # 不能静默成 {}：页面会把「算炸了」显示成「协变量数据不足」，
        # 两种情况的处置完全不同（一个是修数据，一个是修代码）
        return {"view": "error", "error": f"{type(e).__name__}: {e}"}


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
                    cov_report: dict | None = None,
                    errors: dict | None = None) -> list[dict]:
    """逐段叠加协变量看 IC 怎么掉：原始 → +市值 → +行业 → +换手率。

    dd/cov_report 可由调用方传入（协变量只构建一次，ladder 与 views 复用）。
    errors 传入时记录失败原因 —— 「阶梯缺一段」和「这一段算不出来」必须能区分。
    """
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    def _fail(key: str, e: Exception) -> None:
        if errors is not None:
            errors[key] = f"{type(e).__name__}: {e}"

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
        except Exception as e:  # noqa: BLE001
            _fail("neutral_ladder:covariates", e)
            return []
        cov_report = {r["covariate"]: r["coverage"] for r in report}
    cov_report = cov_report or {}
    for label, covs in levels:
        steps = [{"op": "winsorize", "method": "mad", "n": 5},
                 {"op": "standardize", "method": "zscore"}]
        if covs:
            cols = [f"cov_{c}" for c in covs if f"cov_{c}" in dd.columns]
            if covs and not cols:
                _fail(f"neutral_ladder:{label}", RuntimeError("协变量列全缺失"))
                continue
            steps.append({"op": "neutralize", "method": "ols", "factors": cols})
        try:
            r = pipeline_run(dd, col, steps)
        except Exception as e:  # noqa: BLE001
            _fail(f"neutral_ladder:{label}", e)
            continue
        r = drop_nonfinite(r, col)
        s = ic_series(r, col, ret_col)
        if not len(s):
            continue
        out.append({
            "label": label, "covs": covs,
            "ic_mean": _jf(s["ic"].mean()),
            "rank_ic_mean": _jf(s["rank_ic"].mean()),
            "n_days": len(s),
            "coverage": round(min((cov_report.get(c, 1.0) for c in covs), default=1.0), 4),
        })
    return out


def _evaluate_full(req: EvaluateIn, progress=None, cancel_check=None) -> tuple[dict, dict]:
    """一次现算 + 评价，返回 (metrics, series)。

    /evaluate 与 /evaluate/series 共用同一份计算（缺陷 #2：原先两端点
    各自全量重算，2 倍开销且两次结果可能不一致）。
    progress / cancel_check：任务化执行时由 enqueue 注入的回调 ——
    progress 在阶段边界上报 (done, total=100, phase)，cancel_check 在
    阶段边界轮询，返回 True 抛 JobCanceled 提前收尾。同步调用两参皆 None。
    """

    def _step(pct: int, phase: str) -> None:
        if cancel_check is not None and cancel_check():
            from lquant.server.jobs import JobCanceled

            raise JobCanceled(f"因子评价已取消: {req.factor}")
        if progress is not None:
            progress(done=pct, total=100, phase=phase)

    _step(2, "读取日线")
    df = read_daily(start=req.start, end=req.end,
                    symbols=_universe_symbols(req.universe)).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    _step(10, "计算因子")
    d = drop_nonfinite(_compute_factor(df, req.formula), "_factor")
    d = forward_return(d, "close", periods=req.horizons)
    ret_col = f"fwd_ret_{min(req.horizons)}"
    if ret_col not in d.columns:
        raise HTTPException(500, f"前瞻收益列缺失: {ret_col}")

    # 截面异常收益过滤（可选）：过滤一次，指标 / 序列 / 报告三处口径保持一致
    outlier_stats = None
    if req.filter_zscore is not None:
        from lquant.factors.evaluate import zscore_filter_with_stats

        # 只算一次：帧 + 统计一次算出（此前 filter_zscore 被全量算两遍，
        # 面板大时这是评估链路里最贵的一步）
        d, outlier_stats = zscore_filter_with_stats(d, threshold=req.filter_zscore)

    # ---- 协变量（市值/行业/换手率/动量）：必须在 evaluate() 之前构建 ----
    # 报告的「归因分解」需要分类维度，而 read_daily 只有个股维度。此前协变量在
    # evaluate() 之后才建 → 报告拿不到行业列 → 静默回退成按 symbol 归因
    # （实测 242/242 份 API 生成的报告都是「归因分解 · symbol」）。顺序即口径。
    _step(20, "构建协变量")
    errors: dict[str, str] = {}
    cov_names_all = ["market_cap", "industry_sw1", "turnover_1m", "momentum_1m"]
    try:
        with reader() as con:
            ind = con.execute("SELECT symbol, std, code, std_date FROM industry_classify").pl()
    except Exception as e:  # noqa: BLE001
        ind = None
        errors["industry_classify"] = f"{type(e).__name__}: {e}"
    try:
        from lquant.factors.covariates import build_covariates

        d, cov_report = build_covariates(d, cov_names_all, industry_df=ind)
        cov_map = {r["covariate"]: r["coverage"] for r in cov_report}
    except Exception as e:  # noqa: BLE001
        d, cov_map = d, {}
        errors["covariates"] = f"{type(e).__name__}: {e}"

    # 归因阶梯的基线始终是「原始因子」：用户配方只作用于主评价，不改变
    # 「中性化到底吃掉了多少 IC」这个问题的答案。
    d_pre_recipe = d
    # 用户配方（可选）：协变量就绪后才应用，neutralize 步骤才拿得到 cov_* 列。
    # 不给配方 = 维持原口径（原始因子直接评价），不改默认行为。
    applied_steps = None
    if req.steps:
        from lquant.factors.preprocess.pipeline import run as pipeline_run

        try:
            d = pipeline_run(d, "_factor", req.steps)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"预处理配方执行失败: {e}") from e
        applied_steps = req.steps
        d = drop_nonfinite(d, "_factor")

    # 分类维度必须是「有覆盖率的」行业列：coverage=0 的协变量列虽然存在，
    # 但全 null —— 拿它做归因等于又换了一种空输出。原始 industry_sw1 列不受此限。
    cat_col = next(
        (c for c in ("cov_industry_sw1", "industry_sw1")
         if c in d.columns and (c == "industry_sw1" or cov_map.get("industry_sw1", 0) > 0)),
        None)
    cov_cols_present = [c for c in d.columns if c.startswith("cov_")]

    # ---- metrics（原 run_evaluate 计算体） ----
    _step(35, "评价计算")
    res = evaluate(d, "_factor", ret_col=ret_col, n_groups=req.n_groups,
                   horizons=req.horizons, outlier_stats=outlier_stats,
                   cat_col=cat_col, group_col=cat_col,
                   bps_list=[0.0, 5.0, 10.0, 15.0, 30.0],
                   universe=req.universe, expr=req.formula, covs=cov_cols_present,
                   with_robustness=req.with_robustness,
                   event_window=(req.event_window[0], req.event_window[1]))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _save_report_atomic(res["report"], REPORT_DIR / f"{req.factor}.html")
    ic = res["ic"]["ic"]
    ric = res["ic"]["rank_ic"]
    ls = res["quantile"]["long_short"]
    metrics = {
        "factor": req.factor,
        "formula": req.formula,
        "n_samples": len(d),
        # _jf 兜底：薄截面/常数因子下这些值可能是 NaN，裸 NaN 会产出非法 JSON
        "ic": {"mean": _jf(ic["mean"]), "ir": _jf(ic["ir"], 3),
               "t_stat": _jf(ic["t_stat"], 2), "positive_rate": _jf(ic["positive_rate"]),
               "ic_gt_002_rate": _jf(ic["ic_gt_002_rate"]),
               # 与 CLI audit 对齐：NW t 值与 IC 自相关此前只在 CLI 有
               "t_stat_nw": _jf(ic.get("t_stat_nw"), 2),
               "ic_autocorr": _jf(ic.get("ic_autocorr"), 3)},
        "rank_ic_mean": _jf(ric["mean"]),
        "long_short": {"annual_return": _jf(ls["annual_return"]),
                       "sharpe": _jf(ls["sharpe"], 2),
                       "max_drawdown": _jf(ls["max_drawdown"])},
        "monotonicity": _jf(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "suggested_rebalance": res["decay"]["suggested_rebalance"],
        # 评级：L2 判据的最终结论（此前只有 CLI audit 拿得到）
        "rating": res["rating"],
        # L3 稳健性：默认关（要重算因子多遍），请求 with_robustness=true 才返回
        "robustness": res.get("robustness"),
        # 本次实际使用的预处理配方（None = 原始因子直接评价）
        "steps": applied_steps,
        "covariates": cov_map,
        "excess": {},                    # 计算体在下方超额块完成后回填
        "annual_turnover": None,
        "top_n": [],
        "style_corr": {"max_abs": None, "passed": None},
        "report_url": f"/api/factors/reports/{report_path.stem}",
    }

    # ---- series（原 evaluate_series 计算体） ----
    import numpy as np

    from lquant.factors.evaluate import ic_by_year, ic_series, quantile_nav, quantile_summary
    from lquant.factors.evaluate.decay import decay_profile

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

    # ---- IC 归因阶梯（方案 5.3）+ 三种中性化视图（方案 5.1） ----
    _step(55, "归因与中性化")
    ladder = _neutral_ladder(d_pre_recipe, "_factor", ret_col,
                             dd=d_pre_recipe, cov_report=cov_map, errors=errors)
    if not ladder:
        errors.setdefault("neutral_ladder", "归因阶梯为空（协变量不可用或 IC 序列不足）")
    _persist_ic(req.factor, ladder)
    views = _neutral_views_for(d, ret_col, req.n_groups)
    if views.get("view") == "error":
        errors["neutral_views"] = views["error"]

    # ---- 分组 IC：行业组 + 市值组（识破「信号只来自小市值/某一行业」） ----
    # 此前 ic_by_group / size_group 在生产代码里不可达（report 的 group_col 被丢弃）。
    group_ic: dict = {"by": cat_col, "industry": [], "size": [], "size_col": None,
                      "error": None}
    try:
        from lquant.factors.evaluate.group_ic import ic_by_group, size_group

        if cat_col:
            gi = ic_by_group(d, "_factor", ret_col, cat_col)
            group_ic["industry"] = [
                {"group": str(r["group"]), "ic_mean": _jf(r["ic_mean"]),
                 "rank_ic_mean": _jf(r["rank_ic_mean"]), "ir": _jf(r["ir"], 3),
                 "n_days": int(r["n_days"])} for r in gi.to_dicts()
            ] if len(gi) else []
        mcap_col = next((c for c in ("cov_market_cap", "float_mv", "amount")
                         if c in d.columns), None)
        if mcap_col:
            ds = size_group(d, mcap_col=mcap_col, n_groups=3)
            gs = ic_by_group(ds, "_factor", ret_col, "size_q")
            group_ic["size_col"] = mcap_col
            group_ic["size"] = [
                {"group": f"size_q{int(r['group'])}", "ic_mean": _jf(r["ic_mean"]),
                 "rank_ic_mean": _jf(r["rank_ic_mean"]), "ir": _jf(r["ir"], 3),
                 "n_days": int(r["n_days"])} for r in gs.to_dicts()
            ] if len(gs) else []
    except Exception as e:  # noqa: BLE001
        group_ic["error"] = f"{type(e).__name__}: {e}"
        errors["group_ic"] = group_ic["error"]

    # ---- 超额收益体系 / Top-N 收缩测试 / 中性化后风格相关性（研报标准三件套） ----
    _step(70, "超额与Top-N")
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
    except Exception as e:  # noqa: BLE001
        top_n_rows = []
        errors["top_n"] = f"{type(e).__name__}: {e}"

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
    except Exception as e:  # noqa: BLE001
        style_corr = {}
        errors["style_corr"] = f"{type(e).__name__}: {e}"

    try:
        from lquant.factors.evaluate.costs import factor_turnover

        to_df = factor_turnover(d, "_factor", req.n_groups)
        mean_to = to_df["turnover_avg"].drop_nulls().mean() if len(to_df) else None
        annual_turnover = _jf(float(mean_to) * 252, 2) if mean_to is not None else None
    except Exception as e:  # noqa: BLE001
        annual_turnover = None
        errors["turnover"] = f"{type(e).__name__}: {e}"

    # 回填 metrics（超额/TopN/风格相关在 metrics 构造后才可算，这里统一写入）
    metrics["excess"] = excess_metrics
    metrics["annual_turnover"] = annual_turnover
    metrics["top_n"] = top_n_rows
    metrics["style_corr"] = {"max_abs": style_corr.get("max_abs"),
                             "passed": style_corr.get("passed")}

    # 6) 滚动窗口 IC / RankIC / IR
    _step(85, "滚动与事件分析")
    from lquant.factors.evaluate.rolling import rolling_ic
    rwin = rolling_ic(d, "_factor", ret_col, req.window)
    rolling = {
        "window": req.window,
        "dates": [str(x) for x in rwin["trade_date"].to_list()] if len(rwin) else [],
        "ic": [_jf(v) for v in rwin["ic_mean"].to_list()] if len(rwin) else [],
        "rank_ic": [_jf(v) for v in rwin["rank_ic_mean"].to_list()] if len(rwin) else [],
        "ir": [_jf(v, 3) for v in rwin["ir"].to_list()] if len(rwin) else [],
    }

    # 7) 事件式分层收益（±N 日）：事前发散=描述既成趋势，事后发散=预测性信号
    event_study = {}
    try:
        from lquant.factors.evaluate.event_study import event_study_summary

        es = event_study_summary(d, "_factor", "close", n_groups=req.n_groups,
                                 before=req.event_window[0], after=req.event_window[1])
        event_study = {
            "rel_periods": es["rel_periods"],
            "curves": {k: [_jf(v) for v in v_list] for k, v_list in es["curve"].items()},
            "spread": [_jf(v) for v in es["spread"]],
            "look_ahead_ratio": _jf(es["look_ahead_ratio"], 3),
            "before": es["before"], "after": es["after"], "demeaned": es["demeaned"],
        }
    except Exception as e:  # noqa: BLE001
        event_study = {}
        errors["event_study"] = f"{type(e).__name__}: {e}"

    metrics["outlier"] = (
        {"threshold": outlier_stats["threshold"],
         "n_dropped": outlier_stats["n_dropped"],
         "dropped_rate": round(outlier_stats["dropped_rate"], 5)}
        if outlier_stats else None
    )

    series = {
        "factor": req.factor, "formula": req.formula,
        "n_groups": req.n_groups, "n_samples": len(d),
        "ic": {"dates": ic_dates, "ic": ic_vals, "rank_ic": ic_ranks, "cum_ic": cum_ic},
        "quantile": {"dates": qdates, "curves": curves, "groups": groups,
                     "monotonicity": _jf(qsum.get("monotonicity"), 3)},
        "decay": decay, "ic_by_year": ic_year, "neutral_ladder": ladder,
        "neutral_views": views,
        "group_ic": group_ic,
        "rolling": rolling,
        "excess": {"dates": ex_dates, "curves": ex_curves, "benchmark": "股票池等权"},
        "top_n": top_n_rows,
        "style_corr": style_corr,
        "event_study": event_study,
        # 故障可见：UI 必须能区分「没数据」与「算炸了」
        "errors": errors,
    }
    metrics["errors"] = errors
    _step(95, "汇总")
    return metrics, series


def _check_formula_supported(formula: str) -> None:
    """公式白名单预检（与 _compute_factor 分支一致）。

    任务化后计算在队列线程执行，坏公式不再同步 422 —— 校验前移到入队前，
    不支持的公式保持同步 422，而不是变成队列里跑挂的任务。
    """
    from lquant.factors.qlib_alpha import has_factor

    if has_factor(formula) or "$" in formula:
        return
    if formula.startswith("pct_change_") and formula.rsplit("_", 1)[1].isdigit():
        return
    if formula.startswith("rolling_std_") and formula.rsplit("_", 1)[1].isdigit():
        return
    if formula == "turnover":
        return
    raise HTTPException(422, f"暂不支持的因子公式: {formula}（内置因子见 GET /api/factors/builtin）")


def _run_evaluate_job(req_d: dict, cancel_check=None, progress=None) -> dict:
    """因子评价任务体：与 /evaluate 同一计算体，结果落 job_results 供任务中心渲染。"""
    req = EvaluateIn(**req_d)
    m, s = _evaluate_full(req, progress=progress, cancel_check=cancel_check)
    m["series"] = s
    from lquant.server.eval_results import save_result

    # 取消后不落结果：协作式取消收尾时，线程可能已越过最后的 cancel_check
    if cancel_check is not None and cancel_check():
        from lquant.server.jobs import JobCanceled

        raise JobCanceled(f"因子评价已取消: {req_d.get('factor')}")
    save_result(_job_result_id(req.factor), "factor_eval", req_d, m)
    return m


def _job_result_id(factor: str) -> str:
    """确定性任务 id / 结果主键：同因子重跑覆盖旧任务与旧结果。"""
    return f"factor-eval-{factor}"


@router.post("/evaluate", status_code=202)
def run_evaluate(req: EvaluateIn) -> dict:
    """因子评价任务化：入队 lquant-mining，任务中心可见、可取消。

    202 {job_id}；进度经 /ws/jobs/{job_id} 流式推送，完成后同一通道带
    result，或 GET /evaluate/{job_id} 轮询取结果。
    """
    from lquant.server.jobs import enqueue, get_job

    _check_formula_supported(req.formula)
    jid = _job_result_id(req.factor)
    # 确定性 id 的并发保护：同因子上一轮评价仍在跑则拒绝（409），避免同 id
    # 双线程互踩进度/结果。
    existing = get_job(jid)
    if existing is not None and existing.get_status() in ("queued", "started"):
        raise HTTPException(409, f"因子 {req.factor} 的评价任务进行中，请稍后再试")
    enqueue("lquant-mining", _run_evaluate_job, req.model_dump(),
            job_id=jid, name="因子评价")
    return {"job_id": jid, "status": "queued"}


@router.get("/evaluate/{job_id}")
def get_evaluate_result(job_id: str) -> dict:
    """评价任务结果查询（WS 断连兜底 / 刷新页面恢复）。404 = 尚未完成或不存在。"""
    from lquant.server.eval_results import get_result

    r = get_result(job_id)
    if r is None:
        raise HTTPException(404, f"结果不存在（任务未完成或 id 错误）: {job_id}")
    return r


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


@router.get("/universes")
def list_universes() -> list[dict]:
    """股票池选项（评价/相关性/合成统一口径）。"""
    from lquant.factors.universe import all_universe_options

    return [{"key": k, "index_code": code, "label": label}
            for k, code, label in all_universe_options()]


@router.get("/sources")
def list_factor_sources() -> list[dict]:
    """因子来源清单（M2 来源接入）。"""
    from lquant.factors.sources import list_sources

    return list_sources()


@router.get("/preprocess/methods")
def list_preprocess_methods(
    stage: str | None = Query(default=None,
                             description="按阶段过滤：winsorize/standardize/neutralize/orthogonalize"),
) -> dict:
    """预处理方法枚举 + 默认配方（M2.5「配方接入 API/UI」）。

    前端据此渲染配方选择器，不必硬编码方法名 —— 加新方法不用改前端。

    每个方法带口径自省字段（``formula`` / ``notes`` / ``zero_variance``，见
    ``preprocess.registry`` 的元数据词汇表），用于消除与 AlphaPurify 交叉验证时的
    「同名不同义」歧义。顶层另附 ``mad_convention`` 说明 MAD 的 1.4826 修正。
    """
    from lquant.factors.preprocess.registry import STAGES, default_pipeline, list_methods
    from lquant.factors.preprocess.winsorize import MAD_K

    if stage is not None and stage not in STAGES:
        raise HTTPException(422, f"未知预处理阶段 {stage!r}，可选: {list(STAGES)}")
    return {
        "stages": list(STAGES),
        "methods": list_methods(stage),
        "default_recipe": default_pipeline(),
        "mad_convention": {
            "scale_factor": MAD_K,
            "formula": "median ± n × 1.4826 × MAD",
            "n_semantics": "equivalent_sigma_multiple",
            "alphapurify_conversion": "n_lquant = n_alphapurify / 1.4826",
            "note": "AlphaPurify mad_winsorize 的 n 是 MAD 倍数（默认 3），"
                    "本仓 n 是等效 σ 倍数（默认 5）—— 同名不同义。",
        },
    }


class TraceIn(BaseModel):
    """截面快照请求（Phase 3.3）。

    ``factor`` 是**展示名**（原样回显在结果里），不是列名 —— 实际计算列固定为
    ``_factor``，与 ``/evaluate/series`` 同一约定。
    """

    factor: str = Field(default="mom20", max_length=64)
    formula: str = "pct_change_20"
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bins: int = Field(default=10, ge=2, le=50)
    side: str = Field(default="long", pattern=r"^(long|short|top|bottom)$")
    horizon: int = Field(default=1, ge=1, le=60)
    top: int | None = Field(default=50, ge=1, le=1000)
    start: str = "2026-01-01"
    end: str | None = None
    universe: str = "all"


@router.post("/trace")
def trace_ep(req: TraceIn) -> dict:
    """某日某箱的成分与收益明细 —— 排查「净值跳变是哪几只票」的最快路径。

    分箱口径与 ``/factors/evaluate/series`` 的分层回测**同一实现**
    （``evaluate.quantile.add_quantile``），所以快照里的「第 N 组」与曲线上的
    第 N 组一定是同一批票。
    """
    from lquant.factors.evaluate import trace_snapshot
    from lquant.factors.evaluate.returns import forward_return

    df = read_daily(start=req.start, end=req.end,
                    symbols=_universe_symbols(req.universe)).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    d = _compute_factor(df, req.formula)
    d = forward_return(d, "close", periods=[req.horizon])
    d = drop_nonfinite(d, f"fwd_ret_{req.horizon}")
    try:
        # 计算列固定是 `_factor`（与 /evaluate/series 同一约定），
        # `req.factor` 只是**展示名** —— 早先直接拿它当列名，
        # 用默认值 `mom20` 调这个端点必然 422「因子列不存在」。
        snap = trace_snapshot(d, "_factor", date=req.date, bins=req.bins,
                              side=req.side, horizon=req.horizon, top=req.top)
    except FactorError as e:
        raise HTTPException(422, str(e)) from e
    snap["factor"] = req.factor
    return snap


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
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"挖掘台账读取失败: {e}") from e
    return [{"run_id": r[0], "agent": r[1], "generator": r[2],
             "n_evaluated": r[3], "n_static_fail": r[4], "n_low_ic": r[5],
             "n_redundant": r[6], "n_size_proxy": r[7], "n_survivors": r[8],
             "created_at": str(r[9])} for r in rows]


@router.get("/mine/runs/{run_id}")
def get_mining_run(run_id: str) -> dict:
    """单次挖掘会话详情：漏斗计数 + 表达式修正日志（corrections）。"""
    import json

    with reader() as con:
        try:
            r = con.execute(
                "SELECT run_id, agent, generator, n_evaluated, n_static_fail, n_low_ic, "
                "n_redundant, n_size_proxy, n_survivors, corrections, created_at "
                "FROM factor_mining_run WHERE run_id = ?", [run_id]).fetchone()
        except Exception as e:  # noqa: BLE001
            # 读失败 ≠ 不存在：把 DB 故障伪装成 404 会让用户以为记录丢了
            raise HTTPException(503, f"挖掘会话读取失败: {e}") from e
    if not r:
        raise HTTPException(404, f"挖掘会话不存在: {run_id}")
    try:
        corrections = json.loads(r[9] or "{}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"corrections 字段不是合法 JSON: {e}") from e
    return {"run_id": r[0], "agent": r[1], "generator": r[2],
            "n_evaluated": r[3], "n_static_fail": r[4], "n_low_ic": r[5],
            "n_redundant": r[6], "n_size_proxy": r[7], "n_survivors": r[8],
            "corrections": corrections, "created_at": str(r[10])}


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
    except Exception as e:  # noqa: BLE001
        # 不阻断入队，但必须留痕：静默失败会让「台账里没有这次挖掘」无从解释
        import loguru

        loguru.logger.warning(f"挖掘占位行写入失败（任务仍入队）: {e}")


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
    ledger_warning = None
    try:
        with writer() as con:
            con.execute(
                "INSERT OR REPLACE INTO factor_mining_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, agent_name, generator, res.n_evaluated, res.n_static_fail,
                 res.n_low_ic, res.n_redundant, res.n_size_proxy, res.n_survivors,
                 json.dumps(res.corrections, ensure_ascii=False)[:10000], dt.datetime.now()])
    except Exception as e:  # noqa: BLE001
        # 记账失败不阻断结果，但要在响应里显式带出来（CLI 侧同样打 warn）
        import loguru

        loguru.logger.warning(f"挖掘记账落库失败（结果仍有效）: {e}")
        ledger_warning = str(e)
    return {"run_id": run_id, "agent": agent_name, "generator": generator,
            "n_evaluated": res.n_evaluated, "n_static_fail": res.n_static_fail,
            "n_low_ic": res.n_low_ic, "n_redundant": res.n_redundant,
            "n_size_proxy": res.n_size_proxy, "n_survivors": res.n_survivors,
            "survivors": survivors[:10], "ledger_warning": ledger_warning}


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
    from lquant.factors.dsl.normalize import normalize_soft

    with reader() as con:
        try:
            r = con.execute(
                "SELECT name, expression, description, created_at, source, category "
                "FROM factor_def WHERE name = ?", [name]).fetchone()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"因子读取失败: {e}") from e
    if not r:
        raise HTTPException(404, f"因子不存在: {name}")
    reports = []
    if REPORT_DIR.exists():
        for p in sorted(REPORT_DIR.glob(f"{name}*.html"), key=lambda x: -x.stat().st_mtime):
            reports.append({"name": p.stem, "url": f"/api/factors/reports/{p.stem}"})
    return {"name": r[0], "expression": normalize_soft(r[1]), "description": r[2],
            "created_at": str(r[3]), "source": r[4] or "manual",
            "category": r[5] or "", "reports": reports}


class AnalyzeIn(BaseModel):
    formulas: list[str] = Field(min_length=2, max_length=8)
    start: str = "2026-01-01"
    end: str | None = Field(default=None, description="区间终点 YYYY-MM-DD；None = 数据末端")
    universe: str = Field(default="all",
                          description="股票池：all = 全市场，或指数代码/别名")
    threshold: float = Field(default=0.8, ge=0.5, le=1.0)

    @field_validator("universe")
    @classmethod
    def _validate_universe(cls, v: str) -> str:
        return _check_universe(v)

    @field_validator("end")
    @classmethod
    def _validate_end(cls, v: str | None) -> str | None:
        return _check_date(v)


@router.post("/analyze")
def analyze(req: AnalyzeIn) -> dict:
    """多因子相关性 / 冗余分析（F6）。冗余因子不建议同时入库。"""
    from lquant.core.errors import FactorError
    from lquant.factors.analysis import correlation

    df = read_daily(start=req.start, end=req.end,
                    symbols=_universe_symbols(req.universe)).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    try:
        return correlation(df, req.formulas, threshold=req.threshold)
    except (ValueError, FactorError) as e:
        # FactorError 不是 ValueError：公式本身写错（DSL 未知字段/算子）也是
        # 客户端问题，不能放任它冒成 500
        raise HTTPException(422, str(e)) from e


class SynthesizeIn(BaseModel):
    formulas: list[str] = Field(min_length=2, max_length=8)
    method: str = Field(default="equal", pattern="^(equal|ic_weighted)$")
    ic_horizon: int = Field(default=5, ge=1, le=20)
    n_groups: int = Field(default=5, ge=2, le=20)
    start: str = "2026-01-01"
    end: str | None = Field(default=None, description="区间终点 YYYY-MM-DD；None = 数据末端")
    universe: str = Field(default="all",
                          description="股票池：all = 全市场，或指数代码/别名")

    @field_validator("universe")
    @classmethod
    def _validate_universe(cls, v: str) -> str:
        return _check_universe(v)


@router.post("/synthesize")
def synthesize(req: SynthesizeIn) -> dict:
    """因子合成（F7）：等权 / IC 加权 → 全套评价 → 存报告。"""
    from lquant.core.errors import FactorError
    from lquant.factors import analysis as fa

    df = read_daily(start=req.start, end=req.end,
                    symbols=_universe_symbols(req.universe)).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    try:
        d = fa.synthesize(df, req.formulas, method=req.method,
                          ic_horizon=req.ic_horizon).drop_nulls(["_syn"])
    except (ValueError, FactorError) as e:
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
    _save_report_atomic(res["report"], REPORT_DIR / f"{name}.html")
    ic = res["ic"]["ic"]
    ls = res["quantile"]["long_short"]
    return {
        "factor": name, "formulas": req.formulas, "method": req.method,
        "n_samples": len(d),
        "ic": {"mean": _jf(ic["mean"]), "ir": _jf(ic["ir"], 3),
               "t_stat": _jf(ic["t_stat"], 2)},
        "long_short": {"annual_return": _jf(ls["annual_return"]),
                       "sharpe": _jf(ls["sharpe"], 2),
                       "max_drawdown": _jf(ls["max_drawdown"])},
        "monotonicity": _jf(res["quantile"]["monotonicity"], 3),
        "half_life": res["decay"]["half_life"],
        "report_url": f"/api/factors/reports/{name}",
    }
