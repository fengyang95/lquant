"""/qlib API：qlib 数据导出、workflow 配置与运行、历史结果。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from lquant.qlib_io.export import ALL_FIELDS, check
from lquant.server.jobs import enqueue, request_cancel

router = APIRouter(prefix="/qlib", tags=["qlib"])

_QLIB_DATA_DIR = "data/qlib"
_CONFIG_DIR = Path("config/qlib")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

QLIB_RUNNER = Path(__file__).resolve().parents[2] / "qlib_io" / "runner.py"


class ExportIn(BaseModel):
    start: str | None = None
    end: str | None = None
    symbols: list[str] | None = None
    sec_types: list[str] | None = None
    fields: list[str] | None = None
    top: int | None = None

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: list[str] | None) -> list[str] | None:
        if v:
            bad = [f for f in v if f not in ALL_FIELDS]
            if bad:
                raise ValueError(f"未知字段：{bad}（可选：{list(ALL_FIELDS)}）")
        return v

    @field_validator("top")
    @classmethod
    def _top(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 5000):
            raise ValueError("top 需在 1..5000")
        return v


def _run_export_job(params: dict, progress=None, cancel_check=None) -> dict:
    """enqueue 任务体：导出 → 自检。"""
    from lquant.qlib_io import export as export_mod

    if progress:
        progress(done=0, total=2, phase="export", message="导出中")
    manifest = export_mod.export(_QLIB_DATA_DIR, **params)
    if progress:
        progress(done=1, total=2, phase="check", message="自检中")
    report = check(_QLIB_DATA_DIR)
    if progress:
        progress(done=2, total=2, phase="done", message="完成")
    return {"manifest": manifest, "check": report}


@router.get("/status")
def status_ep() -> dict:
    """qlib 数据目录状态：manifest + 日历范围。"""
    d = Path(_QLIB_DATA_DIR)
    out: dict = {"dir": _QLIB_DATA_DIR, "exists": d.exists()}
    if not d.exists():
        return out
    mf = d / "qlib_export_meta.json"
    out["manifest"] = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else None
    cal = d / "calendars" / "day.txt"
    if cal.exists():
        lines = cal.read_text(encoding="utf-8").split()
        out["calendar"] = {"start": lines[0], "end": lines[-1], "days": len(lines)}
    return out


@router.post("/export", status_code=202)
def export_ep(req: ExportIn) -> dict:
    params = {k: v for k, v in req.model_dump().items() if v is not None}
    job = enqueue("lquant-qlib", _run_export_job, params,
                  job_id=uuid.uuid4().hex[:12], name="Qlib 导出")
    return {"job_id": getattr(job, "id", None) or str(job)}


@router.get("/check")
def check_ep() -> dict:
    """同步自检（数据量不大时秒级）。"""
    d = Path(_QLIB_DATA_DIR)
    if not d.exists():
        raise HTTPException(404, f"qlib 数据目录不存在：{d}（先导出）")
    return check(d)


class ConfigIn(BaseModel):
    content: str


@router.get("/configs")
def list_configs_ep() -> list[dict]:
    out = []
    for p in sorted(_CONFIG_DIR.glob("*.yaml")):
        out.append({"name": p.stem, "path": str(p),
                    "mtime": p.stat().st_mtime})
    return out


@router.get("/configs/{name}")
def get_config_ep(name: str) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(422, f"非法配置名：{name!r}")
    p = _CONFIG_DIR / f"{name}.yaml"
    if not p.exists():
        raise HTTPException(404, f"配置不存在：{p}")
    return {"name": name, "content": p.read_text(encoding="utf-8")}


@router.put("/configs/{name}")
def put_config_ep(name: str, req: ConfigIn) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(422, f"非法配置名（含路径分隔符）：{name!r}")
    import yaml as _yaml

    try:
        doc = _yaml.safe_load(req.content)
    except _yaml.YAMLError as e:
        raise HTTPException(422, f"yaml 语法错误：{e}") from e
    if not isinstance(doc, dict) or "qlib_init" not in doc:
        raise HTTPException(422, "workflow 配置需为 dict 且含 qlib_init 键")
    p = _CONFIG_DIR / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    return {"name": name, "saved": True}


class WorkflowIn(BaseModel):
    config: str
    market: str | None = None
    exp_name: str = "lquant_qlib"


class CompareIn(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=2)


from lquant.qlib_io.interpreter import find_qlib_python  # noqa: E402

LOG_DIR = Path("data/qlib/runs")


def _log_path(run_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{run_id}.log"


def _metrics_out_path(run_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{run_id}_metrics.json"


_VENV_MISSING_MSG = (
    "当前环境没有 pyqlib。安装专用 venv：\n"
    "  python -m venv .venv-qlib\n"
    "  .venv-qlib/bin/pip install pyqlib lightgbm\n"
    "或设置 LQ_QLIB_PYTHON 指向已有解释器。"
)


def _run_workflow_job(run_id: str, config: str, market: str | None,
                      exp_name: str, progress=None,
                      cancel_check=None) -> dict:
    """enqueue 任务体：探测解释器 → 子进程跑 runner → 落库。"""
    from lquant.qlib_io import store

    store.update_run(run_id, status="running")
    py = find_qlib_python(None)
    if py == "":
        store.update_run(run_id, status="failed", error=_VENV_MISSING_MSG)
        return {"status": "failed"}

    log_p = _log_path(run_id)
    out_p = _metrics_out_path(run_id)
    runner_py = QLIB_RUNNER
    argv = [str(runner_py), "--provider", _QLIB_DATA_DIR,
            "--config", str(_CONFIG_DIR / f"{config}.yaml"),
            "--exp-name", exp_name, "--out", str(out_p)]
    if market:
        argv += ["--market", market]
    cmd = ([py] + argv) if py else ([sys.executable] + argv)
    if progress:
        progress(done=0, total=1, phase="workflow", message="训练中")
    try:
        with log_p.open("w", encoding="utf-8") as lf:
            rc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT).returncode
    except OSError as e:
        store.update_run(run_id, status="failed", error=str(e))
        return {"status": "failed"}
    if cancel_check and cancel_check():
        store.update_run(run_id, status="canceled")
        return {"status": "canceled"}
    if rc != 0:
        tail = log_p.read_text(encoding="utf-8", errors="replace")[-2000:]
        store.update_run(run_id, status="failed", error=tail, log_path=str(log_p))
        return {"status": "failed"}
    metrics = json.loads(out_p.read_text(encoding="utf-8")) if out_p.exists() else {}
    store.update_run(run_id, status="finished", metrics=metrics, log_path=str(log_p))
    if progress:
        progress(done=1, total=1, phase="done", message="完成")
    return {"status": "finished"}


@router.post("/workflow", status_code=202)
def workflow_ep(req: WorkflowIn) -> dict:
    if not _NAME_RE.match(req.config):
        raise HTTPException(422, f"非法配置名：{req.config!r}")
    cfg = _CONFIG_DIR / f"{req.config}.yaml"
    if not cfg.exists():
        raise HTTPException(404, f"workflow 配置不存在：{cfg}")
    if not Path(_QLIB_DATA_DIR).exists():
        raise HTTPException(422, f"qlib 数据目录不存在：{_QLIB_DATA_DIR}（先导出）")
    if find_qlib_python(None) == "":
        raise HTTPException(422, _VENV_MISSING_MSG)
    snapshot = cfg.read_text(encoding="utf-8")
    from lquant.qlib_io import store

    run = store.create_run(f"{req.config}.yaml", req.market, req.exp_name, snapshot)
    enqueue("lquant-qlib", _run_workflow_job, run["id"], req.config,
            req.market, req.exp_name,
            job_id=f"qlibwf-{run['id']}", name="Qlib 工作流")
    return {"run_id": run["id"]}


@router.get("/runs")
def list_runs_ep(limit: int = Query(default=50, ge=1, le=500),
                 status: str | None = None) -> list[dict]:
    from lquant.qlib_io import store

    return store.list_runs(limit=limit, status=status)


@router.get("/runs/{run_id}")
def get_run_ep(run_id: str) -> dict:
    from lquant.qlib_io import store

    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"运行不存在：{run_id}")
    if run.get("log_path"):
        lp = Path(run["log_path"])
        run["log_tail"] = (lp.read_text(encoding="utf-8", errors="replace")[-5000:]
                           if lp.exists() else None)
    return run


@router.post("/runs/{run_id}/cancel")
def cancel_run_ep(run_id: str) -> dict:
    from lquant.qlib_io import store

    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"运行不存在：{run_id}")
    if run["status"] not in ("queued", "running"):
        raise HTTPException(409, f"运行已结束（{run['status']}）")
    if not request_cancel(f"qlibwf-{run_id}"):
        raise HTTPException(409, "任务当前不可取消")
    store.update_run(run_id, status="canceled")
    return {"run_id": run_id, "canceled": True}


@router.post("/runs/compare")
def compare_ep(req: CompareIn) -> dict:
    from lquant.qlib_io import store

    runs = [store.get_run(i) for i in req.ids]
    missing = [i for i, r in zip(req.ids, runs, strict=True) if r is None]
    if missing:
        raise HTTPException(404, f"运行不存在：{missing}")
    keys: set[str] = set()
    for r in runs:
        keys |= set(r.get("metrics") or {})
    rows = [{"metric": k,
             **{r["id"]: (r.get("metrics") or {}).get(k) for r in runs}}
            for k in sorted(keys)]
    return {"runs": [{"id": r["id"], "config": r["config"],
                      "exp_name": r["exp_name"],
                      "status": r["status"]} for r in runs],
            "rows": rows}
