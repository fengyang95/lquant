"""API 覆盖补齐：data 端点（gaps/repair、check、checkpoints、crosscheck、quote 等）。"""
from __future__ import annotations

import datetime
import os

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_cov_data")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    generate_demo(start="2025-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------- coverage / securities / daily ----------------

def test_coverage_and_ping(client):
    assert client.get("/api/data/ping").json() == {"pong": "data"}
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    assert r.json()["daily_lake"]["rows"] > 0


def test_coverage_monthly(client):
    r = client.get("/api/data/coverage/monthly")
    assert r.status_code == 200
    assert r.json()["rows"]
    assert client.get("/api/data/coverage/monthly",
                      params={"start": "bad-date"}).status_code == 422


def test_securities_filters(client):
    r = client.get("/api/data/securities", params={"q": "600519"})
    assert r.status_code == 200 and r.json()
    r2 = client.get("/api/data/securities", params={"sec_type": "etf"})
    assert r2.status_code == 200
    # 查询异常 → 空
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "security" in sql and "LIKE" in sql:
                raise RuntimeError("boom")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    data_mod.reader = fake
    try:
        assert client.get("/api/data/securities", params={"q": "x"}).json() == []
    finally:
        data_mod.reader = real_reader


def test_securities_search_is_case_insensitive(client):
    """带交易所后缀的代码小写也应命中（LIKE → ILIKE）。"""
    r = client.get("/api/data/securities", params={"q": "600519.sh"})
    assert [it["symbol"] for it in r.json()] == ["600519.SH"]


# ---------------- 全量标的清单（/securities/universe） ----------------

def test_securities_universe_lists_all_by_default(client):
    """默认（无筛选）返回全量清单 + 总数 + 行业归属。"""
    body = client.get("/api/data/securities/universe",
                      params={"limit": 200}).json()
    assert body["total"] == 30
    assert len(body["items"]) == 30
    assert body["std"] == "SW"
    assert body["asof"]

    by_symbol = {it["symbol"]: it for it in body["items"]}
    # 演示数据给 20 只股票铺了 6 个申万行业；ETF 没有分类 → 行业列为空
    assert by_symbol["600519.SH"]["industry"] == "食品饮料"
    assert by_symbol["600519.SH"]["industry_code"] == "801120.SI"
    assert by_symbol["510300.SH"]["industry"] is None
    # 行业下拉是「全量行业清单」，不随证券过滤收缩
    assert len(body["industries"]) == 6
    assert {i["name"] for i in body["industries"]} >= {"银行", "食品饮料"}


def test_securities_universe_filters(client):
    # 代码（大小写不敏感）
    r = client.get("/api/data/securities/universe", params={"q": "600519.sh"})
    assert [it["symbol"] for it in r.json()["items"]] == ["600519.SH"]
    # 名称包含
    r2 = client.get("/api/data/securities/universe", params={"q": "茅台"})
    assert [it["symbol"] for it in r2.json()["items"]] == ["600519.SH"]
    # 类型
    r3 = client.get("/api/data/securities/universe", params={"sec_type": "etf"})
    assert r3.json()["total"] == 10
    # 板块（演示数据：股票 main / ETF unknown）
    r4 = client.get("/api/data/securities/universe", params={"board": "main"})
    assert r4.json()["total"] == 20


def test_securities_universe_industry_filter(client):
    body = client.get("/api/data/securities/universe",
                      params={"industry": "银行"}).json()
    assert body["total"] == 4
    assert {it["industry"] for it in body["items"]} == {"银行"}
    # 行业代码与名称等价
    code = next(i["code"] for i in body["industries"] if i["name"] == "银行")
    same = client.get("/api/data/securities/universe",
                      params={"industry": code}).json()
    assert same["total"] == body["total"]
    assert [it["symbol"] for it in same["items"]] == [it["symbol"] for it in body["items"]]


