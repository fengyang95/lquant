# Qlib 前端接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 qlib 能力（数据导出、工作流运行、历史结果对比、配置管理）接入平台后端 API 与前端页面。

**Architecture:** 后端新增 `qlib_io/store.py`（sqlite 元数据）+ `server/api/qlib.py`（/qlib 路由），工作流经 `lquant-qlib` 队列异步执行（复用 jobs/progress 体系）；CLI 的 qlib 解释器探测抽为公共函数。前端在 /data、/factors、/backtests 三页融入 qlib 区块，任务中心新增 kind=qlib。

**Tech Stack:** FastAPI + pydantic + sqlite；Next.js + TypeScript + vitest。

**Spec:** `docs/superpowers/specs/2026-09-20-qlib-frontend-design.md`

## Global Constraints

- 覆盖率增量门禁 ≥95%：新代码必须带测试（`PYTHONPATH=src` + 主仓 venv 跑 pytest；worktree 缺 data 符号链接时先 `rm -rf data && ln -s /Users/lyp/code/lquant/data data`）。
- pyqlib 不进主 venv：子进程调用，探测顺序 = 显式 python → `LQ_QLIB_PYTHON` → 进程内 import → `.venv-qlib`。
- API 统一走 `server/envelope.py` 既有约定；写操作 422 fail-fast。
- 不破坏现有 CLI 行为（`lq qlib …`）。
- 后端新文件过 ruff：`ruff check <file>`（基线违规不碰）。
- 前端测试 `cd web && npx vitest run <file>`。
- commit 用 conventional 格式，不加 attribution。

---

### Task 1: qlib 解释器探测抽公共函数

**Files:**
- Create: `src/lquant/qlib_io/interpreter.py`
- Modify: `src/lquant/cli/commands/qlib.py`（`_find_qlib_python` 改为转发）
- Test: `tests/unit/test_qlib_interpreter.py`

**Interfaces:**
- Produces: `find_qlib_python(python: str | None = None) -> str | None`（None=当前解释器可用；""=找不到；否则 venv python 路径）

- [ ] **Step 1: 写失败测试**

```python
"""qlib 解释器探测公共函数测试。"""
from unittest import mock

from lquant.qlib_io.interpreter import find_qlib_python


def test_explicit_python_wins(monkeypatch, tmp_path):
    p = tmp_path / "py"
    p.write_text("")
    assert find_qlib_python(str(p)) == str(p)


def test_env_var(monkeypatch, tmp_path):
    p = tmp_path / "envpy"
    p.write_text("")
    monkeypatch.setenv("LQ_QLIB_PYTHON", str(p))
    assert find_qlib_python(None) == str(p)


def test_inprocess_import(monkeypatch):
    monkeypatch.delenv("LQ_QLIB_PYTHON", raising=False)
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "qlib":
            return mock.MagicMock()
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert find_qlib_python(None) is None


def test_not_found(monkeypatch, tmp_path, monkeypatch_chdir):
    monkeypatch.delenv("LQ_QLIB_PYTHON", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_qlib_python(None) == ""
```

注意 `monkeypatch_chdir` 不存在 —— 直接用 `monkeypatch.chdir(tmp_path)`，最后一个用例删掉该 fixture 参数。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_qlib_interpreter.py -v`
Expected: FAIL（ModuleNotFoundError: lquant.qlib_io.interpreter）

- [ ] **Step 3: 实现**

```python
"""qlib 解释器探测（CLI 与 server 共用）。"""
from __future__ import annotations

import os
from pathlib import Path


def find_qlib_python(python: str | None = None) -> str | None:
    """定位可用 qlib 解释器。None=当前解释器可 import qlib；""=找不到。"""
    if python:
        return python
    env_py = os.environ.get("LQ_QLIB_PYTHON")
    if env_py and Path(env_py).exists():
        return env_py
    try:
        import qlib  # noqa: F401
        return None
    except ImportError:
        pass
    for cand in (".venv-qlib/bin/python", ".venv-qlib/Scripts/python.exe"):
        p = Path(cand)
        if p.exists():
            return str(p)
    return ""
```

- [ ] **Step 4: CLI 转发**

`src/lquant/cli/commands/qlib.py`：`_find_qlib_python` 改为：

```python
def _find_qlib_python(python: str | None) -> str | None:
    from lquant.qlib_io.interpreter import find_qlib_python
    return find_qlib_python(python)
```

- [ ] **Step 5: 跑测试通过 + CLI 既有测试不回归**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_qlib_interpreter.py tests/unit/test_qlib_export.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/lquant/qlib_io/interpreter.py tests/unit/test_qlib_interpreter.py src/lquant/cli/commands/qlib.py
git commit -m "refactor(qlib): 解释器探测抽公共函数 find_qlib_python"
```

---

### Task 2: qlib_run 元数据存储

**Files:**
- Create: `src/lquant/qlib_io/store.py`
- Test: `tests/unit/test_qlib_store.py`

**Interfaces:**
- Produces:
  - `create_run(config: str, market: str | None, exp_name: str, config_snapshot: str) -> dict`
  - `update_run(run_id: str, *, status: str | None = None, metrics: dict | None = None, log_path: str | None = None, error: str | None = None) -> None`
  - `get_run(run_id: str) -> dict | None`
  - `list_runs(limit: int = 50, status: str | None = None) -> list[dict]`
  - run dict 键：`id, config, market, exp_name, status, metrics(dict|None), config_snapshot, log_path, created_at, finished_at, error`

- [ ] **Step 1: 写失败测试**

