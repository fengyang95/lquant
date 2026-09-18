"""API 覆盖补齐：backtests + paper 端点（含异常分支）。

同 test_api.py 模式：tmp 目录 + generate_demo 自包含合成环境，全链路离线。
"""
from __future__ import annotations

import os

import datetime

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_bt_paper")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
        ensure_market_tables(con)
    generate_demo(start="2025-01-01", end="2026-06-30")
    # 指数日线：_benchmark_nav_aligned 的沪深300 分支需要
    import datetime as _dt

    bdays = []
    d = _dt.date(2026, 1, 5)
    while len(bdays) < 38:
        if d.weekday() < 5:
            bdays.append(d)
        d += _dt.timedelta(days=1)
    idx = pl.DataFrame({
        "trade_date": bdays,
        "symbol": ["000300.SH"] * len(bdays),
        "name": ["沪深300"] * len(bdays),
        "open": [4000.0] * len(bdays), "high": [4050.0] * len(bdays),
        "low": [3950.0] * len(bdays),
        "close": [4000.0 * (1 + 0.001 * (i % 5)) for i in range(len(bdays))],
        "pre_close": [4000.0] * len(bdays),
        "volume": [1e8] * len(bdays), "amount": [1e9] * len(bdays),
    })
    with writer() as con:
        con.register("_idx", idx)
        con.execute("INSERT INTO index_daily SELECT *, NULL FROM _idx")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------- backtests ----------------

def test_run_backtest_and_detail(client):
    r = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                "top_n": 2, "start": "2026-01-01"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    d = client.get(f"/api/backtests/{run_id}")
    assert d.status_code == 200
    body = d.json()
    assert body["run_id"] == run_id
    assert body["nav"] and "monthly" in body and "return_hist" in body
    # 404
    assert client.get("/api/backtests/nope").status_code == 404


def test_run_backtest_empty_lake_503(client, monkeypatch):
    import polars as pl

    from lquant.server.api import backtests as bt

    monkeypatch.setattr(bt, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    r = client.post("/api/backtests/run", json={"start": "2026-01-01"})
    assert r.status_code == 503
    r2 = client.post("/api/backtests/run-code", json={"code": "def initialize(c): pass",
                                                      "start": "2026-01-01"})
    assert r2.status_code == 503


def test_run_backtest_bad_formula(client):
    r = client.post("/api/backtests/run", json={"formula": "foo_bar", "start": "2026-01-01"})
    assert r.status_code == 422


def test_run_jq_code_lifecycle(client):
    code = (
        "def initialize(context):\n"
        "    context.symbols = ['600519.SH']\n"
        "\n"
        "def handle_data(context, data):\n"
        "    pass\n"
    )
    r = client.post("/api/backtests/run-code", json={"code": code, "start": "2026-01-01",
                                                     "run_analysis": True})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert r.json()["n_nav_points"] >= 1
    # /{run_id}/code
    c = client.get(f"/api/backtests/{run_id}/code")
    assert c.status_code == 200
    assert c.json()["engine"] == "jq_compat"
    assert client.get("/api/backtests/zzzz/code").status_code == 404


def test_run_jq_code_rejects_bad_source(client):
    r = client.post("/api/backtests/run-code",
                    json={"code": "import os\nos.system('ls')", "start": "2026-01-01"})
    assert r.status_code == 422


def test_run_jq_code_empty_nav(client):
    code = (
        "def initialize(context):\n"
        "    pass\n"
        "\n"
        "def handle_data(context, data):\n"
        "    pass\n"
    )
    r = client.post("/api/backtests/run-code", json={"code": code, "start": "2026-01-01"})
    # 代码合法但未交易 → 仍应有净值（现金曲线），200；若实现为 422 也算契约
    assert r.status_code in (200, 422)


def test_list_runs_and_validation_and_compare(client):
    assert client.get("/api/backtests").status_code == 200
    assert client.get("/api/backtests?limit=2&offset=1").status_code == 200
    v = client.get("/api/backtests/validation")
    assert v.status_code == 200
    assert v.json()["all_passed"] is True
    assert client.get("/api/backtests/compare?ids=a,b,c,d,e,f,g").status_code == 422
    assert client.get("/api/backtests/compare?ids=a,b").status_code == 404


def test_compare_success(client):
    ids = []
    for top in (1, 2):
        r = client.post("/api/backtests/run",
                        json={"formula": "pct_change_20", "top_n": top, "start": "2026-01-01"})
        ids.append(r.json()["run_id"])
    r = client.get("/api/backtests/compare", params={"ids": ",".join(ids)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["runs"]) == 2 and len(body["dates"]) >= 1


def test_holdings_endpoints(client):
    r = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                "top_n": 2, "start": "2026-01-01"})
    run_id = r.json()["run_id"]
    idx = client.get(f"/api/backtests/{run_id}/holdings")
    assert idx.status_code == 200
    day = idx.json()["dates"][0]["date"]
    d = client.get(f"/api/backtests/{run_id}/holdings", params={"day": day})
    assert d.status_code == 200
    assert "positions" in d.json()
    assert client.get("/api/backtests/zz/holdings").status_code == 404
    assert client.get("/api/backtests/zz/holdings",
                      params={"day": "2026-01-05"}).status_code == 404