def test_securities_universe_unknown_industry_is_empty_not_error(client):
    body = client.get("/api/data/securities/universe",
                      params={"industry": "不存在的行业"}).json()
    assert body["total"] == 0 and body["items"] == []
    # 下拉清单不受影响，否则用户改不回别的行业
    assert len(body["industries"]) == 6
    assert any("未匹配" in n for n in body["notes"])


def test_securities_universe_pagination(client):
    first = client.get("/api/data/securities/universe",
                       params={"limit": 10, "offset": 0}).json()
    second = client.get("/api/data/securities/universe",
                        params={"limit": 10, "offset": 10}).json()
    assert first["total"] == second["total"] == 30
    assert len(first["items"]) == len(second["items"]) == 10
    syms = [it["symbol"] for it in first["items"]]
    assert syms == sorted(syms)  # 稳定按代码序
    assert not ({it["symbol"] for it in first["items"]}
                & {it["symbol"] for it in second["items"]})


def test_securities_universe_param_validation(client):
    assert client.get("/api/data/securities/universe",
                      params={"limit": 500}).status_code == 422
    assert client.get("/api/data/securities/universe",
                      params={"offset": -1}).status_code == 422


def test_securities_universe_include_delisted(client):
    """默认剔除退市股；include_delisted=true 才放进来（幸存者偏差防护）。"""
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO security "
            "(symbol, name, sec_type, board, list_date, delist_date, is_st, source) "
            "VALUES ('999999.SZ', '退市测试', 'stock', 'main', "
            "DATE '2000-01-01', DATE '2001-01-01', FALSE, 'test')")
    try:
        assert client.get("/api/data/securities/universe",
                          params={"q": "999999"}).json()["total"] == 0
        kept = client.get("/api/data/securities/universe",
                          params={"q": "999999", "include_delisted": True}).json()
        assert [it["symbol"] for it in kept["items"]] == ["999999.SZ"]
    finally:
        with writer() as con:
            con.execute("DELETE FROM security WHERE symbol = '999999.SZ'")


def test_securities_universe_std_fallback_note(client):
    body = client.get("/api/data/securities/universe",
                      params={"std": "CICS", "limit": 1}).json()
    assert body["std"] == "SW"
    assert any("回退" in n for n in body["notes"])


def test_securities_universe_without_industry_data(client):
    """industry_classify 读不到时降级：清单照常出，行业列/下拉为空并给出 note。"""
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "industry_classify" in sql:
                raise RuntimeError("no classify table")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    data_mod.reader = fake
    try:
        body = client.get("/api/data/securities/universe").json()
    finally:
        data_mod.reader = real_reader

    assert body["total"] == 30
    assert all(it["industry"] is None for it in body["items"])
    assert body["industries"] == []
    assert body["std"] is None
    assert any("行业分类数据为空" in n for n in body["notes"])


def test_securities_universe_degrades_on_read_error(client):
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "FROM security" in sql:
                raise RuntimeError("boom")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    data_mod.reader = fake
    try:
        body = client.get("/api/data/securities/universe").json()
    finally:
        data_mod.reader = real_reader

    assert body["total"] == 0 and body["items"] == []
    assert any("读取失败" in n for n in body["notes"])


def test_daily_and_indicators(client):
    r = client.get("/api/data/daily", params={"symbol": "600519.SH", "limit": 5})
    assert r.status_code == 200 and len(r.json()) <= 5
    # 空结果
    assert client.get("/api/data/daily",
                      params={"symbol": "999999.SH"}).json() == []
    r2 = client.get("/api/data/indicators", params={"symbol": "600519.SH"})
    assert r2.status_code == 200 and r2.json()


def test_quote_degrades(client):
    r = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r.status_code == 200
    assert r.json()["available"] in (True, False)


# ---------------- 价 / 额 / 市值（湖内快照 + 区间筛选 + 排序） ----------------

def _universe(client, **params):
    return client.get("/api/data/securities/universe", params=params).json()


