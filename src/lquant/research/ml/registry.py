"""模型注册表：训练记录 ↔ 模型文件 ↔ 在线版本，三者必须能互相定位。

**没有这层会出现什么**（Phase 2 之前的真实状态）：

- ``ml_run`` 表里有 IC/回测指标，但**没有 artifact 列** → 「这条记录对应哪个
  模型文件」无从回答，训练记录不可回放；
- 没有版本概念 → 滚动重训覆盖文件后，无法回答「上周三线上跑的是哪一版」；
- 没有 stage/事件流 → 只能靠改文件名区分候选与线上，回滚靠手抖。

借 qlib ``Recorder`` + ``save_objects`` 的语义（记录与对象一起存、可按
experiment/recorder 取回），但不引 MLflow（见 ADR-11 与 00-borrow-and-port-plan
的「明确不借鉴」表）。

## 四个 stage

``candidate`` → ``staging`` → ``production`` → ``archived``

``production`` 在同一 ``name`` 下**至多一个**：晋级时把原来的线上版降级为
``archived``，同事务写 ``ml_model_event``。事件流是 ``production_asof`` 的唯一
依据 —— 「任意历史日期当时在用的是哪一版」靠**重放事件**回答，而不是靠猜。

## 存储布局

    <model_dir>/<name>/v<version>/model.pkl       模型（pickle，与 Model.save 同构）
    <model_dir>/<name>/v<version>/processor.json  特征处理器状态（复用同一份参数）
    <model_dir>/<name>/v<version>/meta.json       训练记录摘要（便于脱离库排查）

``model_dir`` 默认 ``data/models``，可用 ``LQ_MODEL_DIR`` 覆盖。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from lquant.core.db import reader, writer
from lquant.core.errors import MLError
from lquant.core.types import now_cn

__all__ = [
    "STAGES", "PRODUCTION", "ModelVersion", "model_root", "model_paths",
    "save_artifact", "register_run", "list_models", "get_model", "load_model",
    "promote", "rollback", "production", "production_asof", "archive", "events",
]

#: 版本生命周期。顺序即「从训练出来到下线」的自然过程。
STAGES: tuple[str, ...] = ("candidate", "staging", "production", "archived")
PRODUCTION = "production"
_TERMINAL_STAGES = ("production", "archived")


def model_root() -> Path:
    """模型存储根目录（``LQ_MODEL_DIR`` 缺省 ``data/models``，相对仓库根）。"""
    import os

    from lquant.core.config import get_settings

    env = os.environ.get("LQ_MODEL_DIR")
    if env:
        return Path(env)
    return Path(get_settings().root) / "data" / "models"


def _rel(p: Path) -> str:
    """落库一律存**相对仓库根**的路径：换机器/换挂载点后仍能定位。

    绝对路径在开发机上能跑，一进容器或换 worktree 就成死链 —— 这是
    「记录看起来还在、模型文件找不到」的典型成因。
    """
    from lquant.core.config import get_settings

    root = Path(get_settings().root)
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def _abs(p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p)
    if path.is_absolute():
        return path
    from lquant.core.config import get_settings

    return Path(get_settings().root) / path


def model_paths(name: str, version: int) -> dict[str, Path]:
    d = model_root() / name / f"v{version}"
    return {"dir": d, "model": d / "model.pkl",
            "processor": d / "processor.json", "meta": d / "meta.json"}


@dataclass
class ModelVersion:
    name: str
    version: int
    run_id: str | None = None
    stage: str = "candidate"
    artifact_path: str | None = None
    processor_path: str | None = None
    metrics: dict = field(default_factory=dict)
    fit_window: dict = field(default_factory=dict)
    note: str | None = None
    created_at: datetime | None = None
    promoted_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "run_id": self.run_id,
            "stage": self.stage, "artifact_path": self.artifact_path,
            "processor_path": self.processor_path, "metrics": self.metrics,
            "fit_window": self.fit_window, "note": self.note,
            "created_at": str(self.created_at) if self.created_at else None,
            "promoted_at": str(self.promoted_at) if self.promoted_at else None,
        }


# ---------------------------------------------------------------- 落盘

def save_artifact(name: str, version: int, model, processor=None) -> dict[str, Path]:
    """把模型与处理器状态写进 ``<model_dir>/<name>/v<version>/``。

    两者必须**一起**存：只有模型没有处理器状态，推理时就得用推理段的统计量
    重算标准化参数 —— 那是泄漏（见 ``research/ml/processor.py`` 的纪律）。
    """
    paths = model_paths(name, version)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    model.save(paths["model"])
    if processor is not None:
        paths["processor"].write_text(
            json.dumps(processor.state(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
    return paths


def load_processor(path: str | Path | None):
    """按落盘的处理器状态重建处理器；无文件/无 name 返回 None。

    重建而不是重算：状态里带着训练段的均值/标准差/截断界，推理时必须原样复用。
    """
    p = _abs(str(path)) if path else None
    if p is None or not p.exists():
        return None
    state = json.loads(Path(p).read_text(encoding="utf-8"))
    name = state.get("name")
    from lquant.research.ml.processor import make_processor

    if name == "pipeline":
        from lquant.research.ml.processor import Pipeline

        # pipeline 的 state 里带子处理器名与状态，逐个还原
        steps = state.get("steps", [])
        procs = []
        for s in steps:
            sub = make_processor(s.get("name", ""))
            sub.load_state(s.get("state", {}))
            procs.append(sub)
        pipe = Pipeline(procs) if procs else Pipeline([make_processor("clip")])
        pipe.features = list(state.get("features", []))
        pipe.state_ = dict(state)
        pipe._fitted = True
        return pipe
    proc = make_processor(name or "")
    proc.load_state(state)
    return proc


# ---------------------------------------------------------------- 注册

def _next_version(con, name: str) -> int:
    row = con.execute("SELECT COALESCE(MAX(version), 0) FROM ml_model WHERE name = ?",
                      [name]).fetchone()
    return int(row[0]) + 1


def register_run(
    *,
    run_id: str,
    name: str,
    model,
    metrics: dict,
    params: dict | None = None,
    features: list[str] | None = None,
    processor=None,
    fit_window: dict | None = None,
    dataset: dict | None = None,
    train_rows: int = 0,
    test_rows: int = 0,
    train_end=None,
    test_end=None,
    stage: str = "candidate",
    note: str | None = None,
) -> ModelVersion:
    """把一次训练注册进注册表：分配版本 → 落 artifact → 写 ml_run + ml_model。

    版本号与线上状态的写库在同一事务里完成（进程内写锁由 ``core.db.writer``
    提供），避免两个并发训练领到同一个版本号。
    """
    if stage not in STAGES:
        raise MLError(f"未知 stage {stage!r}，可选: {list(STAGES)}")
    now = now_cn().replace(tzinfo=None)
    with writer() as con:
        version = _next_version(con, name)
        paths = save_artifact(name, version, model, processor)
        pstate = processor.state() if processor is not None else None
        if pstate is not None:
            paths["meta"].write_text(json.dumps({
                "name": name, "version": version, "run_id": run_id,
                "model": getattr(model, "name", None), "features": features,
                "metrics": metrics, "fit_window": fit_window,
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        con.execute(
            "INSERT OR REPLACE INTO ml_run (run_id, model, params, features, metrics,"
            " train_rows, test_rows, train_end, test_end, created_at, model_name,"
            " model_version, stage, artifact_path, processor_path, processor_state,"
            " fit_window, dataset) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, getattr(model, "name", None), json.dumps(params or {}, default=str),
             json.dumps(features or []), json.dumps(metrics, default=str),
             int(train_rows), int(test_rows),
             str(train_end) if train_end else None,
             str(test_end) if test_end else None, now,
             name, version, stage, _rel(paths["model"]),
             _rel(paths["processor"]) if processor is not None else None,
             json.dumps(pstate, default=str) if pstate else None,
             json.dumps(fit_window or {}, default=str),
             json.dumps(dataset or {}, default=str)],
        )
        con.execute(
            "INSERT OR REPLACE INTO ml_model (name, version, run_id, stage,"
            " artifact_path, processor_path, metrics, fit_window, note, created_at,"
            " promoted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [name, version, run_id, stage, _rel(paths["model"]),
             _rel(paths["processor"]) if processor is not None else None,
             json.dumps(metrics, default=str), json.dumps(fit_window or {}, default=str),
             note, now, now if stage in _TERMINAL_STAGES else None],
        )
        _record_event(con, name, version, None, stage, note, now)
    return ModelVersion(name=name, version=version, run_id=run_id, stage=stage,
                        artifact_path=_rel(paths["model"]),
                        processor_path=(_rel(paths["processor"])
                                        if processor is not None else None),
                        metrics=metrics or {}, fit_window=fit_window or {},
                        note=note, created_at=now, promoted_at=now)


def _record_event(con, name: str, version: int, from_stage: str | None,
                  to_stage: str, note: str | None, at: datetime) -> None:
    con.execute(
        "INSERT INTO ml_model_event (event_id, name, version, from_stage, to_stage,"
        " note, occurred_at) VALUES (?,?,?,?,?,?,?)",
        [uuid.uuid4().hex[:12], name, version, from_stage, to_stage, note, at])


# ---------------------------------------------------------------- 查询

def _row_to_version(r) -> ModelVersion:
    return ModelVersion(
        name=r[0], version=int(r[1]), run_id=r[2], stage=r[3],
        artifact_path=r[4], processor_path=r[5],
        metrics=json.loads(r[6]) if r[6] else {},
        fit_window=json.loads(r[7]) if r[7] else {},
        note=r[8], created_at=r[9], promoted_at=r[10],
    )


_SELECT = ("SELECT name, version, run_id, stage, artifact_path, processor_path,"
           " metrics, fit_window, note, created_at, promoted_at FROM ml_model")


def list_models(name: str | None = None, stage: str | None = None) -> list[ModelVersion]:
    """列版本（按 name、version 升序）。表不存在时返回 []（不炸 API）。"""
    sql = _SELECT
    where, args = [], []
    if name:
        where.append("name = ?")
        args.append(name)
    if stage:
        where.append("stage = ?")
        args.append(stage)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name, version"
    try:
        with reader() as con:
            rows = con.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001  迁移未跑/表缺失
        logger.debug(f"ml_model 查询失败（表未建？）: {e}")
        return []
    return [_row_to_version(r) for r in rows]


def get_model(name: str, version: int | None = None) -> ModelVersion | None:
    """取某个版本；``version=None`` 时取**当前 production**。"""
    if version is None:
        return production(name)
    rows = [v for v in list_models(name) if v.version == version]
    return rows[0] if rows else None


def load_model(name: str, version: int | None = None):
    """载入 (model, processor|None)。版本不存在或文件缺失 → MLError。

    模型与处理器必须来自**同一版本目录** —— 混用版本等于用 A 的训练统计量
    去解释 B 的模型，产出的是垃圾且不报错。
    """
    from lquant.research.ml.model import Model

    mv = get_model(name, version)
    if mv is None:
        raise MLError(f"模型 {name} v{version} 不存在（注册表里没有该版本）")
    p = _abs(mv.artifact_path)
    if p is None or not p.exists():
        raise MLError(f"模型文件缺失: {mv.artifact_path}（版本记录在但文件没了）")
    model = Model.load(p)
    proc = load_processor(mv.processor_path)
    return model, proc


# ---------------------------------------------------------------- 版本状态

def promote(name: str, version: int, stage: str, note: str | None = None) -> ModelVersion:
    """把某版本置为目标 stage；晋 production 时原线上版自动 archived（至多一个）。

    同事务完成「降级旧的 + 升级新的 + 记两条事件」，避免中途崩溃留下
    「两个 production」这种需要人工猜的脏状态。
    """
    if stage not in STAGES:
        raise MLError(f"未知 stage {stage!r}，可选: {list(STAGES)}")
    now = now_cn().replace(tzinfo=None)
    with writer() as con:
        row = con.execute("SELECT stage FROM ml_model WHERE name=? AND version=?",
                          [name, version]).fetchone()
        if row is None:
            raise MLError(f"模型 {name} v{version} 不在注册表里，无法晋级")
        from_stage = row[0]
        if stage == PRODUCTION:
            old = con.execute(
                "SELECT version FROM ml_model WHERE name=? AND stage='production'"
                " AND version<>?", [name, version]).fetchall()
            for (ov,) in old:
                con.execute(
                    "UPDATE ml_model SET stage='archived', note=?, promoted_at=?"
                    " WHERE name=? AND version=?", [note, now, name, ov])
                _record_event(con, name, int(ov), PRODUCTION, "archived", note, now)
        con.execute("UPDATE ml_model SET stage=?, note=?, promoted_at=? WHERE name=?"
                    " AND version=?", [stage, note, now, name, version])
        _record_event(con, name, version, from_stage, stage, note, now)
    out = get_model(name, version)
    assert out is not None
    return out


def rollback(name: str, note: str | None = None) -> ModelVersion | None:
    """回滚：把**上一个进入过 production** 的版本重新置为线上。

    「上一个」由事件流的时间顺序决定（只收集进入 production 的事件），而不是
    按 version 大小倒推 —— 手动 pin 过旧版之后，version 大小与上线次序无关。
    历史上线记录里去掉当前版本后取最后一个，就是真正的「上一版」。
    无历史可回滚时返回 None（调用方据此决定是报错还是保持现状）。
    """
    entered = [e["version"] for e in events(name) if e["to_stage"] == PRODUCTION]
    current = production(name)
    cur_v = current.version if current else None
    prev = [v for v in entered if v != cur_v]
    if not prev:
        return None
    target = prev[-1]
    return promote(name, target, PRODUCTION,
                   note=note or f"rollback from v{cur_v} to v{target}")


def archive(name: str, version: int, note: str | None = None) -> ModelVersion:
    return promote(name, version, "archived", note=note)


def production(name: str) -> ModelVersion | None:
    rows = [v for v in list_models(name) if v.stage == PRODUCTION]
    if not rows:
        return None
    if len(rows) > 1:
        # 至多一个的不变量被破坏（手改库/并发写）：取版本号最大者，并告警。
        logger.warning(f"模型 {name} 有 {len(rows)} 个 production 版本，取最大版本号")
        rows.sort(key=lambda v: v.version)
    return rows[-1]


def events(name: str | None = None) -> list[dict]:
    """按时间升序的事件流（as-of 回放的依据）。"""
    sql = ("SELECT event_id, name, version, from_stage, to_stage, note, occurred_at"
           " FROM ml_model_event")
    args: list[Any] = []
    if name:
        sql += " WHERE name = ?"
        args.append(name)
    sql += " ORDER BY occurred_at, version"
    try:
        with reader() as con:
            rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"event_id": r[0], "name": r[1], "version": int(r[2]),
             "from_stage": r[3], "to_stage": r[4], "note": r[5], "occurred_at": r[6]}
            for r in rows]


def production_asof(name: str, when) -> ModelVersion | None:
    """**重放事件流**回答「``when`` 时刻线上跑的是哪一版」。

    直接读当前 ``stage`` 是错的：它只反映现在。回放把每个版本的状态推演到
    ``when`` 为止，取当时处于 production 的那一版 —— 这是事后审计
    「某天的信号是哪个模型出的」的唯一可靠依据。
    """
    at = when
    if isinstance(when, str):
        at = datetime.fromisoformat(when)
    stages: dict[int, str] = {}
    for e in events(name):
        if e["occurred_at"] is None or e["occurred_at"] > at:
            continue
        stages[e["version"]] = e["to_stage"]
    prod = [v for v, s in stages.items() if s == PRODUCTION]
    if not prod:
        return None
    prod.sort()
    return get_model(name, prod[-1])