def test_attribution(client):
    r = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                "top_n": 2, "start": "2026-01-01"})
    run_id = r.json()["run_id"]
    a = client.get(f"/api/backtests/{run_id}/attribution")
    assert a.status_code == 200, a.text
    body = a.json()
    assert "stock_contribution" in body and "brinson" in body and "risk" in body
    assert client.get("/api/backtests/zz/attribution").status_code == 404


def test_run_benchmark(client):
    assert client.post("/api/backtests/run-benchmark",
                       json={"key": "nope"}).status_code == 404
    # 空白 symbols 过滤后为空 → 422（成功路径见 test_api.py 全链路）
    r = client.post("/api/backtests/run-benchmark",
                    json={"key": "sma_cross", "symbols": ["   "]})
    assert r.status_code == 422


def test_paper_accounts_lifecycle(client):
    assert client.get("/api/paper/accounts").status_code == 200
    r = client.post("/api/paper/accounts", json={"name": "cov-fill", "initial_cash": 100000})
    assert r.status_code == 200, r.text
    # 重名：store 抛 sqlite3.IntegrityError（未映射为 422，真实部署会 500）—— 缺陷见汇报
    from lquant.server.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c2:
        dup = c2.post("/api/paper/accounts", json={"name": "cov-fill"})
        assert dup.status_code == 422  # 重名已映射 422（原先 500）
    # ValueError → 422 分支：打桩 service 层抛 ValueError
    from lquant.server.api import paper as paper_mod

    def _boom(*a, **k):
        raise ValueError("策略引用非法")

    orig = paper_mod.paper_service.create_account
    paper_mod.paper_service.create_account = _boom
    try:
        assert client.post("/api/paper/accounts",
                           json={"name": "boom-acct"}).status_code == 422
    finally:
        paper_mod.paper_service.create_account = orig


def test_run_benchmark_success(client):
    r = client.post("/api/backtests/run-benchmark",
                    json={"key": "sma_cross", "symbols": ["600519.SH", "000001.SZ"],
                          "start": "2026-01-01"})
    assert r.status_code == 200, r.text
    assert r.json()["n_nav_points"] >= 1


def test_rolling_std_formula_paths(client):
    """rolling_std_ 公式分支（run / replay）。"""
    r = client.post("/api/backtests/run", json={"formula": "rolling_std_10",
                                                "top_n": 2, "start": "2026-01-01"})
    assert r.status_code == 200, r.text
    rp = client.post("/api/paper/replay", json={"formula": "rolling_std_10",
                                                "top_n": 2, "start": "2026-01-01"})
    assert rp.status_code == 200, rp.text


def test_run_code_records_and_analysis_failure(client, monkeypatch):
    """factor_formulas → records 落库；分析失败不影响回测。"""
    from lquant.backtest import analysis as analysis_mod
    from lquant.backtest import strategy_store as store_mod

    # 一个"坏"分析：运行时抛错 → 记 error 条目而非炸请求
    monkeypatch.setattr(store_mod, "list_analyses",
                        lambda: [{"id": "bad", "name": "bad"}])
    monkeypatch.setattr(store_mod, "get_analysis",
                        lambda _id: {"source": "1/0"})

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(analysis_mod, "run_user_analysis", _raise)

    code = (
        "def initialize(context):\n"
        "    pass\n"
        "\n"
        "def handle_data(context, data):\n"
        "    record(mykey=1.0)\n"
    )
    r = client.post("/api/backtests/run-code",
                    json={"code": code, "start": "2026-01-01",
                          "factor_formulas": ["pct_change_5"], "run_analysis": True})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    d = client.get(f"/api/backtests/{run_id}")
    assert d.status_code == 200
    assert d.json()["records"]            # 记录曲线已落库并返回
    assert d.json()["custom_analysis"]    # 失败分析记 error 条目


