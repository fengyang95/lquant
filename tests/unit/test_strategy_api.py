"""策略库 / 分析库 API + run-code 因子联动与 record 落库 集成测试。

同 test_api.py 的 api_env 模式：chdir tmp + generate_demo + DDL，全离线。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("strategy_api")
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_market_tables(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


GOOD_SRC = '''def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    order_target_value("600519.SH", context.portfolio.total_value * 0.5)
'''


def _builtin_factor() -> str:
    """从 list_builtin() 取一个真实内置因子名，避免硬编码踩空。"""
    from lquant.factors.qlib_alpha import list_builtin

    return list_builtin()[0]["name"]


# ---------- strategies ----------

def test_strategy_crud_roundtrip_and_versions(client):
    r = client.post("/api/strategies", json={"name": "s1", "source": GOOD_SRC})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["version"] == 1

    detail = client.get(f"/api/strategies/{sid}")
    assert detail.status_code == 200
    assert detail.json()["source"].startswith("def initialize")
    assert detail.json()["name"] == "s1"

    # 同名再存 = 版本 bump
    r2 = client.post("/api/strategies", json={"name": "s1", "source": GOOD_SRC + "# v2"})
    assert r2.status_code == 200
    assert r2.json()["version"] == 2

    # 列表：用户策略带 source=user，内置策略带 source=builtin
    lst = client.get("/api/strategies").json()
    user = [s for s in lst if s.get("name") == "s1"]
    assert user and user[0]["source"] == "user"
    assert user[0]["id"] != sid          # 新版本 = 新 id，列表只暴露 is_latest 行
    assert any(s.get("source") == "builtin" for s in lst)
    assert any(s["name"] == "factor_topn" for s in lst)

    # 版本历史
    vs = client.get(f"/api/strategies/{sid}/versions").json()
    assert [v["version"] for v in vs] == [2, 1]
    assert all("deleted" in v for v in vs)

    # PUT = 再存同名新版本
    put = client.put(f"/api/strategies/{sid}", json={"source": GOOD_SRC + "# v3"})
    assert put.status_code == 200
    assert put.json()["version"] == 3

    # 非法 source → 422
    bad = client.post("/api/strategies", json={"name": "s_bad", "source": "import os"})
    assert bad.status_code == 422

    # 软删 + 404
    assert client.delete(f"/api/strategies/{sid}").status_code == 200
    assert client.get(f"/api/strategies/{sid}").status_code == 404


def test_strategy_get_404(client):
    assert client.get("/api/strategies/ghost").status_code == 404


def test_strategy_validate_endpoint(client):
    ok = client.post("/api/strategies/validate", json={"source": GOOD_SRC})
    assert ok.status_code == 200
    assert ok.json()["errors"] == []
    bad = client.post("/api/strategies/validate", json={"source": "import os"})
    assert bad.status_code == 200
    assert bad.json()["errors"] != []


# ---------- analyses ----------

def test_analysis_crud_roundtrip(client):
    src = ('def analyze(result):\n'
           '    cols = ["k", "v"]\n'
           '    row = ["ann", result["metrics"].get("annual_return", 0)]\n'
           '    return [{"type": "table", "title": "汇总", "columns": cols,\n'
           '              "rows": [row]}]')
    r = client.post("/api/analyses", json={"name": "a1", "source": src})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    detail = client.get(f"/api/analyses/{aid}")
    assert detail.status_code == 200
    assert "def analyze" in detail.json()["source"]

    # 冒烟失败 → 422（run_user_analysis 对空数据也应能跑通合法代码）
    r2 = client.post("/api/analyses", json={"name": "a1", "source": src + "# v2"})
    assert r2.status_code == 200
    bad = client.post("/api/analyses", json={"name": "a_bad", "source": "import os"})
    assert bad.status_code == 422

    lst = client.get("/api/analyses").json()
    assert any(a["id"] == aid for a in lst)

    put = client.put(f"/api/analyses/{aid}", json={"source": src + "# v3"})
    assert put.status_code == 200

    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert client.get(f"/api/analyses/{aid}").status_code == 404


def test_analysis_get_404(client):
    assert client.get("/api/analyses/ghost").status_code == 404


# ---------- run-code 因子联动 + record 落库 ----------

def test_run_code_with_factor_and_records(client):
    factor = _builtin_factor()
    code = f'''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    order_target_value("600519.SH", context.portfolio.total_value * 0.5)
    fv = get_factor_values("{factor}", ["600519.SH"])
    record(x=context.portfolio.total_value)
    log.info("rebal done")
'''
    r = client.post("/api/backtests/run-code", json={
        "code": code, "start": "2026-01-01", "initial_cash": 1_000_000,
        "factor_formulas": [factor]})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]

    detail = client.get(f"/api/backtests/{rid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["records"] and "x" in body["records"]
    assert body["records"]["x"] and {"date", "value"} <= set(body["records"]["x"][0])
    assert body["logs"]
    # custom_analysis 本期占位为空列表
    assert body["custom_analysis"] == []

    # params 里应带 strategy_id / factor_formulas / logs
    assert body["params"]["factor_formulas"] == [factor]
    assert "strategy_id" in body["params"]