def test_securities_universe_exposes_price_amount_and_mv(client):
    """清单行带上最新价 / 成交额 / 总市值与各自的口径日（不依赖实时行情源）。"""
    body = _universe(client, q="600519", limit=1)
    it = body["items"][0]
    assert it["symbol"] == "600519.SH"
    assert it["close"] is not None and it["close"] > 0
    assert it["amount"] is not None and it["amount"] > 0
    assert it["total_mv"] is not None and it["total_mv"] > 0
    assert it["float_mv"] is not None and it["float_mv"] > 0
    # 口径日：价 / 额来自日线湖，市值来自估值湖，两者各给各的日期
    assert it["price_date"] == body["asof"]
    assert it["mv_date"] is not None
    assert it["pct_chg"] is not None


def test_securities_universe_price_range_filter(client):
    base = _universe(client, limit=200)
    assert base["total"] == 30
    target = next(i for i in base["items"] if i["symbol"] == "600519.SH")

    above = _universe(client, min_price=target["close"], limit=200)
    assert above["total"] < base["total"]
    assert above["items"]
    assert all(i["close"] >= target["close"] for i in above["items"])

    below = _universe(client, max_price=target["close"], limit=200)
    assert below["items"]
    assert all(i["close"] <= target["close"] for i in below["items"])
    # 上下限同时给 = 闭区间
    both = _universe(client, min_price=target["close"], max_price=target["close"],
                     limit=200)
    assert [i["symbol"] for i in both["items"]] == [target["symbol"]]


def test_securities_universe_mv_and_amount_range_filter(client):
    """市值单位是**亿元**，成交额同理 —— 传参按亿元，湖里是元。"""
    base = _universe(client, limit=200)
    target = max((i for i in base["items"] if i["total_mv"] is not None),
                 key=lambda i: i["total_mv"])
    top_mv = target["total_mv"] / 1e8

    big = _universe(client, min_mv=top_mv, limit=200)
    assert [i["symbol"] for i in big["items"]] == [target["symbol"]]

    small = _universe(client, max_mv=top_mv / 2, limit=200)
    assert small["items"]
    assert all(i["total_mv"] <= top_mv / 2 * 1e8 for i in small["items"])
    assert target["symbol"] not in {i["symbol"] for i in small["items"]}

    busiest = max((i for i in base["items"] if i["amount"] is not None),
                  key=lambda i: i["amount"])
    min_amt = busiest["amount"] / 1e8
    liquid = _universe(client, min_amount=min_amt, limit=200)
    assert [i["symbol"] for i in liquid["items"]] == [busiest["symbol"]]

    # 上限：最冷清的票单独成表
    quietest = min((i for i in base["items"] if i["amount"] is not None),
                   key=lambda i: i["amount"])
    illiquid = _universe(client, max_amount=quietest["amount"] / 1e8, limit=200)
    assert [i["symbol"] for i in illiquid["items"]] == [quietest["symbol"]]


def test_securities_universe_range_filter_drops_rows_without_data(client):
    """没有湖内数据的标的：不筛时照常列出（列为 —），一旦按价/市值筛就被剔除。

    宁可少几行，也不能把「市值不明」的票混进「市值 < 50 亿」的结果里。
    """
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO security "
            "(symbol, name, sec_type, board, list_date, is_st, source) "
            "VALUES ('999997.SZ', '无行情测试', 'stock', 'main', "
            "DATE '2020-01-01', FALSE, 'test')")
    try:
        plain = _universe(client, q="999997", limit=5)
        assert plain["total"] == 1
        row = plain["items"][0]
        assert row["close"] is None and row["total_mv"] is None
        assert row["price_date"] is None and row["mv_date"] is None

        assert _universe(client, q="999997", min_price=0.01)["total"] == 0
        assert _universe(client, q="999997", max_mv=100000)["total"] == 0
    finally:
        with writer() as con:
            con.execute("DELETE FROM security WHERE symbol = '999997.SZ'")