```python
"""qlib_run sqlite 存储测试。"""
import pytest

from lquant.qlib_io import store


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "qlib.db"))
    yield


def test_create_and_get():
    r = store.create_run("config/qlib/a.yaml", "top300", "exp1", "yaml-text")
    assert r["status"] == "queued" and r["metrics"] is None
    assert store.get_run(r["id"])["config"] == "config/qlib/a.yaml"


def test_update_lifecycle():
    r = store.create_run("a.yaml", None, "exp1", "y")
    store.update_run(r["id"], status="running")
    store.update_run(r["id"], status="finished",
                     metrics={"IC": 0.03}, log_path="/tmp/log.txt")
    got = store.get_run(r["id"])
    assert got["status"] == "finished"
    assert got["metrics"] == {"IC": 0.03}
    assert got["finished_at"]


def test_update_failed_sets_error():
    r = store.create_run("a.yaml", None, "exp", "y")
    store.update_run(r["id"], status="failed", error="boom")
    assert store.get_run(r["id"])["error"] == "boom"


def test_list_runs_filter_and_order():
    a = store.create_run("a.yaml", None, "e", "y")
    b = store.create_run("b.yaml", None, "e", "y")
    store.update_run(a["id"], status="finished")
    assert [x["id"] for x in store.list_runs(status="finished")] == [a["id"]]
    ids = [x["id"] for x in store.list_runs()]
    assert set(ids) == {a["id"], b["id"]}


def test_get_missing_none():
    assert store.get_run("nope") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_qlib_store.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**

```python
"""qlib 运行元数据存储（sqlite，独立于 DuckDB 主库，模式参照 paper/store.py）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qlib_run(
  id TEXT PRIMARY KEY,
  config TEXT NOT NULL,
  market TEXT,
  exp_name TEXT NOT NULL,
  status TEXT NOT NULL,
  metrics TEXT,
  config_snapshot TEXT,
  log_path TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
"""


def db_path() -> Path:
    env = os.getenv("LQ_QLIB_DB")
    if env:
        p = Path(env)
    else:
        from lquant.core.config import get_settings
        p = Path(get_settings().parquet_dir).parent / "qlib" / "qlib_runs.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
    return d


