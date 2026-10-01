"""因子 CRUD 增强：POST /factors/validate 端点（PUT/DELETE 见 test_api_cov_factors）。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_factor_crud")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    generate_demo(start="2025-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------- validate ----------------

def test_validate_ok_and_bad(client):
    r = client.post("/api/factors/validate", json={"expression": "Rank(close)"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "error": None}

    r2 = client.post("/api/factors/validate", json={"expression": "1 +"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is False
    assert body["error"]


def test_validate_empty_expression(client):
    r = client.post("/api/factors/validate", json={"expression": ""})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "表达式为空"}
