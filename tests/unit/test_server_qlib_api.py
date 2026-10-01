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
    assert r.json()["exists"] is False


def test_status_with_manifest(client, tmp_path, monkeypatch):
    d = tmp_path / "qlib"
    d.mkdir()
    (d / "qlib_export_meta.json").write_text('{"symbols": 10}', encoding="utf-8")
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(d))
    body = client.get("/api/qlib/status")
    assert body.json()["exists"] is True
    assert body.json()["manifest"] == {"symbols": 10}


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
    got = client.get("/api/qlib/configs/wf_new").json()
    assert "provider_uri" in got["content"]


def test_config_put_invalid_yaml(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    r = client.put("/api/qlib/configs/wf_bad",
                   json={"content": "a: [unclosed\n"})
    assert r.status_code == 422


def test_config_put_path_traversal(client):
    # starlette 路由不匹配带 / 的参数 → 404；显式非法名（万一绕过）→ 422。都不允许写穿。
    r = client.put("/api/qlib/configs/..%2Fevil",
                   json={"content": "x: 1\n"})
    assert r.status_code in (404, 422)


def test_workflow_precheck_no_data(client, monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_api, "_QLIB_DATA_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(qlib_api, "_CONFIG_DIR", tmp_path)
    (tmp_path / "wf_a.yaml").write_text("qlib_init: {}\n", encoding="utf-8")
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
    captured = {}

    def fake_enqueue(queue, fn, *a, **k):
        captured["queue"] = queue
        captured["fn"] = fn
        captured["args"] = a
        return {"id": k.get("job_id")}

    monkeypatch.setattr(qlib_api, "enqueue", fake_enqueue)
    # 预探测：venv 缺失 → 422
    monkeypatch.setattr(qlib_api, "find_qlib_python", lambda py=None: "")
    r = client.post("/api/qlib/workflow", json={"config": "wf_a", "exp_name": "e1"})
    assert r.status_code == 422
    # venv 可用 → 202 且入队
    monkeypatch.setattr(qlib_api, "find_qlib_python", lambda py=None: "fakepy")
    r2 = client.post("/api/qlib/workflow", json={"config": "wf_a", "exp_name": "e1"})
    assert r2.status_code == 202
    rid = r2.json()["run_id"]
    assert store.get_run(rid)["status"] == "queued"
    assert captured["queue"] == "lquant-qlib"
    assert captured["fn"] is qlib_api._run_workflow_job


def test_run_job_success(monkeypatch, tmp_path):
    monkeypatch.setenv("LQ_QLIB_DB", str(tmp_path / "runs.db"))
    from lquant.qlib_io import store
    from lquant.server.api.qlib import _run_workflow_job
    r = store.create_run("wf_a", None, "e1", "yaml")
    log = tmp_path / "run.log"
    monkeypatch.setattr("lquant.server.api.qlib.find_qlib_python",
                        lambda py=None: "fakepy")
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
    assert got["status"] == "finished"
    assert got["metrics"] == {"IC": 0.03}


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
    resp = client.post(f"/api/qlib/runs/{r['id']}/cancel")
    assert resp.status_code == 200
    assert store.get_run(r["id"])["status"] == "canceled"
