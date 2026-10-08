"""/ml API：模型注册表、训练/滚动重训任务、每日推理与信号。

设计要点（与 Phase 2 的目标对齐：**可调度、可回放、可回滚**）：

- 训练与重训都是**队列任务**（``lquant-ml``），返回 job_id 供任务中心轮询；
  长训练不能占住请求线程（Alpha158 + LGBM 在几千只票上是分钟级）。
- 模型版本、晋级、回滚、as-of 查询走 ``research.ml.registry``；
  **同一份状态只有一个真源**，API 不自己拼 SQL 逻辑。
- 股票池（``universe``）一律取**区间起点当时已生效**的指数成分快照，
  口径与 ``server/api/factors`` 对齐：拿「今天的成分」回溯历史窗口是
  幸存者偏差。降级替换（起点早于首批快照）走 ``universe_note`` 显式披露。
- 推理端点把信号落 ``ml_signal``（带 model_version），
  事后可审计「某天的信号是哪一版出的」。
"""
from __future__ import annotations

import json
import uuid
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from lquant.core.db import reader
from lquant.core.errors import MLError
from lquant.data.store.parquet import read_daily
from lquant.research.ml import registry
from lquant.server.jobs import enqueue, get_job, request_cancel

router = APIRouter(prefix="/ml", tags=["ml"])

_QUEUE = "lquant-ml"
_MAX_FEATURES = 400


# ---------------------------------------------------------------- 请求模型