def create_run(config: str, market: str | None, exp_name: str,
               config_snapshot: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO qlib_run(id, config, market, exp_name, status,"
            " config_snapshot, created_at) VALUES(?,?,?,?,?,?,?)",
            (rid, config, market, exp_name, "queued", config_snapshot, _now()))
    return get_run(rid)


def update_run(run_id: str, *, status: str | None = None,
               metrics: dict | None = None, log_path: str | None = None,
               error: str | None = None) -> None:
    sets, vals = [], []
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if metrics is not None:
        sets.append("metrics=?"); vals.append(json.dumps(metrics, ensure_ascii=False))
    if log_path is not None:
        sets.append("log_path=?"); vals.append(log_path)
    if error is not None:
        sets.append("error=?"); vals.append(error)
    if status in ("finished", "failed", "canceled"):
        sets.append("finished_at=?"); vals.append(_now())
    if not sets:
        return
    vals.append(run_id)
    with _LOCK, _conn() as c:
        cur = c.execute(f"UPDATE qlib_run SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            raise ValueError(f"qlib_run 不存在: {run_id}")


def get_run(run_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM qlib_run WHERE id=?", (run_id,)).fetchone()
    return _to_dict(row) if row else None


def list_runs(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM qlib_run"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _LOCK, _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [_to_dict(r) for r in rows]
```

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_qlib_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/qlib_io/store.py tests/unit/test_qlib_store.py
git commit -m "feat(qlib): qlib_run 运行元数据 sqlite 存储"
```

---

### Task 3: /qlib status + export 端点

**Files:**
- Create: `src/lquant/server/api/qlib.py`
- Test: `tests/unit/test_server_qlib_api.py`

**Interfaces:**
- Consumes: `qlib_io.export.export / check`，`server.jobs.enqueue`（fn 带 `progress`/`cancel_check` 形参自动注入）
- Produces: router `qlib.router`（prefix `/qlib`）；后续任务在同一文件追加端点。

- [ ] **Step 1: 写失败测试**

```python
"""server /qlib API 测试。"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from lquant.server.api import qlib as qlib_api
from lquant.server.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def test_status_missing_dir(client, tmp_path, monkeypatch):
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(tmp_path / "nope"))
    r = client.get("/api/qlib/status")
    assert r.status_code == 200
    assert r.json()["exists"] is False


def test_status_with_manifest(client, tmp_path, monkeypatch):
    d = tmp_path / "qlib"
    d.mkdir()
    (d / "qlib_export_meta.json").write_text('{"symbols": 10}', encoding="utf-8")
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(d))
    body = client.get("/api/qlib/status").json()
    assert body["exists"] is True
    assert body["manifest"] == {"symbols": 10}


def test_export_enqueue(client, monkeypatch):
    calls = {}

    def fake_enqueue(queue, fn, *a, **k):
        calls["queue"] = queue
        return {"id": "job1"}

    monkeypatch.setattr(qlib_api, "enqueue", fake_enqueue)
    r = client.post("/api/qlib/export", json={"top": 300})
    assert r.status_code == 202
    assert calls["queue"] == "lquant-qlib"


def test_export_invalid_field(client):
    r = client.post("/api/qlib/export", json={"fields": ["bogus"]})
    assert r.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v`
Expected: FAIL（ModuleNotFoundError: lquant.server.api.qlib）

- [ ] **Step 3: 实现 qlib.py（本任务部分）**

```python
"""/qlib API：qlib 数据导出、workflow 配置与运行、历史结果。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
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
```

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/server/api/qlib.py tests/unit/test_server_qlib_api.py
git commit -m "feat(qlib): /qlib status/export/check API"
```

---

### Task 4: workflow 配置管理端点

**Files:**
- Modify: `src/lquant/server/api/qlib.py`
- Test: `tests/unit/test_server_qlib_api.py`（追加）

**Interfaces:**
- Produces: `GET /qlib/configs`、`GET /qlib/configs/{name}`、`PUT /qlib/configs/{name}`

- [ ] **Step 1: 写失败测试（追加到 test_server_qlib_api.py）**

```python
def test_configs_list(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    (tmp_path / "wf_a.yaml").write_text("qlib_init: {}\n", encoding="utf-8")
    names = [c["name"] for c in client.get("/api/qlib/configs").json()]
    assert "wf_a" in names


def test_config_get_put(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    r = client.put("/api/qlib/configs/wf_new",
                   json={"content": "qlib_init: {provider_uri: data/qlib}\n"})
    assert r.status_code == 200
    assert "provider_uri" in client.get("/api/qlib/configs/wf_new").json()["content"]


def test_config_put_invalid_yaml(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    r = client.put("/api/qlib/configs/wf_bad",
                   json={"content": "a: [unclosed\n"})
    assert r.status_code == 422


def test_config_put_path_traversal(client):
    assert client.put("/api/qlib/configs/../evil",
                      json={"content": "x: 1\n"}).status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v -k config`
Expected: FAIL（404）

- [ ] **Step 3: 实现（追加到 qlib.py）**

```python
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
        raise HTTPException(422, f"非法配置名：{name!r}")
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
```

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/server/api/qlib.py tests/unit/test_server_qlib_api.py
git commit -m "feat(qlib): workflow 配置读取/保存 API"
```

---

### Task 5: workflow 运行 + runs/cancel/compare

**Files:**
- Modify: `src/lquant/server/api/qlib.py`
- Test: `tests/unit/test_server_qlib_api.py`（追加）

**Interfaces:**
- Consumes: Task 1 `find_qlib_python`、Task 2 store、`qlib_io.runner.main`
- Produces: `POST /qlib/workflow`、`GET /qlib/runs`、`GET /qlib/runs/{id}`、`POST /qlib/runs/{id}/cancel`、`POST /qlib/runs/compare`

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_workflow_precheck_no_data(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(tmp_path / "nope"))
    r = client.post("/api/qlib/workflow",
                    json={"config": "wf_a", "exp_name": "e1"})
    assert r.status_code == 422


def test_workflow_enqueue_and_run_lifecycle(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(tmp_path / "qlib"))
    (tmp_path / "qlib").mkdir()
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    (tmp_path / "wf_a.yaml").write_text("qlib_init: {}\n", encoding="utf-8")
    from lquant.qlib_io import store
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    # 预探测：venv 缺失 → 422
    monkeypatch.setattr(qlib_api, "find_qlib_python", lambda py=None: "")
    r = client.post("/api/qlib/workflow", json={"config": "wf_a", "exp_name": "e1"})
    assert r.status_code == 422
    # venv 可用 → 202
    monkeypatch.setattr(qlib_api, "find_qlib_python", lambda py=None: "fakepy")
    r2 = client.post("/api/qlib/workflow", json={"config": "wf_a", "exp_name": "e1"})
    assert r2.status_code == 202
    rid = r2.json()["run_id"]
    assert store.get_run(rid)["status"] == "queued"


def test_run_job_success(monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    from lquant.qlib_io import store
    from lquant.server.api.qlib import _run_workflow_job
    r = store.create_run("wf_a", None, "e1", "yaml")
    log = tmp_path / "run.log"
    monkeypatch.setattr("lquant.server.api.qlib.subprocess.run",
                        lambda *a, **k: mock.Mock(returncode=0))
    metrics_path = tmp_path / "m.json"
    metrics_path.write_text('{"IC": 0.03}', encoding="utf-8")
    monkeypatch.setattr("lquant.server.api.qlib._metrics_out_path",
                        lambda rid: metrics_path)
    monkeypatch.setattr("lquant.server.api.qlib._log_path",
                        lambda rid: log)
    _run_workflow_job(r["id"], "wf_a", None, "e1", None)
    got = store.get_run(r["id"])
    assert got["status"] == "finished" and got["metrics"] == {"IC": 0.03}


def test_run_job_venv_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    from lquant.qlib_io import store
    from lquant.server.api.qlib import _run_workflow_job
    r = store.create_run("wf_a", None, "e1", "yaml")
    monkeypatch.setattr("lquant.server.api.qlib.find_qlib_python", lambda py=None: "")
    _run_workflow_job(r["id"], "wf_a", None, "e1", None)
    got = store.get_run(r["id"])
    assert got["status"] == "failed"
    assert "pyqlib" in got["error"]


def test_runs_list_and_compare(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    from lquant.qlib_io import store
    a = store.create_run("a.yaml", None, "e", "y")
    b = store.create_run("b.yaml", None, "e", "y")
    store.update_run(a["id"], status="finished", metrics={"IC": 0.01, "ICIR": 0.1})
    store.update_run(b["id"], status="finished", metrics={"IC": 0.03, "Rank IC": 0.05})
    assert len(client.get("/api/qlib/runs").json()) >= 2
    cmp = client.post("/api/qlib/runs/compare", json={"ids": [a["id"], b["id"]]})
    assert cmp.status_code == 200
    keys = {k["metric"] for k in cmp.json()["rows"]}
    assert {"IC", "ICIR", "Rank IC"} <= keys


def test_cancel(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    from lquant.qlib_io import store
    r = store.create_run("a.yaml", None, "e", "y")
    store.update_run(r["id"], status="running")
    monkeypatch.setattr(qlib_api, "request_cancel", lambda jid: True)
    assert client.post(f"/api/qlib/runs/{r['id']}/cancel").status_code == 200
    assert store.get_run(r["id"])["status"] == "canceled"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v -k "workflow or run"`
Expected: FAIL

- [ ] **Step 3: 实现（追加到 qlib.py）**

```python
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
    if not py and py != "" and py is not None:  # pragma: no cover - 防御
        py = None
    if py == "":
        store.update_run(run_id, status="failed", error=_VENV_MISSING_MSG)
        return {"status": "failed"}

    log_p = _log_path(run_id)
    out_p = _metrics_out_path(run_id)
    runner_py = Path(__file__).resolve().parents[2] / "qlib_io" / "runner.py"
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
        store.update_run(run_id, status="failed", error=tail,
                         log_path=str(log_p))
        return {"status": "failed"}
    metrics = json.loads(out_p.read_text(encoding="utf-8")) if out_p.exists() else {}
    store.update_run(run_id, status="finished", metrics=metrics,
                     log_path=str(log_p))
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
    missing = [i for i, r in zip(req.ids, runs) if r is None]
    if missing:
        raise HTTPException(404, f"运行不存在：{missing}")
    keys: set[str] = set()
    for r in runs:
        keys |= set((r.get("metrics") or {}))
    rows = [{"metric": k,
             **{r["id"]: (r.get("metrics") or {}).get(k) for r in runs}}
            for k in sorted(keys)]
    return {"runs": [{"id": r["id"], "config": r["config"],
                      "exp_name": r["exp_name"],
                      "status": r["status"]} for r in runs],
            "rows": rows}
```

注意：文件头部需补 `from pydantic import Field`，以及 `from lquant.server.jobs import enqueue, request_cancel`（Task 3 的 import 行改为同时引 `request_cancel`）。

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_server_qlib_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/server/api/qlib.py tests/unit/test_server_qlib_api.py
git commit -m "feat(qlib): workflow 运行/历史/取消/对比 API"
```

---

### Task 6: 任务中心接入 kind=qlib

**Files:**
- Modify: `src/lquant/server/jobs.py:19`（QUEUES）
- Modify: `src/lquant/server/api/task_center.py`（KINDS、_job_items、_items）
- Modify: `src/lquant/server/main.py`（include qlib router）
- Test: `tests/unit/test_task_center_qlib.py`

**Interfaces:**
- Consumes: Task 3–5 的 router（main.py 注册后 `/api/qlib/*` 生效）

- [ ] **Step 1: 写失败测试**

```python
"""任务中心 qlib 归一测试。"""
from lquant.server.api import task_center as tc


def test_kinds_contains_qlib():
    assert "qlib" in tc.KINDS


def test_job_items_qlib(monkeypatch):
    monkeypatch.setattr(tc, "list_recent_jobs",
                        lambda limit: [{"id": "j1", "queue": "lquant-qlib",
                                        "status": "finished",
                                        "created_at": 1.0}])
    items = tc._items("qlib", 10)
    assert items[0]["kind"] == "qlib"
    assert items[0]["state"] == "finished"


def test_items_unknown_raises():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        tc._items("bogus", 10)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_task_center_qlib.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

- `jobs.py:19`：`QUEUES = ("lquant-default", "lquant-ingest", "lquant-backtest", "lquant-mining", "lquant-qlib")`
- `task_center.py`：`KINDS = ("data", "sync", "backtest", "factor", "qlib")`；`_QUEUE_OF_KIND` 加 `"qlib": "lquant-qlib"`；`_items` 加：

```python
    if kind == "qlib":
        return _job_items("lquant-qlib", "qlib", limit)
```

`_job_items` 显示名 fallback 改为：`"参数扫描" if kind == "backtest" else ("Qlib 任务" if kind == "qlib" else "因子挖掘")`。

- `main.py`：`from lquant.server.api import (...)` 列表加 `qlib`；`for r in (...)` 元组加 `qlib`。

- [ ] **Step 4: 跑测试通过 + 既有 task_center 测试不回归**

Run: `PYTHONPATH=src python -m pytest tests/unit/test_task_center_qlib.py -v -k task_center` 与 `PYTHONPATH=src python -m pytest tests/unit -k "task_center or server" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/server/jobs.py src/lquant/server/api/task_center.py src/lquant/server/main.py tests/unit/test_task_center_qlib.py
git commit -m "feat(qlib): 任务中心接入 kind=qlib + /qlib 路由注册"
```

---

### Task 7: 前端 API 封装 + 类型

**Files:**
- Create: `web/src/lib/qlib.ts`
- Test: `web/src/lib/__tests__/qlib.test.ts`

**Interfaces:**
- Produces:

```ts
export type QlibStatus = { dir: string; exists: boolean; manifest?: Record<string, unknown> | null; calendar?: { start: string; end: string; days: number } | null };
export type QlibConfigMeta = { name: string; path: string; mtime: number };
export type QlibRun = {
  id: string; config: string; market: string | null; exp_name: string;
  status: 'queued' | 'running' | 'finished' | 'failed' | 'canceled';
  metrics: Record<string, number> | null; config_snapshot: string | null;
  log_path: string | null; log_tail?: string | null;
  created_at: string; finished_at: string | null; error: string | null;
};
export type CompareRow = { metric: string; [runId: string]: string | number | null };
export function getQlibStatus(): Promise<QlibStatus>;
export function postQlibExport(body: Partial<ExportIn>): Promise<{ job_id: string }>;
export function listQlibConfigs(): Promise<QlibConfigMeta[]>;
export function getQlibConfig(name: string): Promise<{ name: string; content: string }>;
export function putQlibConfig(name: string, content: string): Promise<{ saved: boolean }>;
export function postQlibWorkflow(body: { config: string; market?: string | null; exp_name: string }): Promise<{ run_id: string }>;
export function listQlibRuns(limit?: number): Promise<QlibRun[]>;
export function getQlibRun(id: string): Promise<QlibRun>;
export function cancelQlibRun(id: string): Promise<{ canceled: boolean }>;
export function compareQlibRuns(ids: [string, string]): Promise<{ runs: { id: string; config: string; exp_name: string; status: string }[]; rows: CompareRow[] }>;
export const EXPORT_FIELDS: string[];
```

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { getQlibStatus, compareQlibRuns, EXPORT_FIELDS } from '../qlib';
import * as api from '../api';

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  postData: vi.fn(),
  putData: vi.fn(),
}));

import { get, post, postData, putData } from '../api';

describe('qlib api', () => {
  beforeEach(() => vi.clearAllMocks());

  it('getQlibStatus hits /qlib/status', async () => {
    vi.mocked(get).mockResolvedValueOnce({ exists: false });
    await getQlibStatus();
    expect(get).toHaveBeenCalledWith('/qlib/status');
  });

  it('compareQlibRuns posts ids', async () => {
    vi.mocked(postData).mockResolvedValueOnce({ runs: [], rows: [] });
    await compareQlibRuns(['a', 'b']);
    expect(postData).toHaveBeenCalledWith('/qlib/runs/compare', { ids: ['a', 'b'] });
  });

  it('EXPORT_FIELDS covers default set', () => {
    expect(EXPORT_FIELDS).toContain('close');
    expect(EXPORT_FIELDS).toContain('vwap');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/lib/__tests__/qlib.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `web/src/lib/qlib.ts`**

```ts
/** qlib 前端 API 封装（/api/qlib/*）。 */
import { get, post, postData, putData } from './api';

export const EXPORT_FIELDS = [
  'open', 'high', 'low', 'close', 'volume', 'amount', 'vwap', 'factor',
  'turnover_rate', 'total_mv', 'float_mv', 'pe_ttm', 'pb_mrq', 'ps_ttm', 'pct_chg',
];

export type QlibStatus = {
  dir: string;
  exists: boolean;
  manifest?: Record<string, unknown> | null;
  calendar?: { start: string; end: string; days: number } | null;
};

export type QlibConfigMeta = { name: string; path: string; mtime: number };

export type QlibRunStatus = 'queued' | 'running' | 'finished' | 'failed' | 'canceled';

export type QlibRun = {
  id: string;
  config: string;
  market: string | null;
  exp_name: string;
  status: QlibRunStatus;
  metrics: Record<string, number> | null;
  config_snapshot: string | null;
  log_path: string | null;
  log_tail?: string | null;
  created_at: string;
  finished_at: string | null;
  error: string | null;
};

export type CompareRow = { metric: string } & Record<string, string | number | null>;

export type ExportIn = {
  start?: string | null;
  end?: string | null;
  symbols?: string[] | null;
  sec_types?: string[] | null;
  fields?: string[] | null;
  top?: number | null;
};

export function getQlibStatus(): Promise<QlibStatus> {
  return get<QlibStatus>('/qlib/status');
}

export function postQlibExport(body: ExportIn): Promise<{ job_id: string }> {
  return postData<{ job_id: string }>('/qlib/export', body);
}

export function listQlibConfigs(): Promise<QlibConfigMeta[]> {
  return get<QlibConfigMeta[]>('/qlib/configs');
}

export function getQlibConfig(name: string): Promise<{ name: string; content: string }> {
  return get(`/qlib/configs/${encodeURIComponent(name)}`);
}

export function putQlibConfig(name: string, content: string): Promise<{ saved: boolean }> {
  return putData(`/qlib/configs/${encodeURIComponent(name)}`, { content });
}

export function postQlibWorkflow(body: {
  config: string; market?: string | null; exp_name: string;
}): Promise<{ run_id: string }> {
  return postData('/qlib/workflow', body);
}

export function listQlibRuns(limit = 50): Promise<QlibRun[]> {
  return get<QlibRun[]>(`/qlib/runs?limit=${limit}`);
}

export function getQlibRun(id: string): Promise<QlibRun> {
  return get<QlibRun>(`/qlib/runs/${encodeURIComponent(id)}`);
}

export function cancelQlibRun(id: string): Promise<{ canceled: boolean }> {
  return post(`/qlib/runs/${encodeURIComponent(id)}/cancel`, {});
}

export function compareQlibRuns(ids: [string, string]) {
  return postData('/qlib/runs/compare', { ids });
}
```

- [ ] **Step 4: 跑测试通过**

Run: `cd web && npx vitest run src/lib/__tests__/qlib.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/qlib.ts web/src/lib/__tests__/qlib.test.ts
git commit -m "feat(web): qlib API 封装与类型"
```

---

### Task 8: /data 页 Qlib 导出卡片

**Files:**
- Create: `web/src/app/data/QlibExportCard.tsx`
- Modify: `web/src/app/data/page.tsx`（挂载卡片）
- Test: `web/src/app/data/__tests__/QlibExportCard.test.tsx`

**Interfaces:**
- Consumes: Task 7 `getQlibStatus / postQlibExport / EXPORT_FIELDS`

- [ ] **Step 1: 写失败测试**

```tsx
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibExportCard from '../QlibExportCard';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

describe('QlibExportCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows missing-data hint when not exported', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({ dir: 'data/qlib', exists: false });
    render(<QlibExportCard />);
    await waitFor(() => screen.getByText(/尚未导出/));
  });

  it('shows manifest calendar when exported', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({
      dir: 'data/qlib', exists: true,
      manifest: { symbols: 7212 },
      calendar: { start: '2021-01-04', end: '2026-09-18', days: 1378 },
    });
    render(<QlibExportCard />);
    await waitFor(() => screen.getByText(/2021-01-04/));
  });

  it('submits export and reports job', async () => {
    vi.mocked(qlib.getQlibStatus).mockResolvedValue({ dir: 'data/qlib', exists: true });
    vi.mocked(qlib.postQlibExport).mockResolvedValue({ job_id: 'j1' });
    render(<QlibExportCard />);
    await waitFor(() => screen.getByRole('button', { name: /导出/ }));
    fireEvent.click(screen.getByRole('button', { name: /导出/ }));
    await waitFor(() => expect(qlib.postQlibExport).toHaveBeenCalled());
    expect(await screen.findByText(/j1/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/app/data/__tests__/QlibExportCard.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现 QlibExportCard**

`web/src/app/data/QlibExportCard.tsx`（遵循页面现有卡片样式，参考 `DataVersionCard.tsx` 的容器 class）：

```tsx
'use client';

/** Qlib 数据导出卡片：状态 + 导出表单，任务进度看任务中心。 */
import { useCallback, useEffect, useState } from 'react';
import {
  EXPORT_FIELDS, getQlibStatus, postQlibExport,
  type QlibStatus,
} from '@/lib/qlib';

const FIELD_LABELS: Record<string, string> = {
  open: '开盘', high: '最高', low: '最低', close: '收盘', volume: '成交量',
  amount: '成交额', vwap: 'VWAP', factor: '复权因子',
  turnover_rate: '换手率', total_mv: '总市值', float_mv: '流通市值',
  pe_ttm: 'PE', pb_mrq: 'PB', ps_ttm: 'PS', pct_chg: '涨跌幅',
};

export default function QlibExportCard() {
  const [status, setStatus] = useState<QlibStatus | null>(null);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [top, setTop] = useState('');
  const [fields, setFields] = useState<string[]>([]);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getQlibStatus().then(setStatus).catch((e) => setMsg(String(e)));
  }, []);
  useEffect(refresh, [refresh]);

  const submit = async () => {
    setMsg(null);
    try {
      const body: Record<string, unknown> = {};
      if (start) body.start = start;
      if (end) body.end = end;
      if (top) body.top = Number(top);
      if (fields.length) body.fields = fields;
      const r = await postQlibExport(body as never);
      setMsg(`已提交导出任务 ${r.job_id}，进度见任务中心`);
    } catch (e) {
      setMsg(`导出失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const cal = status?.calendar;
  return (
    <div className="rounded-lg border border-token-border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-medium">Qlib 数据导出</h3>
        <button className="text-xs text-token-muted" onClick={refresh}>刷新</button>
      </div>
      {!status?.exists ? (
        <p className="text-sm text-token-muted">尚未导出 qlib 数据（data/qlib 不存在）。</p>
      ) : (
        <div className="text-sm space-y-1">
          {cal && <p>日历：{cal.start} ~ {cal.end}（{cal.days} 个交易日）</p>}
          {status.manifest != null && (
            <p>标的：{String((status.manifest as Record<string, unknown>).symbols ?? '—')}</p>
          )}
        </div>
      )}
      <div className="grid grid-cols-3 gap-2 text-sm">
        <label>开始<input className="..." type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label>结束<input className="..." type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        <label>TopN<input className="..." type="number" value={top} onChange={(e) => setTop(e.target.value)} /></label>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        {EXPORT_FIELDS.map((f) => (
          <label key={f} className="...">
            <input type="checkbox" checked={fields.includes(f)}
              onChange={(e) => setFields((old) =>
                e.target.checked ? [...old, f] : old.filter((x) => x !== f))} />
            {FIELD_LABELS[f] ?? f}
          </label>
        ))}
      </div>
      <button className="..." onClick={submit}>导出</button>
      {msg && <p className="text-xs text-token-muted">{msg}</p>}
    </div>
  );
}
```

实现时把 `className="..."` 替换为页面现有卡片/输入样式（读 `DataVersionCard.tsx` 抄同类 class）。

- [ ] **Step 4: page.tsx 挂载**

在 `web/src/app/data/page.tsx` 的合适网格位（质量面板附近）加入：

```tsx
import QlibExportCard from './QlibExportCard';
// ...
<QlibExportCard />
```

- [ ] **Step 5: 跑测试通过**

Run: `cd web && npx vitest run src/app/data/__tests__/QlibExportCard.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/app/data/QlibExportCard.tsx web/src/app/data/page.tsx web/src/app/data/__tests__/QlibExportCard.test.tsx
git commit -m "feat(web): /data 页 qlib 导出卡片"
```

---

### Task 9: /factors 页 qlib 工作流区块

**Files:**
- Create: `web/src/app/factors/QlibWorkflowPanel.tsx`
- Modify: `web/src/app/factors/page.tsx`（挂载）
- Test: `web/src/app/factors/__tests__/QlibWorkflowPanel.test.tsx`

**Interfaces:**
- Consumes: Task 7 `listQlibConfigs / getQlibConfig / putQlibConfig / postQlibWorkflow / listQlibRuns`

- [ ] **Step 1: 写失败测试**

```tsx
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibWorkflowPanel from '../QlibWorkflowPanel';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

const CFGS = [{ name: 'wf_a', path: 'x', mtime: 1 }];

describe('QlibWorkflowPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders config list and runs', async () => {
    vi.mocked(qlib.listQlibConfigs).mockResolvedValue(CFGS);
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([
      { id: 'r1', config: 'wf_a.yaml', market: null, exp_name: 'e', status: 'finished',
        metrics: { IC: 0.03 }, config_snapshot: null, log_path: null,
        created_at: '2026-09-20T00:00:00+00:00', finished_at: null, error: null },
    ]);
    render(<QlibWorkflowPanel />);
    await waitFor(() => screen.getByText('wf_a'));
    expect(screen.getByText(/r1/)).toBeTruthy();
  });

  it('runs workflow with selected config', async () => {
    vi.mocked(qlib.listQlibConfigs).mockResolvedValue(CFGS);
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([]);
    vi.mocked(qlib.postQlibWorkflow).mockResolvedValue({ run_id: 'r9' });
    render(<QlibWorkflowPanel />);
    const btn = await screen.findByRole('button', { name: /运行/ });
    fireEvent.click(btn);
    await waitFor(() =>
      expect(qlib.postQlibWorkflow).toHaveBeenCalledWith(
        expect.objectContaining({ config: 'wf_a' })));
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/app/factors/__tests__/QlibWorkflowPanel.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现 QlibWorkflowPanel**

```tsx
'use client';

/** Qlib 工作流面板：配置选择/编辑保存 + 运行 + 历史运行列表。 */
import { useCallback, useEffect, useState } from 'react';
import {
  getQlibConfig, listQlibConfigs, listQlibRuns, postQlibWorkflow,
  putQlibConfig, type QlibRun,
} from '@/lib/qlib';

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '运行中', finished: '已完成',
  failed: '失败', canceled: '已取消',
};

export default function QlibWorkflowPanel() {
  const [configs, setConfigs] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [content, setContent] = useState<string>('');
  const [expName, setExpName] = useState('lquant_qlib');
  const [market, setMarket] = useState('');
  const [runs, setRuns] = useState<QlibRun[]>([]);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listQlibConfigs().then((cs) => {
      setConfigs(cs.map((c) => c.name));
      setSelected((s) => s || cs[0]?.name || '');
    }).catch((e) => setMsg(String(e)));
    listQlibRuns().then(setRuns).catch(() => undefined);
  }, []);
  useEffect(refresh, [refresh]);

  useEffect(() => {
    if (selected) getQlibConfig(selected).then((c) => setContent(c.content))
      .catch(() => setContent(''));
  }, [selected]);

  const save = async () => {
    try {
      await putQlibConfig(selected, content);
      setMsg('配置已保存');
    } catch (e) {
      setMsg(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const run = async () => {
    setMsg(null);
    try {
      const r = await postQlibWorkflow({
        config: selected, exp_name: expName, market: market || null,
      });
      setMsg(`已提交运行 ${r.run_id}，进度见任务中心`);
      setTimeout(refresh, 500);
    } catch (e) {
      setMsg(`提交失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <section className="space-y-3">
      <h3 className="font-medium">Qlib 工作流</h3>
      <div className="flex gap-2 items-end text-sm">
        <label>配置
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            {configs.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label>股票池
          <input value={market} onChange={(e) => setMarket(e.target.value)}
            placeholder="all / top300" />
        </label>
        <label>实验名<input value={expName} onChange={(e) => setExpName(e.target.value)} /></label>
        <button onClick={save}>保存配置</button>
        <button onClick={run}>运行</button>
      </div>
      <textarea className="w-full font-mono text-xs" rows={16}
        value={content} onChange={(e) => setContent(e.target.value)} />
      {msg && <p className="text-xs text-token-muted">{msg}</p>}
      <table className="w-full text-sm">
        <thead><tr><th>ID</th><th>配置</th><th>状态</th><th>IC</th><th>时间</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td><td>{r.config}</td>
              <td>{STATUS_TEXT[r.status] ?? r.status}</td>
              <td>{r.metrics?.IC ?? '—'}</td>
              <td>{r.created_at.slice(0, 19)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
```

- [ ] **Step 4: page.tsx 挂载（factors 页尾部加 `<QlibWorkflowPanel />`）**

- [ ] **Step 5: 跑测试通过**

Run: `cd web && npx vitest run src/app/factors/__tests__/QlibWorkflowPanel.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/app/factors/QlibWorkflowPanel.tsx web/src/app/factors/page.tsx web/src/app/factors/__tests__/QlibWorkflowPanel.test.tsx
git commit -m "feat(web): /factors 页 qlib 工作流面板"
```

---

### Task 10: /backtests 页 qlib 结果展示 + 对比

**Files:**
- Create: `web/src/app/backtests/QlibRunsSection.tsx`
- Modify: `web/src/app/backtests/page.tsx`（挂载）
- Test: `web/src/app/backtests/__tests__/QlibRunsSection.test.tsx`

**Interfaces:**
- Consumes: Task 7 `listQlibRuns / cancelQlibRun / compareQlibRuns`

- [ ] **Step 1: 写失败测试**

```tsx
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import QlibRunsSection from '../QlibRunsSection';
import * as qlib from '@/lib/qlib';

vi.mock('@/lib/qlib');

const RUNS: qlib.QlibRun[] = [
  { id: 'a1', config: 'a.yaml', market: null, exp_name: 'e', status: 'finished',
    metrics: { IC: 0.01, ICIR: 0.1 }, config_snapshot: null, log_path: null,
    created_at: '2026-09-19T00:00:00+00:00', finished_at: null, error: null },
  { id: 'b2', config: 'b.yaml', market: null, exp_name: 'e', status: 'finished',
    metrics: { IC: 0.03, 'Rank IC': 0.05 }, config_snapshot: null, log_path: null,
    created_at: '2026-09-20T00:00:00+00:00', finished_at: null, error: null },
];

describe('QlibRunsSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders metric cards and rows', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue(RUNS);
    render(<QlibRunsSection />);
    await waitFor(() => screen.getByText(/Qlib 运行/));
    expect(screen.getByText('0.0100')).toBeTruthy();
  });

  it('compare view shows both values', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue(RUNS);
    vi.mocked(qlib.compareQlibRuns).mockResolvedValue({
      runs: [{ id: 'a1', config: 'a.yaml', exp_name: 'e', status: 'finished' },
             { id: 'b2', config: 'b.yaml', exp_name: 'e', status: 'finished' }],
      rows: [{ metric: 'IC', a1: 0.01, b2: 0.03 }],
    });
    render(<QlibRunsSection />);
    const boxes = await screen.findAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    fireEvent.click(screen.getByRole('button', { name: /对比/ }));
    await waitFor(() => screen.getByText('0.0300'));
  });

  it('cancel calls api', async () => {
    vi.mocked(qlib.listQlibRuns).mockResolvedValue([
      { ...RUNS[0], status: 'running' },
    ]);
    vi.mocked(qlib.cancelQlibRun).mockResolvedValue({ canceled: true });
    render(<QlibRunsSection />);
    const btn = await screen.findByRole('button', { name: /取消/ });
    fireEvent.click(btn);
    await waitFor(() => expect(qlib.cancelQlibRun).toHaveBeenCalledWith('a1'));
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/app/backtests/__tests__/QlibRunsSection.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现 QlibRunsSection**

```tsx
'use client';

/** Qlib 运行结果区块：指标卡片 + 勾选对比 + 取消。 */
import { useCallback, useEffect, useState } from 'react';
import {
  cancelQlibRun, compareQlibRuns, listQlibRuns, type QlibRun,
} from '@/lib/qlib';

const METRIC_TEXT: Record<string, string> = {
  IC: 'IC', ICIR: 'ICIR', 'Rank IC': 'Rank IC', 'Rank ICIR': 'Rank ICIR',
  excess_return_without_cost: '超额收益(无成本)',
  excess_return_with_cost: '超额收益(含成本)',
};

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '运行中', finished: '已完成',
  failed: '失败', canceled: '已取消',
};

const fmt = (v: number | null | undefined) =>
  v == null ? '—' : v.toFixed(4);

export default function QlibRunsSection() {
  const [runs, setRuns] = useState<QlibRun[]>([]);
  const [sel, setSel] = useState<string[]>([]);
  const [cmp, setCmp] = useState<Awaited<ReturnType<typeof compareQlibRuns>> | null>(null);

  const refresh = useCallback(() => {
    listQlibRuns().then(setRuns).catch(() => undefined);
  }, []);
  useEffect(refresh, [refresh]);

  const toggle = (id: string) =>
    setSel((old) => old.includes(id)
      ? old.filter((x) => x !== id) : old.length < 2 ? [...old, id] : [old[1], id]);

  const doCompare = async () => {
    if (sel.length === 2) setCmp(await compareQlibRuns(sel as [string, string]));
  };

  const cancel = async (id: string) => {
    await cancelQlibRun(id);
    refresh();
  };

  return (
    <section className="space-y-3">
      <h3 className="font-medium">Qlib 运行</h3>
      <table className="w-full text-sm">
        <thead><tr>
          <th>对比</th><th>ID</th><th>配置</th><th>状态</th>
          <th>IC</th><th>ICIR</th><th>超额(含成本)</th><th>操作</th>
        </tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td><input type="checkbox" checked={sel.includes(r.id)}
                onChange={() => toggle(r.id)} disabled={r.status !== 'finished'} /></td>
              <td>{r.id}</td><td>{r.config}</td>
              <td>{STATUS_TEXT[r.status] ?? r.status}</td>
              <td>{fmt(r.metrics?.IC)}</td>
              <td>{fmt(r.metrics?.ICIR)}</td>
              <td>{fmt(r.metrics?.excess_return_with_cost)}</td>
              <td>
                {r.status === 'running' || r.status === 'queued'
                  ? <button onClick={() => cancel(r.id)}>取消</button> : null}
                {r.error && <span title={r.error}>⚠</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button disabled={sel.length !== 2} onClick={doCompare}>对比</button>
      {cmp && (
        <table className="w-full text-sm">
          <thead><tr>
            <th>指标</th>{cmp.runs.map((r) => <th key={r.id}>{r.id}</th>)}
          </tr></thead>
          <tbody>
            {cmp.rows.map((row) => (
              <tr key={row.metric}>
                <td>{METRIC_TEXT[row.metric] ?? row.metric}</td>
                {cmp.runs.map((r) => (
                  <td key={r.id}>{fmt(Number(row[r.id]))}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

- [ ] **Step 4: backtests/page.tsx 挂载（结果列表区加 `<QlibRunsSection />`）**

- [ ] **Step 5: 跑测试通过**

Run: `cd web && npx vitest run src/app/backtests/__tests__/QlibRunsSection.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/app/backtests/QlibRunsSection.tsx web/src/app/backtests/page.tsx web/src/app/backtests/__tests__/QlibRunsSection.test.tsx
git commit -m "feat(web): /backtests 页 qlib 结果展示与对比"
```

---

### Task 11: 任务中心前端 kind=qlib

**Files:**
- Modify: `web/src/app/tasks/types.ts`（TaskItem.kind 联合类型、KIND_TEXT）
- Modify: `web/src/app/tasks/TaskTable.tsx` / `panels/`（如按 kind 有分支需补）
- Test: `web/src/app/tasks/__tests__`（追加 qlib kind 用例，文件名按现有约定）

**Interfaces:**
- Consumes: Task 6 后端 kind=qlib

- [ ] **Step 1: 写失败测试**

```tsx
import { KIND_TEXT } from '../types';

describe('task center qlib kind', () => {
  it('has qlib label', () => {
    expect(KIND_TEXT.qlib).toBe('Qlib');
  });
});
```

（若现有测试文件已有 KIND_TEXT 用例，直接追加到该文件；`TaskItem['kind']` 联合类型加入 `'qlib'` 后 TS 编译即验证完整性。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/app/tasks`
Expected: FAIL（KIND_TEXT.qlib undefined）

- [ ] **Step 3: 实现**

`types.ts`：

```ts
kind: 'data' | 'sync' | 'backtest' | 'factor' | 'qlib';
// KIND_TEXT 加：
qlib: 'Qlib',
```

检查 `TaskTable.tsx` / `panels/` 里按 kind 的 switch/映射，补 qlib 分支（一般为通用渲染，仅枚举处需加）。

- [ ] **Step 4: 跑全部 tasks 测试 + tsc**

Run: `cd web && npx vitest run src/app/tasks && npx tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/app/tasks
git commit -m "feat(web): 任务中心支持 kind=qlib"
```

---

### Task 12: 全量验证收口

- [ ] **Step 1: 后端全量单测（worktree 环境噪声按 memory 处理：data 符号链接 + LQ_SKIP_PUSH_CHECK）**

Run: `PYTHONPATH=src python -m pytest tests/unit -x -q`
Expected: PASS（与 main 基线一致，无新增失败）

- [ ] **Step 2: ruff 检查新文件**

Run: `ruff check src/lquant/qlib_io/interpreter.py src/lquant/qlib_io/store.py src/lquant/server/api/qlib.py src/lquant/server/api/task_center.py tests/unit/test_qlib_interpreter.py tests/unit/test_qlib_store.py tests/unit/test_server_qlib_api.py tests/unit/test_task_center_qlib.py`
Expected: 无新增违规

- [ ] **Step 3: 前端全量**

Run: `cd web && npx vitest run && npx tsc --noEmit && npm run build`
Expected: PASS

- [ ] **Step 4: 手动冒烟（可选）**

起 server + web dev，走一遍：导出 → 任务中心看进度 → 跑 workflow → /backtests 看结果 → 对比。

- [ ] **Step 5: 汇总提交**

如尚有未提交改动，按 conventional 格式提交。
