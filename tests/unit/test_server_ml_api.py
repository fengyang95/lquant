"""/ml API 测试：训练/重训任务、模型版本管理、推理与信号。

任务端点用打桩的 ``enqueue``（不真跑训练，那是 ``test_ml_online`` 的职责）；
版本管理端点走**真注册表 + 真隔离库**，因为它的价值恰恰在「SQL 与状态机
一起正确」—— 打桩会把最该测的部分测掉。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lquant.server.api import ml as ml_api
from lquant.server.main import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


@pytest.fixture()
def ml_env(tmp_path, monkeypatch):
    """隔离根 / DuckDB / 模型目录，并建全量表结构。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    yield tmp_path
    get_settings.cache_clear()


def _register(name: str = "line", version_metric: float = 0.05) -> int:
    """往注册表塞一个可载入的版本（用真模型，便于验证/载入端点）。"""
    import numpy as np

    from lquant.research.ml.model import RidgeModel
    from lquant.research.ml.processor import StandardizeProcessor

    model = RidgeModel()
    model.feature_names = ["f1"]
    model.fit(np.array([[1.0], [2.0], [3.0]]), np.array([1.0, 2.0, 3.0]))
    proc = StandardizeProcessor()
    import polars as pl

    proc.fit(pl.DataFrame({"f1": [1.0, 2.0, 3.0]}), features=["f1"])
    from lquant.research.ml import registry

    mv = registry.register_run(
        run_id=f"r{name}{version_metric}", name=name, model=model, processor=proc,
        metrics={"ml": {"test_rank_ic_mean": version_metric}},
        features=["f1"], train_rows=3, test_rows=3,
    )
    return mv.version


# ---------------------------------------------------------------- 状态 / 自省

def test_status_reports_backends_and_stages(client, ml_env):
    _register()
    body = client.get("/api/ml/status").json()
    assert body["backends"]
    assert body["model_lines"] == 1 and body["versions"] == 1
    assert body["by_stage"]["candidate"] == 1
    assert body["model_dir"]


def test_features_without_lake_still_lists_builtins(client, ml_env, monkeypatch):
    """湖空时仍要给出内置因子与公式模板（前端选择器不能空白）。"""
    monkeypatch.setattr(ml_api, "_load", lambda *a, **k: (_ for _ in ()).throw(
        ml_api.HTTPException(503, "empty")))
    body = client.get("/api/ml/features").json()
    assert body["alpha158_count"] == 158
    assert "pct_change_20" in body["formulas"]


# ---------------------------------------------------------------- 任务端点

def test_train_enqueues_ml_queue(client, monkeypatch):
    calls = {}

    class _Job:
        id = "job1"

    def fake_enqueue(queue, fn, *a, **k):
        calls["queue"] = queue
        calls["name"] = k.get("name")
        return _Job()

    monkeypatch.setattr(ml_api, "enqueue", fake_enqueue)
    r = client.post("/api/ml/train", json={
        "features": ["pct_change_20"], "train_end": "2025-06-30",
        "valid_end": "2025-12-31", "model_name": "line"})
    assert r.status_code == 202 and r.json()["job_id"] == "job1"
    assert calls["queue"] == "lquant-ml" and calls["name"] == "ML 训练"


def test_train_validates_window_order(client, ml_env):
    r = client.post("/api/ml/train", json={
        "features": ["close"], "train_end": "2025-06-30",
        "valid_end": "2025-01-01"})
    assert r.status_code == 422


def test_train_rejects_unknown_backend(client, ml_env):
    r = client.post("/api/ml/train", json={
        "features": ["close"], "train_end": "2025-06-30",
        "valid_end": "2025-12-31", "kind": "not_a_model"})
    assert r.status_code == 422


def test_train_rejects_unknown_processor(client, ml_env):
    r = client.post("/api/ml/train", json={
        "features": ["close"], "train_end": "2025-06-30",
        "valid_end": "2025-12-31",
        "processors": [{"kind": "svm"}]})
    assert r.status_code == 422


def test_train_rejects_empty_features(client, ml_env):
    r = client.post("/api/ml/train", json={
        "features": [], "train_end": "2025-06-30", "valid_end": "2025-12-31"})
    assert r.status_code == 422


def test_retrain_enqueues_and_validates_months(client, monkeypatch):
    calls = {}
    class _Job:
        id = "j"

    monkeypatch.setattr(ml_api, "enqueue",
                        lambda q, fn, *a, **k: (calls.update(queue=q) or _Job()))
    r = client.post("/api/ml/retrain", json={"features": ["MA20"], "name": "x",
                                             "model_name": "x"})
    assert r.status_code == 202 and calls["queue"] == "lquant-ml"
    r2 = client.post("/api/ml/retrain", json={"features": ["MA20"], "train_months": 1})
    assert r2.status_code == 422      # ge=3


def test_job_endpoints(client, ml_env):
    assert client.get("/api/ml/jobs/nope").status_code == 404
    assert client.post("/api/ml/jobs/nope/cancel").json()["canceled"] is False