def test_securities_universe_sort(client):
    body = _universe(client, sort="total_mv", order="desc", limit=200)
    mvs = [i["total_mv"] for i in body["items"]]
    assert mvs == sorted(mvs, reverse=True, key=lambda v: (v is None, v))

    asc = _universe(client, sort="close", order="asc", limit=200)
    closes = [i["close"] for i in asc["items"]]
    assert closes == sorted(closes, key=lambda v: (v is None, v))

    by_amount = _universe(client, sort="amount", order="desc", limit=5)
    assert [i["amount"] for i in by_amount["items"]] == \
        sorted([i["amount"] for i in by_amount["items"]], reverse=True)

    # 默认仍是代码升序（翻页稳定性的前提）
    default = _universe(client, limit=200)
    syms = [i["symbol"] for i in default["items"]]
    assert syms == sorted(syms)


def test_securities_universe_sort_whitelist(client):
    """排序字段走白名单：客户端传任意列名一律 422，不放任内部 schema 外泄。"""
    assert client.get("/api/data/securities/universe",
                      params={"sort": "name"}).status_code == 422
    assert client.get("/api/data/securities/universe",
                      params={"order": "sideways"}).status_code == 422
    assert client.get("/api/data/securities/universe",
                      params={"min_price": -1}).status_code == 422
    assert client.get("/api/data/securities/universe",
                      params={"min_mv": -1}).status_code == 422


def test_securities_universe_snapshot_failure_degrades(client, monkeypatch):
    """快照读取失败 → 清单照常出（价/额/市值为空 + note），不是 500 也不是空表。"""
    from lquant.data.store import parquet as pq

    def _boom(*a, **k):
        raise RuntimeError("lake down")

    monkeypatch.setattr(pq, "latest_snapshot", _boom)
    body = _universe(client, limit=5)
    assert body["total"] == 30
    assert len(body["items"]) == 5
    assert all(i["close"] is None and i["total_mv"] is None for i in body["items"])
    assert any("读取失败" in n for n in body["notes"])


# ---------------- quote：实时不可用时用湖内日线兜底 ----------------

def _no_network(*a, **k):
    raise RuntimeError("no network")


def test_quote_falls_back_to_lake_when_realtime_unavailable(client, monkeypatch):
    """实时源挂掉时成交额 / 总市值不该是空的 —— 湖里有，就照实给，并标 source。"""
    from lquant.market import em_client as em_mod

    monkeypatch.setattr(em_mod, "em_get", _no_network)
    body = client.get("/api/data/quote", params={"symbol": "600519.SH"}).json()

    assert body["available"] is False          # available 语义不变：只表示实时
    assert body["source"] == "daily"
    assert body["price"] and body["price"] > 0
    assert body["prev_close"] and body["prev_close"] > 0
    assert body["amount"] and body["amount"] > 0
    assert body["market_cap"] and body["market_cap"] > 0
    assert body["change"] == pytest.approx(body["price"] - body["prev_close"])
    assert body["trade_date"] and body["mv_date"]


def test_quote_backfills_missing_live_fields_from_lake(client, monkeypatch):
    """实时可用但缺 f48/f116 时用湖内补，source 仍是 realtime（口径以实时为准）。"""
    from lquant.market import em_client as em_mod

    class _R:
        def json(self):
            return {"data": {"f57": "600519", "f58": "贵州茅台", "f43": 1500.0,
                             "f169": 12.0, "f170": 0.8, "f46": 1490.0,
                             "f44": 1510.0, "f45": 1480.0, "f60": 1488.0,
                             "f47": 1000.0}}

    monkeypatch.setattr(em_mod, "em_get", lambda url, qps=None: _R())
    body = client.get("/api/data/quote", params={"symbol": "600519.SH"}).json()

    assert body["available"] is True and body["source"] == "realtime"
    assert body["price"] == 1500.0            # 实时值优先，不被湖内覆盖
    assert body["amount"] and body["amount"] > 0            # 湖内兜底
    assert body["market_cap"] and body["market_cap"] > 0    # 湖内兜底


