"""策略工作台端到端集成测试 —— 保存策略 → 因子联动回测 → record/logs 落库 → 自定义分析。

全链路自包含：tmp 目录 + generate_demo 合成数据（同 test_api.py 的 api_env 模式），
不依赖本地真实库。流程与断言：

1. POST /api/strategies 保存使用 run_daily + get_factor_values + record + log.info 的
   聚宽策略 → 200、version 1；
2. GET 回读 source → POST /api/backtests/run-code（带 factor_formulas + strategy_id）
   → run_id、metrics、logs 非空；
3. GET /api/backtests/{run_id} → records 含 record 的 key 且有日期序列、
   logs 非空、custom_analysis 非空（run-code 自动执行已保存分析，chart spec 回读）；
4. POST /api/analyses 保存一个从 result["metrics"] 产 chart spec 的分析 → 200 且冒烟通过；
5. run_user_analysis 直接跑保存的分析源码（真实 metrics payload）→ 非空 chart spec；
6. 反例：策略 source import os → 422（import 白名单拒绝）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# 测试环境不启动同步后台线程（避免测试期间触发真实采集）
os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")

# ---------------------------------------------------------------- 测试数据

STRATEGY_NAME = "e2e_momentum_desk"

STRATEGY_SOURCE = '''
def initialize(context):
    set_benchmark("000300.SH")
    run_daily(rebalance, time="open")

def rebalance(context):
    fv = get_factor_values("ROC5", count=1)
    ranked = sorted(
        ((s, v[-1]) for s, v in fv.items() if v),
        key=lambda x: x[1], reverse=True)
    picks = [s for s, _ in ranked[:3]]
    for s in picks:
        order_target_value(s, context.portfolio.total_value * 0.3)
    for s in set(context.portfolio.positions) - set(picks):
        order_target_value(s, 0)
    record(total=context.portfolio.total_value)
    log.info("n_picks=%d", len(picks))
'''

ANALYSIS_SOURCE = '''
def analyze(result):
    m = result.get("metrics", {})
    numeric = {k: v for k, v in m.items() if isinstance(v, (int, float))
               and not isinstance(v, bool)}
    return [{
        "type": "chart",
        "title": "回测指标一览",
        "data": [{"x": k, "value": float(v)} for k, v in sorted(numeric.items())],
        "x": "x",
        "ys": ["value"],
    }]
'''


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    """chdir 到 tmp 目录，用 generate_demo 造一份自包含合成数据环境。"""
    base = tmp_path_factory.mktemp("strategy_desk_e2e")
    prev_cwd = os.getcwd()
    os.chdir(base)
    try:
        from lquant.core.config import get_settings

        get_settings.cache_clear()

        from lquant.core.db import writer
        from lquant.data.ingest.demo import generate_demo
        from lquant.data.store.ddl import DDL_STATEMENTS
        from lquant.market.schema import ensure_market_tables
        with writer() as con:                # startup 前手动建库 + 看板表
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

    with TestClient(create_app()) as c:     # with 触发 startup DDL/迁移
        yield c


@pytest.fixture(scope="module")
def run_metrics(client):
    """链路主干：保存策略 → 因子联动回测 → 返回 (run_id, run 详情, metrics)。"""
    # 1. 保存策略
    r = client.post("/api/strategies", json={
        "name": STRATEGY_NAME, "source": STRATEGY_SOURCE,
        "description": "e2e 动量轮动（ROC5 Top3）"})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["name"] == STRATEGY_NAME
    assert saved["version"] == 1
    sid = saved["id"]

    # 1.5 保存一个自定义分析（run-code 会自动执行它）
    ra = client.post("/api/analyses", json={
        "name": "e2e_metrics_chart", "source": ANALYSIS_SOURCE})
    assert ra.status_code == 200, ra.text

    # 2. 回读 source 再跑回测（工作台前端同款路径）
    got = client.get(f"/api/strategies/{sid}")
    assert got.status_code == 200
    assert got.json()["source"] == STRATEGY_SOURCE

    run = client.post("/api/backtests/run-code", json={
        "code": got.json()["source"],
        "start": "2026-01-01",
        "initial_cash": 1_000_000,
        "benchmark": "000300.SH",
        "factor_formulas": ["ROC5"],
        "strategy_id": sid,
    })
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["run_id"]
    assert body["n_nav_points"] > 10
    assert body["metrics"], "metrics 不应为空"
    assert body["logs"], "log.info 未产生日志"
    # 因子联动必须是承重的：选股数 > 0 且真实产生过订单
    import re as _re

    counts = [int(m.group(1)) for lg in body["logs"]
              if (m := _re.search(r"n_picks=(\d+)", str(lg)))]
    assert counts and max(counts) > 0, f"get_factor_values 选股全空: {body['logs']}"
    assert body["n_trades"] > 0, "策略未产生任何成交（选股联动失效）"

    # 3. 详情：records / logs / custom_analysis
    detail = client.get(f"/api/backtests/{body['run_id']}")
    assert detail.status_code == 200
    d = detail.json()
    assert "total" in d["records"], f"record(total) 未落库: {list(d['records'])}"
    series = d["records"]["total"]
    assert len(series) > 10
    assert {"date", "value"} <= set(series[0])
    assert all(x["value"] > 0 for x in series)          # 组合总资产恒正
    assert d["logs"], "详情里 logs 不应为空"
    ca = d["custom_analysis"]
    assert ca, "run-code 应自动执行已保存分析，custom_analysis 不为空"
    chart = next(s for s in ca if s.get("type") == "chart")
    assert chart["type"] == "chart" and chart["data"], "chart spec 数据为空"
    return body["run_id"], d, body["metrics"]


# ---------------------------------------------------------------- 断言链路

def test_01_strategy_saved_and_versioned(client, run_metrics):
    """步骤 1-2 主干由 run_metrics fixture 覆盖；这里补版本/枚举口径。"""
    lst = client.get("/api/strategies").json()
    hit = [s for s in lst if s["name"] == STRATEGY_NAME]
    assert hit and hit[0]["source"] == "user"


def test_02_analysis_saved_smoke_ok(client):
    """步骤 4：POST /api/analyses 保存 chart 分析（保存前冒烟跑空 payload）。"""
    r = client.post("/api/analyses", json={
        "name": "e2e_metrics_chart", "source": ANALYSIS_SOURCE})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["name"] == "e2e_metrics_chart"
    assert client.get(f"/api/analyses/{saved['id']}").status_code == 200
    lst = client.get("/api/analyses").json()
    assert any(a["id"] == saved["id"] and a["name"] == "e2e_metrics_chart"
               for a in lst), "保存的分析未出现在列表里"


def test_03_run_user_analysis_chart(client, run_metrics):
    """步骤 5：真实 metrics payload 直接跑 run_user_analysis → 非空 chart spec。"""
    import polars as pl

    from lquant.backtest.analysis import run_user_analysis

    _, detail, metrics = run_metrics
    payload = {
        "dates": [p["date"] for p in detail["nav"]],
        "nav": [p["nav"] for p in detail["nav"]],
        "returns": [0.0] + [
            (detail["nav"][i]["nav"] / detail["nav"][i - 1]["nav"] - 1)
            for i in range(1, len(detail["nav"]))],
        "trades": pl.DataFrame(),
        "positions": {},
        "records": detail["records"],
        "metrics": metrics,
    }
    specs = run_user_analysis(ANALYSIS_SOURCE, payload)
    assert specs, "run_user_analysis 返回空"
    assert specs[0]["type"] == "chart"
    assert specs[0]["data"], "chart data 不应为空"
    assert specs[0]["x"] == "x" and specs[0]["ys"] == ["value"]


def test_04_validate_rejects_import_os(client):
    """步骤 6 反例：import os 不在白名单 → 保存 422。"""
    bad = "import os\ndef initialize(context):\n    pass"
    assert client.post("/api/strategies/validate",
                       json={"source": bad}).json()["errors"]
    r = client.post("/api/strategies", json={
        "name": "e2e_bad_import", "source": bad})
    assert r.status_code == 422
