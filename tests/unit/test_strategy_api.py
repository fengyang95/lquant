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
    prev_cwd = os.getcwd()
    os.chdir(base)
    try:
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
    finally:
        os.chdir(prev_cwd)


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


def test_strategy_put_on_deleted_404(client):
    """PUT / DELETE 一个已软删的策略 id 不复活：一律 404。"""
    sid = client.post("/api/strategies", json={"name": "s_dead", "source": GOOD_SRC}).json()["id"]
    assert client.delete(f"/api/strategies/{sid}").status_code == 200
    put = client.put(f"/api/strategies/{sid}", json={"source": GOOD_SRC + "# zombie"})
    assert put.status_code == 404
    delete = client.delete(f"/api/strategies/{sid}")
    assert delete.status_code == 404


def test_strategy_get_404(client):
    assert client.get("/api/strategies/ghost").status_code == 404


def test_strategy_put_preserves_metadata(client):
    """PUT 只改 source 时，description/config/benchmark 沿用现有值不被清空。"""
    r = client.post("/api/strategies", json={
        "name": "meta_s", "source": GOOD_SRC, "description": "说明",
        "config": {"top_n": 5}, "benchmark": "000905.SH"})
    assert r.status_code == 200
    sid = r.json()["id"]

    # 只带 source 的 PUT：元数据沿用
    put = client.put(f"/api/strategies/{sid}", json={"source": GOOD_SRC + "# meta v2"})
    assert put.status_code == 200
    body = put.json()
    assert body["version"] == 2
    assert body["description"] == "说明"
    assert body["config"] == {"top_n": 5}
    assert body["benchmark"] == "000905.SH"

    # 显式传 null 的 benchmark + 显式 config：覆盖
    put2 = client.put(f"/api/strategies/{sid}", json={
        "source": GOOD_SRC + "# meta v3", "config": {"top_n": 8}, "benchmark": None})
    assert put2.status_code == 200
    assert put2.json()["config"] == {"top_n": 8}
    assert put2.json()["benchmark"] is None
    assert put2.json()["description"] == "说明"     # 未传，继续沿用


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

RUN_CODE_SRC = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    order_target_value("600519.SH", context.portfolio.total_value * 0.5)
    record(x=context.portfolio.total_value)
    log.info("rebal done")