# ---------------------------------------------------------------- 训练记录

def test_runs_list_and_detail(client, ml_env):
    _register()
    rows = client.get("/api/ml/runs").json()
    assert len(rows) == 1
    run_id = rows[0]["run_id"]
    assert rows[0]["model_name"] == "line" and rows[0]["artifact_path"]

    d = client.get(f"/api/ml/runs/{run_id}").json()
    assert d["features"] == ["f1"]
    assert d["metrics"]["ml"]["test_rank_ic_mean"] == 0.05
    assert d["model_version"] == 1
    assert client.get("/api/ml/runs/missing").status_code == 404


# ---------------------------------------------------------------- 模型版本

def test_models_list_and_filters(client, ml_env):
    _register(name="a")
    _register(name="b")
    all_rows = client.get("/api/ml/models").json()
    assert len(all_rows) == 2
    assert len(client.get("/api/ml/models", params={"name": "a"}).json()) == 1
    assert len(client.get("/api/ml/models", params={"stage": "candidate"}).json()) == 2


def test_promote_and_production(client, ml_env):
    v1 = _register(version_metric=0.01)
    v2 = _register(version_metric=0.09)
    client.post(f"/api/ml/models/line/{v1}/promote", json={"stage": "production"})
    assert client.get("/api/ml/models/line/production").json()["version"] == v1

    r = client.post(f"/api/ml/models/line/{v2}/promote",
                    json={"stage": "production", "note": "test"})
    assert r.status_code == 200 and r.json()["stage"] == "production"

    prod = client.get("/api/ml/models/line/production").json()
    assert prod["version"] == v2
    # 旧线上版自动 archived（至多一个 production）
    models = {m["version"]: m["stage"] for m in client.get("/api/ml/models").json()}
    assert models[v1] == "archived"

    assert client.post("/api/ml/models/line/99/promote",
                       json={"stage": "production"}).status_code == 404


def test_rollback_endpoint(client, ml_env):
    v1 = _register(version_metric=0.01)
    v2 = _register(version_metric=0.09)
    client.post(f"/api/ml/models/line/{v1}/promote", json={"stage": "production"})
    client.post(f"/api/ml/models/line/{v2}/promote", json={"stage": "production"})
    r = client.post("/api/ml/models/line/rollback")
    assert r.status_code == 200 and r.json()["version"] == v1
    # 没有可回滚的历史时 409（不是 500）
    assert client.post("/api/ml/models/none/rollback").status_code == 409


def test_production_asof_requires_events(client, ml_env):
    _register()
    assert client.get("/api/ml/models/line/production",
                      params={"asof": "2000-01-01T00:00:00"}).status_code == 404


def test_events_endpoint(client, ml_env):
    _register()
    ev = client.get("/api/ml/models/line/events").json()
    assert len(ev) == 1 and ev[0]["to_stage"] == "candidate"


# ---------------------------------------------------------------- 推理 / 信号

def test_predict_without_production_409(client, ml_env, monkeypatch):
    _register()
    monkeypatch.setattr(ml_api, "_load", lambda *a, **k: __import__(
        "polars").DataFrame({"trade_date": [], "symbol": [], "f1": []}))
    r = client.post("/api/ml/predict", json={"name": "line", "features": ["f1"]})
    assert r.status_code == 409
    assert "没有线上版本" in r.json()["detail"]


def test_signals_empty(client, ml_env):
    body = client.get("/api/ml/signals", params={"name": "line"}).json()
    assert body["rows"] == 0 and body["items"] == [] and body["versions"] == []


def test_signals_reads_persisted_rows(client, ml_env):
    import polars as pl

    from lquant.research.ml.online import persist_signals

    persist_signals(pl.DataFrame({"trade_date": [__import__("datetime").date(2026, 1, 5)],
                                  "symbol": ["600000.SH"], "signal": [0.5]}),
                    name="line", version=2, run_id="r1")
    body = client.get("/api/ml/signals", params={"name": "line"}).json()
    assert body["rows"] == 1
    assert body["items"][0]["symbol"] == "600000.SH"
    assert body["versions"] == [2]


# ---------------------------------------------------------------- 任务中心接线

def test_task_center_knows_ml_kind(client, ml_env):
    from lquant.server.api import task_center

    assert "ml" in task_center.KINDS
    assert task_center._QUEUE_OF_KIND["ml"] == "lquant-ml"
    assert task_center._DEFAULT_JOB_NAME["ml"] == "ML 训练"
    r = client.get("/api/tasks", params={"kind": "ml"})
    assert r.status_code == 200


def test_ml_queue_is_declared():
    from lquant.server.jobs import QUEUES

    assert "lquant-ml" in QUEUES


def test_safe_json_handles_text_and_objects():
    assert ml_api._safe_json(None) is None
    assert ml_api._safe_json('{"a": 1}') == {"a": 1}
    assert ml_api._safe_json({"a": 1}) == {"a": 1}
    assert ml_api._safe_json("not json") == "not json"
    assert json.dumps(ml_api._safe_json('{"a": 1}'))