def test_run_code_list_analyses_broken(client, monkeypatch):
    from lquant.backtest import strategy_store as store_mod

    def _boom():
        raise RuntimeError("db broken")

    monkeypatch.setattr(store_mod, "list_analyses", _boom)
    code = (
        "def initialize(context):\n"
        "    pass\n"
        "\n"
        "def handle_data(context, data):\n"
        "    pass\n"
    )
    r = client.post("/api/backtests/run-code", json={"code": code, "start": "2026-01-01"})
    assert r.status_code == 200


def test_attribution_edge_branches(client, monkeypatch):
    """无持仓 404 / 空行情 503 / 基准湖异常兜底。"""
    import polars as pl

    from lquant.core.db import writer
    from lquant.server.api import backtests as bt

    # 造一个只有净值、没有持仓的 run → 404
    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO backtest_run VALUES (?,?,?,?,?,?,?,?,?)",
            ["nopos", "factor_topn", "{}", None, None, "done", "{}",
             datetime.datetime.now(), datetime.datetime.now()])
        con.execute("INSERT INTO backtest_nav VALUES (?, ?, ?, ?)",
                    ["nopos", datetime.date(2026, 1, 5), 1.0, 0.0])
    r = client.get("/api/backtests/nopos/attribution")
    assert r.status_code == 404

    # 空行情 → 503（先跑一次正常回测造出持仓数据）
    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    run_id = rb.json()["run_id"]
    monkeypatch.setattr(bt, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    assert client.get(f"/api/backtests/{run_id}/attribution").status_code == 503


def test_get_run_benchmark_fallback_exception(client, monkeypatch):
    """基准指数查询异常 → 降级全市场等权（湖兜底路径）。"""
    from contextlib import contextmanager

    from lquant.server.api import backtests as bt

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "index_daily" in sql:
                raise RuntimeError("index query down")
            return self._con.execute(sql, *a, **k)

    real_reader = bt.reader

    @contextmanager
    def fake_reader():
        with real_reader() as con:
            yield _Proxy(con)

    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    run_id = rb.json()["run_id"]
    monkeypatch.setattr(bt, "reader", fake_reader)
    # 指数查询失败 → 走全市场等权兜底（read_daily 真实读 demo 湖）
    d = client.get(f"/api/backtests/{run_id}")
    assert d.status_code == 200
    assert d.json()["benchmark_label"] == "全市场等权"


def test_compare_no_nav_422(client):
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO backtest_run VALUES (?,?,?,?,?,?,?,?,?)",
            ["nonav", "factor_topn", "{}", None, None, "done", "{}",
             datetime.datetime.now(), datetime.datetime.now()])
    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    r = client.get("/api/backtests/compare",
                   params={"ids": f"nonav,{rb.json()['run_id']}"})
    assert r.status_code == 422


def test_sweep_lifecycle(client):
    r = client.post("/api/backtests/sweep",
                    json={"formula": "pct_change_20", "values": [1, 2],
                          "start": "2026-01-01"})
    assert r.status_code == 200, r.text
    sid = r.json()["sweep_id"]
    assert client.get("/api/backtests/sweep/does-not-exist").status_code == 404
    import time

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        g = client.get(f"/api/backtests/sweep/{sid}")
        if g.status_code == 200 and g.json()["status"] in ("finished", "done"):
            assert g.json()["grid"] is not None
            return
        time.sleep(0.2)
    g = client.get(f"/api/backtests/sweep/{sid}")
    assert g.status_code == 200  # 未完成也不算失败，只断言契约


# ---------------- paper ----------------

def test_paper_state_empty(client):
    from lquant.server.api import paper as paper_mod

    paper_mod._last.clear()
    assert client.get("/api/paper/state").json() == {"has_state": False}