def test_quote_without_realtime_or_lake_data(client, monkeypatch):
    """实时不可用且湖内没有该标的 → source=None（前端显示 —，不编数）。"""
    from lquant.market import em_client as em_mod

    monkeypatch.setattr(em_mod, "em_get", _no_network)
    body = client.get("/api/data/quote", params={"symbol": "999999.SZ"}).json()
    assert body == {"symbol": "999999.SZ", "available": False, "source": None}


def test_quote_survives_lake_read_failure(client, monkeypatch):
    """湖内兜底读取失败不该把实时路径一起带崩（source=None）。"""
    from lquant.data.store import parquet as pq
    from lquant.market import em_client as em_mod

    monkeypatch.setattr(em_mod, "em_get", _no_network)
    monkeypatch.setattr(pq, "latest_snapshot", _no_network)
    body = client.get("/api/data/quote", params={"symbol": "600519.SH"}).json()
    assert body == {"symbol": "600519.SH", "available": False, "source": None}


# ---------------- gaps / repair ----------------

def _fake_rep(missing):
    return {
        "window": {"start": datetime.date(2026, 6, 1), "end": datetime.date(2026, 6, 30)},
        "tables": {
            "daily": {"missing_dates": missing, "sparse_symbols": []},
            "daily_basic": {"missing_dates": [], "sparse_symbols": []},
        },
    }


def test_gaps_endpoint(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep(
                            [datetime.date(2026, 6, 5)]))
    r = client.get("/api/data/gaps")
    assert r.status_code == 200
    body = r.json()
    assert body["missing_dates"] == ["2026-0-05"] or body["missing_dates"]


def test_repair_gaps_no_gap(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep([]))
    r = client.post("/api/data/gaps/repair", json={"days": 30})
    assert r.status_code == 202
    assert r.json() == {"created": False, "task_id": None, "reason": "no_gap"}


def test_repair_gaps_creates_task(client, monkeypatch):
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod
    from lquant.data.quality import coverage as cov_mod
    from lquant.server.api import data as data_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep(
                            [datetime.date(2026, 6, 5)]))

    def _noop(task_id):
        from lquant.data.ingest.tasks import get_task

        return get_task(task_id)

    old_exec = data_mod.execute_task
    old_tasks_exec = tasks_mod.execute_task
    data_mod.execute_task = _noop
    tasks_mod.execute_task = _noop
    try:
        r = client.post("/api/data/gaps/repair", json={"days": 30})
        assert r.status_code == 202, r.text
        assert r.json()["created"] is True
        # 有活跃任务 → 冲突分支
        r2 = client.post("/api/data/gaps/repair", json={"days": 30})
        assert r2.status_code == 202
        assert r2.json()["reason"] == "active_task_exists"
    finally:
        data_mod.execute_task = old_exec
        tasks_mod.execute_task = old_tasks_exec
        with writer() as con:
            con.execute("DELETE FROM data_task")


