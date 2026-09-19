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