'''


def _save_chart_analysis(client, name: str) -> str:
    """保存一个从 metrics 产 chart spec 的分析，返回 id。"""
    src = ('def analyze(result):\n'
           '    m = result.get("metrics", {})\n'
           '    numeric = {k: v for k, v in m.items() if isinstance(v, (int, float))\n'
           '               and not isinstance(v, bool)}\n'
           '    return [{"type": "chart", "title": "指标一览",\n'
           '              "data": [{"x": k, "value": float(v)}\n'
           '                       for k, v in sorted(numeric.items())],\n'
           '              "x": "x", "ys": ["value"]}]')
    r = client.post("/api/analyses", json={"name": name, "source": src})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _wait_run_code(client, job_id: str) -> dict:
    """run-code 异步契约：轮询状态端点直到终态（done/failed/canceled）。"""
    import time as _t

    deadline = _t.time() + 60
    while _t.time() < deadline:
        s = client.get(f"/api/backtests/run-code/{job_id}")
        assert s.status_code == 200, s.text
        body = s.json()
        if body["status"] in ("done", "failed", "canceled"):
            return body
        _t.sleep(0.05)
    raise AssertionError("run-code 任务 60s 未到终态")


def _run_code(client) -> str:
    """跑一次默认 run-code，返回 run_id。"""
    r = client.post("/api/backtests/run-code", json={
        "code": RUN_CODE_SRC, "start": "2026-01-01", "initial_cash": 1_000_000})
    assert r.status_code == 200, r.text
    body = _wait_run_code(client, r.json()["job_id"])
    assert body["status"] == "done", body.get("error")
    return body["run_id"]


def test_run_code_auto_executes_saved_analysis(client):
    """run_analysis=True（默认）：已保存分析自动执行，chart spec 落库并回读。"""
    _save_chart_analysis(client, "autoexec_chart")
    rid = _run_code(client)

    body = client.get(f"/api/backtests/{rid}").json()
    ca = body["custom_analysis"]
    assert ca, "custom_analysis 不应为空"
    chart = next(s for s in ca if s.get("type") == "chart")
    assert chart["title"] == "指标一览"
    assert chart["data"] and chart["x"] == "x" and chart["ys"] == ["value"]
    assert any("annual_return" in str(d["x"]) for d in chart["data"]) or chart["data"]


def test_run_code_broken_analysis_does_not_fail_backtest(client):
    """坏分析只产生 error 条目，回测本身照常成功落库。"""
    r = client.post("/api/analyses", json={
        "name": "autoexec_broken",
        # 空数据冒烟能过（返回 []），真实数据上输出非法 spec → 运行时失败
        "source": 'def analyze(result):\n'
                  '    if not result["nav"]:\n'
                  '        return []\n'
                  '    return [{"type": "pie"}]\n'})
    assert r.status_code == 200, r.text
    rid = _run_code(client)

    assert client.get(f"/api/backtests/{rid}").status_code == 200
    body = client.get(f"/api/backtests/{rid}").json()
    err = [s for s in body["custom_analysis"] if "error" in s]
    assert err, f"应包含 error 条目: {body['custom_analysis']}"
    assert any("type 必须是" in s["error"] for s in err)


def test_run_code_without_analysis_flag(client):
    """run_analysis=False：跳过分析执行，custom_analysis 为空列表。"""
    _save_chart_analysis(client, "autoexec_skipped")
    r = client.post("/api/backtests/run-code", json={
        "code": RUN_CODE_SRC, "start": "2026-01-01", "initial_cash": 1_000_000,
        "run_analysis": False})
    assert r.status_code == 200, r.text
    body = _wait_run_code(client, r.json()["job_id"])
    assert body["status"] == "done", body.get("error")
    rid = body["run_id"]
    assert client.get(f"/api/backtests/{rid}").json()["custom_analysis"] == []


def test_run_code_payload_dates_returns_aligned(client):
    """payload 契约：returns 与 dates 对齐 —— len(returns) == len(dates) - 1。

    用一个把长度写进 table spec 的分析来断言（零净值点位也不得跳过丢位）。
    """
    r = client.post("/api/analyses", json={
        "name": "autoexec_lencheck",
        "source": 'def analyze(result):\n'
                  '    d = result["dates"]\n'
                  '    r = result["returns"]\n'
                  '    return [{"type": "table", "title": "len",\n'
                  '              "columns": ["key", "n"],\n'
                  '              "rows": [["dates", len(d)], ["returns", len(r)]]}]'})
    assert r.status_code == 200, r.text
    rid = _run_code(client)
    body = client.get(f"/api/backtests/{rid}").json()
    tbl = next(s for s in body["custom_analysis"] if s.get("type") == "table")
    rows = {row[0]: row[1] for row in tbl["rows"]}
    assert rows["returns"] == rows["dates"] - 1


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
    body = _wait_run_code(client, r.json()["job_id"])
    assert body["status"] == "done", body.get("error")
    rid = body["run_id"]

    detail = client.get(f"/api/backtests/{rid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["records"] and "x" in body["records"]
    assert body["records"]["x"] and {"date", "value"} <= set(body["records"]["x"][0])
    assert body["logs"]
    assert isinstance(body["custom_analysis"], list)

    # params 里应带 strategy_id / factor_formulas / logs
    assert body["params"]["factor_formulas"] == [factor]
    assert "strategy_id" in body["params"]


# ---------- run-code 静态闸（安全边界） ----------

def test_run_code_rejects_dynamic_execution(client):
    """用户代码入口必须先过 validate_source，不能直接进 exec。"""
    r = client.post("/api/backtests/run-code", json={
        "code": "def initialize(context):\n    __import__('os').system('id')\n",
        "start": "2026-01-01", "initial_cash": 1_000_000})
    assert r.status_code == 422, r.text
    assert "__import__" in r.text


def test_run_code_rejects_object_graph_escape(client):
    r = client.post("/api/backtests/run-code", json={
        "code": ("def initialize(context):\n"
                 "    x = ().__class__.__bases__[0]\n"),
        "start": "2026-01-01", "initial_cash": 1_000_000})
    assert r.status_code == 422, r.text
    assert "__class__" in r.text
