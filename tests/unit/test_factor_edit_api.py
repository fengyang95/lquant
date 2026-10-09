"""因子编辑 / 删除 / 挖掘会话详情端点测试（PUT·DELETE /api/factors/{name}）。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

# api_env 是 module 级（整模块共用一个临时库 + factor "edit_me"），用例间有
# 顺序依赖，xdist 下必须整组同 worker 按序执行。
pytestmark = [
    pytest.mark.usefixtures("api_env"),
    pytest.mark.xdist_group("factor_edit_api"),
]


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("factor_edit_api")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="module")
def factor_name(client) -> str:
    r = client.post("/api/factors", json={
        "name": "edit_me", "expression": "Rank(Ts_Mean($close,5)/$close-1)",
        "description": "原始描述"})
    assert r.status_code == 200, r.text
    return "edit_me"


# ---------------- PUT 编辑 ----------------

def test_update_expression_and_description(client, factor_name):
    r = client.put(f"/api/factors/{factor_name}", json={
        "expression": "Rank($close/Ts_Mean($close,10)-1)",
        "description": "改过的描述", "category": "动量"})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/factors/{factor_name}").json()
    assert d["expression"] == "Rank($close/Ts_Mean($close,10)-1)"
    assert d["description"] == "改过的描述"
    assert d["category"] == "动量"
    assert d["source"] == "manual"


def test_update_none_fields_keep_original(client, factor_name):
    r = client.put(f"/api/factors/{factor_name}", json={})
    assert r.status_code == 200
    d = client.get(f"/api/factors/{factor_name}").json()
    assert d["expression"] == "Rank($close/Ts_Mean($close,10)-1)"


def test_update_bad_dsl_422(client, factor_name):
    r = client.put(f"/api/factors/{factor_name}", json={"expression": "1 + "})
    assert r.status_code == 422


def test_update_missing_404(client):
    r = client.put("/api/factors/no_such_factor", json={"description": "x"})
    assert r.status_code == 404


def test_update_seed_source_rejected(client):
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO factor_def "
            "(name, expression, description, enabled, created_at, source, category) "
            "VALUES ('seeded_f', 'x', '', TRUE, now(), 'qlib', '')")
    r = client.put("/api/factors/seeded_f", json={"description": "x"})
    assert r.status_code == 422
    client.delete("/api/factors/seeded_f")


# ---------------- DELETE ----------------

def test_delete_factor(client, factor_name):
    r = client.delete(f"/api/factors/{factor_name}")
    assert r.status_code == 200
    assert client.get(f"/api/factors/{factor_name}").status_code == 404
    # 二次删除 → 404
    assert client.delete(f"/api/factors/{factor_name}").status_code == 404


# ---------------- 挖掘会话详情 ----------------

def test_mine_run_detail(client):
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO factor_mining_run VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            ["detrun1", "gp-internal", "gp", 10, 1, 2, 0, 0, 1,
             '{"a": "b"}', "2026-09-25 08:00:00"])
    r = client.get("/api/factors/mine/runs/detrun1")
    assert r.status_code == 200
    body = r.json()
    assert body["corrections"] == {"a": "b"}
    assert body["n_survivors"] == 1
    assert client.get("/api/factors/mine/runs/nope").status_code == 404