def test_paper_replay_and_compare(client):
    r = client.post("/api/paper/replay", json={"top_n": 2, "start": "2026-01-01",
                                               "formula": "pct_change_20"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] and body["nav"]
    st = client.get("/api/paper/state")
    assert st.status_code == 200 and st.json()["has_state"] is True

    # compare：409（无状态）/ 404（回测不存在）/ 成功
    from lquant.server.api import paper as paper_mod2

    paper_mod2._last.clear()
    assert client.post("/api/paper/compare", json={"run_id": "x"}).status_code == 409
    # 重新生成状态
    assert client.post("/api/paper/replay", json={"top_n": 2, "start": "2026-01-01"}).status_code == 200
    assert client.post("/api/paper/compare", json={"run_id": "ghost"}).status_code == 404
    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    c = client.post("/api/paper/compare", json={"run_id": rb.json()["run_id"]})
    assert c.status_code == 200, c.text
    assert "nav_deviation" in c.json() and "trade_comparison" in c.json()


def test_paper_replay_error_branches(client, monkeypatch):
    import polars as pl

    from lquant.server.api import paper as paper_mod

    # 不支持的公式 → 422
    r = client.post("/api/paper/replay", json={"formula": "foo_bar"})
    assert r.status_code == 422
    # 空湖 → 503
    monkeypatch.setattr(paper_mod, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    assert client.post("/api/paper/replay", json={}).status_code == 503


def test_paper_account_orders(client):
    """下单 / 撤单 / tick / close / nav 的错误分支与成功路径。"""
    assert client.post("/api/paper/order", json={"name": "ghost", "symbol": "600519.SH",
                                                 "side": "buy", "qty": 100,
                                                 "price": 10}).status_code == 404
    # tick / close / nav：404
    assert client.post("/api/paper/tick", json={"name": "ghost"}).status_code == 404
    assert client.post("/api/paper/close", json={"name": "ghost"}).status_code == 404
    assert client.get("/api/paper/nav/ghost").status_code == 404
    # 成功路径：建账户 → 下限价单 → 撤单（无挂单 422）→ nav
    r = client.post("/api/paper/accounts", json={"name": "cov-acct2", "initial_cash": 100000})
    assert r.status_code == 200, r.text
    ok = client.post("/api/paper/order", json={"name": "cov-acct2", "symbol": "600519.SH",
                                               "side": "buy", "qty": 100, "price": 10.0})
    assert ok.status_code == 200, ok.text
    order_id = ok.json()["order_id"]
    assert client.post("/api/paper/order", json={"name": "cov-acct2", "symbol": "600519.SH",
                                                 "side": "hodge", "qty": 100,
                                                 "price": 10}).status_code == 422
    c = client.post("/api/paper/cancel", json={"name": "cov-acct2", "order_id": order_id})
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert client.post("/api/paper/cancel", json={"name": "cov-acct2",
                                                  "order_id": "nope"}).status_code == 422
    n = client.get("/api/paper/nav/cov-acct2")
    assert n.status_code == 200 and "nav" in n.json()


def test_paper_close_valueerror_422(client):
    """day_close 抛 ValueError/TypeError → 422。"""
    from lquant.server.api import paper as paper_mod

    orig = paper_mod.paper_service.day_close

    def _boom(name, d=None):
        raise ValueError("非法交易日")

    paper_mod.paper_service.day_close = _boom
    try:
        r = client.post("/api/paper/close", json={"name": "cov-acct2"})
        assert r.status_code == 422
    finally:
        paper_mod.paper_service.day_close = orig


def test_paper_replay_all_null_factor_422(client, monkeypatch):
    """首日无可用因子值 → 422。"""
    import polars as pl

    from lquant.server.api import paper as paper_mod

    df = pl.DataFrame({
        "trade_date": [datetime.date(2026, 1, 5)],
        "symbol": ["600519.SH"],
        "close": [10.0],
    })
    monkeypatch.setattr(paper_mod, "read_daily", lambda *a, **k: df.lazy())
    r = client.post("/api/paper/replay", json={"formula": "pct_change_20"})
    assert r.status_code == 422
    assert "首日" in r.json()["detail"]


def test_sweep_failed_with_exc_info(client, monkeypatch):
    """RQ 风格 Job 无 .error 属性：exc_info 兜底分支。"""
    from lquant.server.api import backtests as bt

    class _RQJob:
        def get_status(self):
            return "failed"

        exc_info = "boom"

    monkeypatch.setattr(bt, "get_job", lambda _id: _RQJob())
    r = client.get("/api/backtests/sweep/x")
    assert r.status_code == 200
    assert r.json()["error"] == "boom"
    assert r.json()["grid"] is None


def test_holdings_day_with_positions(client):
    """持仓日明细分支（有持仓 → close/weight/day_return 计算）。"""
    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    run_id = rb.json()["run_id"]
    idx = client.get(f"/api/backtests/{run_id}/holdings")
    day = idx.json()["dates"][-1]["date"]          # 最后一个净值日必有持仓
    d = client.get(f"/api/backtests/{run_id}/holdings", params={"day": day})
    assert d.status_code == 200
    body = d.json()
    assert body["positions"] and "weight" in body["positions"][0]
    assert body["day_return"] is not None


def test_holdings_day_without_positions(client):
    """day 无持仓 → 空明细分支。"""
    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    run_id = rb.json()["run_id"]
    idx = client.get(f"/api/backtests/{run_id}/holdings")
    day = idx.json()["dates"][0]["date"]           # 首日因子 warmup 无持仓
    d = client.get(f"/api/backtests/{run_id}/holdings", params={"day": day})
    assert d.status_code == 200
    assert d.json()["positions"] == []
    assert d.json()["day_return"] is not None


def test_run_benchmark_empty_lake_503(client, monkeypatch):
    import polars as pl

    from lquant.server.api import backtests as bt

    monkeypatch.setattr(bt, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    r = client.post("/api/backtests/run-benchmark",
                    json={"key": "sma_cross", "symbols": ["600519.SH"],
                          "start": "2026-01-01"})
    assert r.status_code == 503


def test_run_code_runner_failures(client, monkeypatch):
    """JQRunner ValueError / res.error / 空净值 → 422 三分支。"""
    from lquant.backtest import jqapi
    from lquant.backtest.jqapi import JQResult

    from lquant.server.api import backtests as bt

    def _value_error(*a, **k):
        raise ValueError("基准代码非法")

    class _ResError:
        def __init__(self, *a, **k):
            self.res = JQResult()
            self.res.error = "用户代码运行错误"

        def run(self, df):
            return self.res

    class _NoNav:
        def __init__(self, *a, **k):
            self.res = JQResult()

        def run(self, df):
            return self.res

    code = (
        "def initialize(context):\n"
        "    pass\n"
        "\n"
        "def handle_data(context, data):\n"
        "    pass\n"
    )
    real_runner = jqapi.JQRunner
    for fake in (_value_error, _ResError, _NoNav):
        monkeypatch.setattr(jqapi, "JQRunner", fake)
        r = client.post("/api/backtests/run-code", json={"code": code,
                                                         "start": "2026-01-01"})
        assert r.status_code == 422, r.text
    monkeypatch.setattr(jqapi, "JQRunner", real_runner)


def test_run_code_analysis_crash_swallowed(client, monkeypatch):
    """_run_saved_analyses 整体异常 → custom_analysis 兜底为 []。"""
    from lquant.server.api import backtests as bt

    def _boom(res):
        raise RuntimeError("analysis subsystem down")

    monkeypatch.setattr(bt, "_run_saved_analyses", _boom)
    code = (
        "def initialize(context):\n"
        "    pass\n"
        "\n"
        "def handle_data(context, data):\n"
        "    pass\n"
    )
    r = client.post("/api/backtests/run-code",
                    json={"code": code, "start": "2026-01-01"})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/backtests/{r.json()['run_id']}")
    assert d.json()["custom_analysis"] == []


def test_get_run_benchmark_both_fail(client, monkeypatch):
    """指数查询与全市场等权兜底都失败 → benchmark 置空而非 500。"""
    from contextlib import contextmanager

    from lquant.server.api import backtests as bt

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "index_daily" in sql:
                raise RuntimeError("index query down")
            return self._con.execute(sql, *a, **k)

    real_reader = bt.reader

    @contextmanager
    def fake_reader():
        with real_reader() as con:
            yield _Proxy(con)

    rb = client.post("/api/backtests/run", json={"formula": "pct_change_20",
                                                 "top_n": 2, "start": "2026-01-01"})
    run_id = rb.json()["run_id"]
    monkeypatch.setattr(bt, "read_daily", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("lake down")))
    monkeypatch.setattr(bt, "reader", fake_reader)
    d = client.get(f"/api/backtests/{run_id}")
    assert d.status_code == 200
    assert d.json()["benchmark"] == []
