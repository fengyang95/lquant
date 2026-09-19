"""/qlib API：qlib 数据导出、workflow 配置与运行、历史结果。"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from lquant.qlib_io.export import ALL_FIELDS, check
from lquant.server.jobs import enqueue

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
