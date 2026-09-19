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