class _Base(BaseModel):
    features: list[str] = Field(min_length=1, max_length=_MAX_FEATURES)
    start: str = Field(default="2024-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    universe: str = "all"
    label_horizon: int = Field(default=5, ge=1, le=60)
    kind: str = "auto"
    top_n: int = Field(default=30, ge=1, le=200)
    processors: list[dict] | None = Field(default=None, max_length=8)
    model_name: str | None = Field(default=None, max_length=64,
                                   pattern=r"^[A-Za-z0-9_-]*$")
    model_params: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        from lquant.research.ml.model import available_backends

        if v != "auto" and v not in available_backends():
            raise ValueError(f"未知/不可用的模型后端 {v!r}，可选: {available_backends()}")
        return v

    @field_validator("processors")
    @classmethod
    def _procs(cls, v: list[dict] | None) -> list[dict] | None:
        if not v:
            return v
        from lquant.research.ml.processor import make_processor

        for spec in v:
            try:
                make_processor(spec)      # 未知 kind 直接 422，别等任务跑起来才炸
            except (KeyError, MLError) as e:
                # pydantic 只把 ValueError/AssertionError 转 422；注册表查不到
                # 抛的是 KeyError、我们的领域错误是 MLError，都会穿透成 500。
                raise ValueError(str(e)) from e
        return v


class TrainIn(_Base):
    train_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    stage: str = "candidate"


class RetrainIn(_Base):
    train_months: int = Field(default=24, ge=3, le=120)
    valid_months: int = Field(default=6, ge=1, le=24)
    test_months: int = Field(default=6, ge=1, le=24)
    step_months: int = Field(default=6, ge=1, le=24)
    promote: bool = True
    min_improvement: float = 0.0
    promote_metric: str = "test_rank_ic_mean"


class PredictIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    features: list[str] = Field(min_length=1, max_length=_MAX_FEATURES)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    version: int | None = Field(default=None, ge=1)
    start: str = Field(default="2024-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    universe: str = "all"
    persist: bool = True


class PromoteIn(BaseModel):
    stage: str = "production"
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------- 数据装载

def _resolve_universe(universe: str, as_of: str) -> tuple[list[str] | None, str | None]:
    """股票池 → (成分股清单, 口径降级说明)；all/None → (None, None)。

    口径与 ``server/api/factors._resolve_universe(as_of=...)`` 严格对齐：按
    **训练/推理区间起点**取当时已生效的成分快照（``IndexConsRepo.symbols_as_of``），
    而不是「今天的成分」。用最新快照回溯历史窗口是幸存者偏差：中途调入的牛股
    被事后追溯进样本、被调出的衰落股丢失，训练/预测指标系统性高估。ML 的
    ``start`` 默认 ``2024-01-01``，正是踩这个坑的典型场景。

    降级与披露也照搬 factors 侧语义（不另发明一套）：成分快照目前只同步最新
    一批，而请求起点早于首批快照是常态；此时退回该指数**现存最早一批**作为
    最接近的可得成分，并把替换显式回传（响应 ``universe_note`` / 任务结果），
    绝不静默冒充严格 point-in-time。只有一条快照都没有才 503。
    """
    if not universe or universe == "all":
        return None, None
    from lquant.data.store.catalog import IndexConsRepo
    from lquant.factors.universe import resolve_index_code

    try:
        as_of_d = date.fromisoformat(as_of)
    except ValueError as e:
        raise HTTPException(422, f"start 日期非法: {as_of!r}") from e
    code = resolve_index_code(universe)
    repo = IndexConsRepo()
    symbols = repo.symbols_as_of(code, as_of_d)
    if symbols:
        return symbols, None
    eff, fallback = repo.earliest_batch(code)
    if eff is None:
        raise HTTPException(
            503,
            f"指数 {code} 成分股为空（无任何已生效成分快照），"
            "先在数据页同步指数成分（index_cons）")
    return fallback, (
        f"成分口径替换：{as_of} 之前无已生效快照，改用现存最早一批 {eff} 的成分"
        "（该池非严格 point-in-time，幸存者偏差防护降级；要精确口径需按期累积成分快照）")


def _load(start: str, end: str | None, universe: str,
          note_out: dict | None = None) -> pl.DataFrame:
    """读日线面板。universe != all 时按 ``start`` 的 as-of 成分过滤。

    ``note_out`` 给了就回填 ``universe_note``（无降级时为 None）：口径被替换
    这件事必须走到调用方的响应/任务结果里，不能在数据装载层被吞掉。
    """
    symbols, note = _resolve_universe(universe, start)
    if note_out is not None:
        note_out["universe_note"] = note
    df = read_daily(start=start, end=end, symbols=symbols).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    return df


# ---------------------------------------------------------------- 任务体

def _attach_note(out: dict, note: str | None) -> dict:
    """把成分口径降级披露挂到响应/任务结果上（只在真降级时出现，同 factors 侧）。"""
    if note:
        out["universe_note"] = note
    return out


def _run_train_job(req: dict, progress=None, cancel_check=None) -> dict:
    """enqueue 任务体：一次性训练 + 注册版本。"""
    from lquant.research.ml.backtest import run_ml_pipeline

    if cancel_check is not None and cancel_check():
        from lquant.server.jobs import JobCanceled

        raise JobCanceled("训练已取消")
    if progress is not None:
        progress(done=0, total=3, phase="load", message="读取日线")
    note: dict = {}
    df = _load(req["start"], req.get("end"), req.get("universe", "all"),
               note_out=note)
    if progress is not None:
        progress(done=1, total=3, phase="train", message="训练中")
    out = run_ml_pipeline(
        df, req["features"], label_horizon=req["label_horizon"],
        train_end=req["train_end"], valid_end=req["valid_end"],
        kind=req["kind"], top_n=req["top_n"],
        processors=req.get("processors"),
        model_name=req.get("model_name"),
        stage=req.get("stage", "candidate"),
        note="api: /ml/train",
        **req.get("model_params", {}),
    )
    if progress is not None:
        progress(done=3, total=3, phase="done", message="完成")
    return _attach_note(
        {"ml_run_id": out.get("ml_run_id"), "model": out.get("model"),
         "ml": out.get("ml"), "dataset": out.get("dataset"),
         "backtest": out.get("backtest")},
        note.get("universe_note"))


def _run_retrain_job(req: dict, progress=None, cancel_check=None) -> dict:
    """enqueue 任务体：滚动重训 + 先验证再晋级。"""
    from lquant.research.ml.online import OnlineConfig, rolling_retrain

    note: dict = {}
    df = _load(req["start"], req.get("end"), req.get("universe", "all"),
               note_out=note)
    name = req.get("model_name") or _default_name(req)
    cfg = OnlineConfig(
        name=name, features=req["features"], label_horizon=req["label_horizon"],
        kind=req["kind"], processors=req.get("processors"), top_n=req["top_n"],
        train_months=req["train_months"], valid_months=req["valid_months"],
        test_months=req["test_months"], step_months=req["step_months"],
        promote_metric=req.get("promote_metric", "test_rank_ic_mean"),
        min_improvement=req.get("min_improvement", 0.0),
        model_params=req.get("model_params", {}),
    )
    out = rolling_retrain(df, cfg, promote=req.get("promote", True),
                          progress=progress, cancel_check=cancel_check)
    return _attach_note(out, note.get("universe_note"))


def _default_name(req: dict) -> str:
    h = req.get("label_horizon", 5)
    return f"ml_h{h}_top{req.get('top_n', 30)}"


# ---------------------------------------------------------------- 状态 / 自省

@router.get("/status")
def status_ep() -> dict:
    """ML 子系统状态：可用后端、版本统计、线上版本、最近信号。"""
    from lquant.research.ml.model import available_backends

    models = registry.list_models()
    names = sorted({m.name for m in models})
    prods = {}
    for n in names:
        p = registry.production(n)
        if p is not None:
            prods[n] = {"version": p.version,
                        "metric": (p.metrics or {}).get("ml", {}).get(
                            "test_rank_ic_mean")}
    signals: list[dict] = []
    try:
        with reader() as con:
            signals = [
                {"name": r[0], "days": r[1], "last": str(r[2]),
                 "version": r[3]}
                for r in con.execute(
                    "SELECT name, COUNT(DISTINCT trade_date), MAX(trade_date),"
                    " MAX(model_version) FROM ml_signal GROUP BY name").fetchall()
            ]
    except Exception:  # noqa: BLE001  表未建
        signals = []
    return {
        "backends": available_backends(),
        "model_lines": len(names),
        "versions": len(models),
        "by_stage": {s: sum(1 for m in models if m.stage == s)
                     for s in registry.STAGES},
        "production": prods,
        "signals": signals,
        "model_dir": str(registry.model_root()),
    }


@router.get("/features")
def features_ep(universe: str = "all", start: str = "2024-01-01",
                end: str | None = None, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """可用特征清单（湖列 + Alpha158 内置 + 简单公式），供前端选择器。"""
    from lquant.research.ml.panel import available_features

    note: dict = {}
    try:
        df = _load(start, end, universe, note_out=note)
        out = available_features(df, limit=limit)
    except HTTPException:
        # 湖空时仍给出内置因子与公式模板（前端选择器不至于空白）
        out = available_features(None, limit=limit)
    return _attach_note(out, note.get("universe_note"))


# ---------------------------------------------------------------- 训练

@router.post("/train", status_code=202)
def train_ep(req: TrainIn) -> dict:
    """提交一次性训练（异步）。返回 job_id。"""
    if req.valid_end <= req.train_end:
        raise HTTPException(422, "valid_end 必须晚于 train_end")
    params = req.model_dump()
    job = enqueue(_QUEUE, _run_train_job, params,
                  job_id=uuid.uuid4().hex[:12], name="ML 训练")
    return {"job_id": getattr(job, "id", None) or str(job)}


@router.post("/retrain", status_code=202)
def retrain_ep(req: RetrainIn) -> dict:
    """提交滚动重训（异步）。返回 job_id。"""
    params = req.model_dump()
    job = enqueue(_QUEUE, _run_retrain_job, params,
                  job_id=uuid.uuid4().hex[:12], name="ML 滚动重训")
    return {"job_id": getattr(job, "id", None) or str(job)}


@router.get("/jobs/{job_id}")
def job_ep(job_id: str) -> dict:
    from lquant.server.progress import get_progress

    j = get_job(job_id)
    if j is None:
        raise HTTPException(404, f"任务不存在: {job_id}")
    return {"job_id": job_id, "status": j.get_status(),
            "result": j.result if j.is_finished else None,
            "error": getattr(j, "error", None),
            "progress": get_progress(job_id)}


@router.post("/jobs/{job_id}/cancel")
def cancel_ep(job_id: str) -> dict:
    """请求取消 ML 任务。契约与 backtests/任务中心对齐：任务不存在 404，
    已终态 409，取消受理 200 —— 不存在的任务不能静默伪装成「已取消」。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"未找到 ML 任务 {job_id}")
    if job.get_status() in ("finished", "failed", "canceled"):
        raise HTTPException(409, f"任务已结束（{job.get_status()}），不可取消")
    if not request_cancel(job_id):
        raise HTTPException(409, f"任务不可取消: {job_id}")
    return {"job_id": job_id, "canceled": True}


# ---------------------------------------------------------------- 训练记录

@router.get("/runs")
def list_runs_ep(limit: int = Query(default=50, ge=1, le=500),
                 model_name: str | None = None,
                 stage: str | None = None) -> list[dict]:
    """训练记录列表（ml_run）。老库缺新列时降级为旧列子集，不 500。"""
    sql = ("SELECT run_id, model, model_name, model_version, stage,"
           " metrics, train_rows, test_rows, train_end, test_end, created_at,"
           " artifact_path FROM ml_run")
    where, args = [], []
    if model_name:
        where.append("model_name = ?")
        args.append(model_name)
    if stage:
        where.append("stage = ?")
        args.append(stage)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    try:
        with reader() as con:
            rows = con.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001  迁移未跑
        raise HTTPException(503, f"ml_run 表结构未迁移（重启服务会自动迁移）: {e}") from e
    return [{"run_id": r[0], "model": r[1], "model_name": r[2],
             "model_version": r[3], "stage": r[4],
             "metrics": _safe_json(r[5]), "train_rows": r[6], "test_rows": r[7],
             "train_end": str(r[8]) if r[8] else None,
             "test_end": str(r[9]) if r[9] else None,
             "created_at": str(r[10]) if r[10] else None,
             "artifact_path": r[11]} for r in rows]


@router.get("/runs/{run_id}")
def get_run_ep(run_id: str) -> dict:
    with reader() as con:
        row = con.execute(
            "SELECT run_id, model, params, features, metrics, train_rows, test_rows,"
            " train_end, test_end, created_at, model_name, model_version, stage,"
            " artifact_path, processor_path, processor_state, fit_window, dataset"
            " FROM ml_run WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        raise HTTPException(404, f"训练记录不存在: {run_id}")
    return {
        "run_id": row[0], "model": row[1], "params": _safe_json(row[2]),
        "features": _safe_json(row[3]), "metrics": _safe_json(row[4]),
        "train_rows": row[5], "test_rows": row[6],
        "train_end": str(row[7]) if row[7] else None,
        "test_end": str(row[8]) if row[8] else None,
        "created_at": str(row[9]) if row[9] else None,
        "model_name": row[10], "model_version": row[11], "stage": row[12],
        "artifact_path": row[13], "processor_path": row[14],
        "processor_state": _safe_json(row[15]), "fit_window": _safe_json(row[16]),
        "dataset": _safe_json(row[17]),
    }


# ---------------------------------------------------------------- 模型版本

@router.get("/models")
def list_models_ep(name: str | None = None, stage: str | None = None) -> list[dict]:
    return [m.as_dict() for m in registry.list_models(name, stage)]


@router.get("/models/{name}/production")
def production_ep(name: str, asof: str | None = Query(
        default=None, description="ISO 时刻；给了就重放事件流回答当时在线的版本")):
    """当前线上版本；``asof`` 给了就回答**当时**在线的版本（审计用）。"""
    mv = registry.production_asof(name, asof) if asof else registry.production(name)
    if mv is None:
        raise HTTPException(404, f"模型线 {name} 没有线上版本"
                                 + ("（或该时刻尚无线上版本）" if asof else ""))
    return mv.as_dict()


@router.post("/models/{name}/{version}/promote")
def promote_ep(name: str, version: int, req: PromoteIn) -> dict:
    from lquant.core.errors import MLError

    try:
        mv = registry.promote(name, version, req.stage, note=req.note or "api: promote")
    except MLError as e:
        raise HTTPException(404, str(e)) from e
    return mv.as_dict()


@router.post("/models/{name}/rollback")
def rollback_ep(name: str, note: str | None = None) -> dict:
    mv = registry.rollback(name, note=note or "api: rollback")
    if mv is None:
        raise HTTPException(409, f"模型线 {name} 没有可回滚的历史线上版本")
    return mv.as_dict()


@router.get("/models/{name}/events")
def events_ep(name: str) -> list[dict]:
    return registry.events(name)


# ---------------------------------------------------------------- 推理 / 信号

@router.post("/predict")
def predict_ep(req: PredictIn) -> dict:
    """用线上版本（或指定版本）产出信号并落库。

    同步执行：推理是单次前向，毫秒级；训练才需要异步。
    """
    from lquant.core.errors import MLError
    from lquant.research.ml.online import OnlineConfig, daily_inference

    note: dict = {}
    df = _load(req.start, req.end, req.universe, note_out=note)
    cfg = OnlineConfig(name=req.name, features=req.features)
    try:
        return _attach_note(
            daily_inference(df, cfg, date=req.date, version=req.version,
                            persist=req.persist),
            note.get("universe_note"))
    except MLError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/signals")
def signals_ep(name: str, start: str | None = None, end: str | None = None,
               version: int | None = None,
               limit: int = Query(default=500, ge=1, le=10000)) -> dict:
    """读已落库的模型信号（回测/监控用）。"""
    from lquant.research.ml.online import load_signals

    sig = load_signals(name, start=start, end=end, version=version)
    rows = sig.head(limit).to_dicts() if len(sig) else []
    for r in rows:
        r["trade_date"] = str(r["trade_date"])
    return {"name": name, "rows": len(sig), "items": rows,
            "versions": sorted(sig["model_version"].unique().to_list())
            if len(sig) else []}


def _safe_json(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, json.JSONDecodeError):
        return v


def _as_date(v: str | date | None) -> date | None:
    return date.fromisoformat(v) if isinstance(v, str) else v