def test_gaps_error_502(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    def _boom(days, repair=False):
        raise RuntimeError("scan down")

    monkeypatch.setattr(cov_mod, "scan_coverage", _boom)
    r = client.get("/api/data/gaps")
    assert r.status_code == 502


# ---------------- reference sync / index-cons / tasks ----------------

def test_reference_sync_202_and_409(client):
    from lquant.server.api import data as data_mod

    orig = data_mod.sync_reference
    calls = []

    def _fake(skip_details=True):
        calls.append(skip_details)

    # 锁被占用 → 409
    assert data_mod._ref_lock.acquire(blocking=False)
    try:
        assert client.post("/api/data/reference/sync",
                           json={}).status_code == 409
    finally:
        data_mod._ref_lock.release()
    # 空闲 → 202（stub 后台任务，不碰网络）
    data_mod.sync_reference = _fake
    try:
        r = client.post("/api/data/reference/sync", json={})
        assert r.status_code == 202
        assert r.json() == {"accepted": True, "sync_details": False}
    finally:
        data_mod.sync_reference = orig


def test_index_cons_sync(client, monkeypatch):
    from lquant.data.ingest import index_cons as ic_mod

    monkeypatch.setattr(ic_mod, "sync_index_cons", lambda indexes=None: {"n": 1})
    r = client.post("/api/data/index-cons/sync", json={})
    assert r.status_code == 200 and r.json() == {"n": 1}

    def _boom(indexes=None):
        raise RuntimeError("tushare down")

    monkeypatch.setattr(ic_mod, "sync_index_cons", _boom)
    assert client.post("/api/data/index-cons/sync", json={}).status_code == 502


def test_data_tasks_contract(client):
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    r = client.post("/api/data/tasks", json={"kind": "nope_kind"})
    assert r.status_code == 422
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"start": "2026-06-01",
                                                        "end": "2026-06-30"}})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    # pending → conflict 409
    assert client.post("/api/data/tasks", json={"kind": "daily_update",
                                                "params": {"start": "2026-06-01",
                                                           "end": "2026-06-30"}}).status_code == 409
    # 列表 / 详情
    assert client.get("/api/data/tasks").status_code == 200
    assert client.get(f"/api/data/tasks/{tid}").status_code == 200
    assert client.get("/api/data/tasks/ghost").status_code == 404
    # pending → retry 409
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 409
    # ok 终态 → retry 422
    with writer() as con:
        con.execute("UPDATE data_task SET status = 'ok' WHERE task_id = ?", [tid])
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 422
    with writer() as con:
        con.execute("DELETE FROM data_task")


def test_task_events_404(client):
    assert client.get("/api/data/tasks/ghost/events").status_code == 404


async def test_task_events_stream(api_env):
    """SSE 流：snapshot → progress → done 后收线（复用 test_task_events 模式）。"""
    import asyncio

    import httpx

    from lquant.core import task_events
    from lquant.data.ingest.tasks import create_task
    from lquant.server.main import create_app

    task_id = create_task(kind="daily_update", params={"days": 1})["task_id"]
    frames: list[bytes] = []

    async def publish_events() -> None:
        await asyncio.sleep(0.2)
        task_events.publish(task_id, {"task_id": task_id, "done": 3})
        task_events.publish(task_id, {"task_id": task_id, "terminal": True, "done": 10})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as hx:
        pub = asyncio.create_task(publish_events())
        async with hx.stream("GET", f"/api/data/tasks/{task_id}/events") as r:
            assert r.status_code == 200
            async for chunk in r.aiter_bytes():
                frames.append(chunk)
                if b"event: done" in b"".join(frames):
                    break
        await asyncio.wait_for(pub, timeout=5)

    raw = b"".join(frames)
    assert b"event: snapshot" in raw
    assert b"event: progress" in raw
    assert b"event: done" in raw


# ---------------- check / checkpoints / crosscheck ----------------

def test_lake_check(client, monkeypatch):
    from lquant.data.quality import pipeline as pipe_mod
    from lquant.data.quality.issues import Issue

    issues = [
        Issue(rule="rule_a", severity="error", detail="3 行异常"),
        Issue(rule="rule_b", severity="info", detail="ok", dataset="daily_basic"),
    ]
    monkeypatch.setattr(pipe_mod, "run_lake_checks", lambda start=None, end=None: issues)
    r = client.post("/api/data/check", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total"] == 2
    assert body["issues"][0]["severity"] == "error"     # error 排最前
    # 非法日期 → 422
    assert client.post("/api/data/check",
                       json={"start": "junk"}).status_code == 422


def test_lake_check_409_locked(client):
    from lquant.server.api import data as data_mod

    assert data_mod._lake_check_lock.acquire(blocking=False)
    try:
        r = client.post("/api/data/check", json={})
        assert r.status_code == 409
    finally:
        data_mod._lake_check_lock.release()


def test_checkpoints_lifecycle(client):
    import json as _json
    from pathlib import Path

    from lquant.core.config import get_settings

    d = Path(get_settings().cache_dir) / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    (d / "daily_update.json").write_text(
        _json.dumps({"done": ["600519.SH", "000001.SZ"],
                     "updated_at": "2026-06-30T15:00:00",
                     "meta": {"start": "2026-06-01"}}), encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "list.json").write_text("[1,2]", encoding="utf-8")

    r = client.get("/api/data/checkpoints")
    assert r.status_code == 200
    names = {c["name"] for c in r.json()}
    assert "daily_update" in names

    # 归档
    a = client.delete("/api/data/checkpoints/daily_update")
    assert a.status_code == 200
    assert a.json()["archived_to"].startswith("daily_update.done-")
    assert client.delete("/api/data/checkpoints/ghost").status_code == 404
    # 名称含 .. → 防路径穿越 422
    assert client.delete("/api/data/checkpoints/a..b").status_code == 422


def test_crosscheck_endpoints(client, monkeypatch):
    from lquant.data.ingest import crosscheck as cc_mod
    from lquant.data.quality.issues import Issue, ensure_table, save_issues

    def _ok(**k):
        return {"summary": {"n": 1}, "issues": [], "flagged_rows": []}

    monkeypatch.setattr(cc_mod, "run_crosscheck", _ok)
    r = client.post("/api/data/crosscheck", json={})
    assert r.status_code == 200 and r.json()["summary"]["n"] == 1

    def _boom(**k):
        raise ValueError("peers 参数非法")

    monkeypatch.setattr(cc_mod, "run_crosscheck", _boom)
    assert client.post("/api/data/crosscheck", json={}).status_code == 422

    # issues 检索 + resolve
    from lquant.core.db import writer

    with writer() as con:
        ensure_table(con)
    save_issues([Issue(rule="rule_x", severity="warn", detail="需要修")])
    r = client.get("/api/data/crosscheck/issues")
    assert r.status_code == 200
    hit = next(i for i in r.json() if i["rule_code"] == "rule_x")
    assert client.post("/api/data/crosscheck/issues/resolve",
                       json={"issue_id": hit["issue_id"]}).status_code == 200
    assert client.post("/api/data/crosscheck/issues/resolve",
                       json={"issue_id": "ghost"}).status_code == 404


def test_retry_claim_error_branches(client, monkeypatch):
    """claim_retry ValueError/TaskConflictError → 422/409；成功路径。"""
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod
    from lquant.data.ingest.tasks import TaskConflictError
    from lquant.server.api import data as data_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    tid = client.post("/api/data/tasks", json={"kind": "daily_update",
                                               "params": {"start": "2026-06-01",
                                                          "end": "2026-06-30"}}).json()["task_id"]
    # pending → 409
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 409
    with writer() as con:
        con.execute("UPDATE data_task SET status = 'failed' WHERE task_id = ?", [tid])

    def _ok(task_id):
        return True

    def _value_error(task_id):
        raise ValueError("终态 ok 不可 retry")

    def _conflict(task_id):
        raise TaskConflictError("竞争抢先")

    old = tasks_mod.claim_retry
    old_rc = tasks_mod.run_claimed_task
    old_dr = data_mod.claim_retry
    old_drc = data_mod.run_claimed_task

    def _noop(task_id):
        return None

    for stub, expected in ((_value_error, 422), (_conflict, 409), (_ok, 202)):
        tasks_mod.claim_retry = stub
        data_mod.claim_retry = stub
        tasks_mod.run_claimed_task = _noop
        data_mod.run_claimed_task = _noop
        r = client.post(f"/api/data/tasks/{tid}/retry")
        assert r.status_code == expected, r.text
    tasks_mod.claim_retry = old
    tasks_mod.run_claimed_task = old_rc
    data_mod.claim_retry = old_dr
    data_mod.run_claimed_task = old_drc
    with writer() as con:
        con.execute("DELETE FROM data_task")


# ---------------- 补充分支（日期校验 / 空湖 / quote 打桩 / retry / issue 异常） ----------------

def test_crosscheck_input_date_validation(client):
    """CrosscheckIn._validate_date：非法 start/end → 422。"""
    r = client.post("/api/data/crosscheck", json={"start": "junk"})
    assert r.status_code == 422
    r2 = client.post("/api/data/crosscheck", json={"end": "2026/01/02"})
    assert r2.status_code == 422


def test_coverage_table_and_lake_error_branches(client, monkeypatch):
    """单表坏了不影响整页；湖异常 → error 标记；空 schema → rows: []。"""
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "DESCRIBE" in sql or "count(*)" in sql:
                raise RuntimeError("table broken")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(data_mod, "reader", fake)
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    bad = [t for t in r.json()["tables"] if t.get("error")]
    assert bad                                    # 坏表 → error 条目

    # 湖异常 → daily_lake.error = True
    def _boom(*a, **k):
        raise RuntimeError("lake down")

    monkeypatch.setattr(data_mod, "read_daily", _boom)
    r2 = client.get("/api/data/coverage")
    assert r2.status_code == 200
    assert r2.json()["daily_lake"]["error"] is True

    # coverage/monthly：湖异常 → 降级空 rows
    r3 = client.get("/api/data/coverage/monthly")
    assert r3.status_code == 200
    assert r3.json()["rows"] == []

    # coverage/monthly：空 schema → rows: []（非异常路径）
    monkeypatch.setattr(data_mod, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    r4 = client.get("/api/data/coverage/monthly")
    assert r4.status_code == 200
    assert r4.json()["rows"] == []


def test_indicators_empty_lake(client, monkeypatch):
    """指标端点空湖 → []。"""
    from lquant.server.api import data as data_mod

    empty = pl.DataFrame(schema={"trade_date": pl.Date, "symbol": pl.String,
                                 "close": pl.Float64, "open": pl.Float64,
                                 "high": pl.Float64, "low": pl.Float64,
                                 "volume": pl.Float64})
    monkeypatch.setattr(data_mod, "read_daily", lambda *a, **k: empty.lazy())
    r = client.get("/api/data/indicators", params={"symbol": "600519.SH"})
    assert r.status_code == 200 and r.json() == []


def test_quote_stubbed_success_and_deep_codes(client, monkeypatch):
    """quote 成功路径（打桩 em_get）+ 深市 secid 分支。"""
    from lquant.market import em_client as em_mod

    class _R:
        def json(self):
            return {"data": {"f57": "600519", "f58": "贵州茅台", "f43": 1500.0,
                             "f169": 12.0, "f170": 0.8, "f46": 1490.0,
                             "f44": 1510.0, "f45": 1480.0, "f60": 1488.0,
                             "f47": 1000, "f48": 1.5e8, "f50": 0.6,
                             "f116": 1.8e12}}

    captured = {}

    def _fake(url, qps=None):
        captured["url"] = url
        return _R()

    monkeypatch.setattr(em_mod, "em_get", _fake)
    r = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["name"] == "贵州茅台"
    assert "secid=1.600519" in captured["url"]      # 沪市
    r2 = client.get("/api/data/quote", params={"symbol": "000001.SZ"})
    assert r2.status_code == 200 and r2.json()["available"] is True
    assert "secid=0.000001" in captured["url"]      # 深市
    # f57 缺失 → available False
    monkeypatch.setattr(em_mod, "em_get",
                        lambda url, qps=None: type("R2", (), {
                            "json": staticmethod(lambda: {"data": {}})})())
    r3 = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r3.json()["available"] is False
