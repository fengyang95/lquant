"""API 覆盖补齐：market 端点（含空态 / 422 / 503 分支）。

同 test_api.py 模式：tmp 目录 + generate_demo + demo 采集灌看板表。
"""
from __future__ import annotations

import datetime
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_market")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS
    from lquant.market.scheduler import collect_and_save
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_market_tables(con)
    generate_demo(start="2025-01-01", end="2026-06-30")
    collect_and_save(schedule=None, demo=True)
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_overview(client):
    r = client.get("/api/market/overview")
    assert r.status_code == 200
    assert "sentiment" in r.json() and "northbound" in r.json()


def test_sectors_kinds(client):
    for kind in ("industry", "concept", "area"):
        r = client.get("/api/market/sectors", params={"kind": kind})
        assert r.status_code == 200
    # 非法 kind → 422
    assert client.get("/api/market/sectors", params={"kind": "bogus"}).status_code == 422


def test_money_flow(client):
    r = client.get("/api/market/money-flow")
    assert r.status_code == 200
    r2 = client.get("/api/market/money-flow", params={"symbol": "600519", "top": 5})
    assert r2.status_code == 200
    # 超长 symbol → 422（max_length）
    assert client.get("/api/market/money-flow",
                      params={"symbol": "x" * 20}).status_code == 422


def test_limit_up_and_dragon_tiger(client):
    assert client.get("/api/market/limit-up").status_code == 200
    assert client.get("/api/market/limit-up", params={"limit": 3}).status_code == 200
    assert client.get("/api/market/dragon-tiger").status_code == 200


def test_collectors_index_collect_status_schedules(client):
    assert client.get("/api/market/collectors").status_code == 200
    assert client.get("/api/market/index").status_code == 200
    assert client.get("/api/market/collect-status").status_code == 200
    s = client.get("/api/market/schedules")
    assert s.status_code == 200
    assert "schedules" in s.json()


def test_collect_bad_date_422(client):
    r = client.post("/api/market/collect", json={"trade_date": "not-a-date"})
    assert r.status_code == 422


def test_backfill_503(client, monkeypatch):
    from lquant.market import backfill as bf_mod

    def _boom(**k):
        raise RuntimeError("source down")

    monkeypatch.setattr(bf_mod, "ensure_market_coverage", _boom)
    r = client.post("/api/market/backfill", json={"demo": True})
    assert r.status_code == 503
    # 成功分支
    monkeypatch.setattr(bf_mod, "ensure_market_coverage",
                        lambda **k: {"ok": True})
    r2 = client.post("/api/market/backfill", json={"demo": True})
    assert r2.status_code == 200


def test_breadth(client):
    r = client.get("/api/market/breadth")
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] and "up_ratio" in body["latest"]


def test_batch_success_and_errors(client):
    r = client.get("/api/market/batch", params={"symbols": "600519,000001.SZ",
                                                "days": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["latest"] and body["series"] and body["equal_weight_nav"]
    # 空 symbols → 422
    assert client.get("/api/market/batch", params={"symbols": "      "}).status_code == 422
    # 超过 20 只 → 422（用湖里真实存在的代码走到数量校验）
    many = ",".join(["600000", "600036", "600519", "600887", "601318", "601899",
                     "000001", "000002", "000333", "000651", "002594", "300750",
                     "688111", "603259", "600276", "000858", "002415", "600900",
                     "510300", "510050", "159915"])
    assert client.get("/api/market/batch", params={"symbols": many}).status_code == 422


def test_snapshot(client):
    r = client.get("/api/market/snapshot", params={"page": 1, "size": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0 and len(body["rows"]) <= 5
    r2 = client.get("/api/market/snapshot", params={"sort": "amount"})
    assert r2.status_code == 200


def test_heat(client):
    r = client.get("/api/market/heat")
    assert r.status_code == 200
    body = r.json()
    assert body["gainers"] and body["losers"] and body["volume"]


def test_breadth_threshold_branches(client, monkeypatch):
    """北交所 30% / 创业板 20% / 主板 10% 三档阈值 + 空 series 分支。"""
    import polars as pl

    from lquant.data.store import parquet as pq_mod

    df = pl.DataFrame({
        "trade_date": [datetime.date(2026, 1, 5), datetime.date(2026, 1, 5),
                       datetime.date(2026, 1, 5)],
        "symbol": ["830001.BJ", "300750.SZ", "600000.SH"],
        "close": [10.0, 10.0, 10.0],
        "pre_close": [7.0, 8.0, 10.0],       # 涨幅 42.9% / 25% / 0
        "amount": [1e8, 1e8, 1e8],
    })
    monkeypatch.setattr(pq_mod, "read_daily", lambda *a, **k: df.lazy())
    r = client.get("/api/market/breadth")
    assert r.status_code == 200
    latest = r.json()["latest"]
    assert latest["limit_up"] == 2          # 北交所 + 创业板触发各自阈值

    # batch：一只不在湖里的票 → series 缺失分支
    b = client.get("/api/market/batch", params={"symbols": "999999.SH"})
    assert b.status_code == 200
    assert b.json()["series"] == {}


def test_endpoints_with_broken_reader(client, monkeypatch):
    """表缺失/查询异常 → 空结构兜底。"""
    from contextlib import contextmanager

    from lquant.server.api import market as market_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "sector_daily" in sql or "sentiment_daily" in sql \
                    or "northbound_flow" in sql or "money_flow" in sql \
                    or "limit_up_pool" in sql or "dragon_tiger" in sql \
                    or "index_daily" in sql or "security" in sql:
                raise RuntimeError("table missing")
            return self._con.execute(sql, *a, **k)

    real_reader = market_mod.reader

    @contextmanager
    def fake_reader():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(market_mod, "reader", fake_reader)
    assert client.get("/api/market/overview").status_code == 200
    assert client.get("/api/market/sectors").status_code == 200
    assert client.get("/api/market/sectors", params={"kind": "concept"}).status_code == 200
    assert client.get("/api/market/money-flow").status_code == 200
    assert client.get("/api/market/money-flow", params={"symbol": "600519"}).status_code == 200
    assert client.get("/api/market/limit-up").status_code == 200
    assert client.get("/api/market/dragon-tiger").status_code == 200
    assert client.get("/api/market/index").status_code == 200
    h = client.get("/api/market/heat")
    assert h.status_code == 200 and h.json()["dragon_tiger"] == []
    b = client.get("/api/market/batch", params={"symbols": "600519"})
    assert b.status_code == 200        # 湖数据仍在，名称查询异常被吞掉


def test_daily_aggregate_exception_empty(client, monkeypatch):
    """_daily_aggregate 湖异常 → 空态（snapshot/heat 不 500）。"""
    from lquant.data.store import parquet as pq_mod

    def _boom(*a, **k):
        raise RuntimeError("lake down")
    """_daily_aggregate 湖异常 → 空态（snapshot/heat/snapshot 不 500）。"""

    def _boom(*a, **k):
        raise RuntimeError("lake down")

    monkeypatch.setattr(pq_mod, "read_daily", _boom)
    s = client.get("/api/market/snapshot")
    assert s.status_code == 200 and s.json()["rows"] == []
    h = client.get("/api/market/heat")
    assert h.status_code == 200 and h.json()["gainers"] == []


def test_endpoints_with_empty_lake(client, monkeypatch):
    """空湖（schema 空 DataFrame）→ 空结构而非 500。"""
    import polars as pl

    from lquant.data.store import parquet as pq_mod

    empty = pl.DataFrame(schema={"trade_date": pl.Date, "symbol": pl.String,
                                 "close": pl.Float64, "pre_close": pl.Float64,
                                 "amount": pl.Float64, "turnover_rate": pl.Float64})
    monkeypatch.setattr(pq_mod, "read_daily", lambda *a, **k: empty.lazy())
    r = client.get("/api/market/breadth")
    assert r.status_code == 200
    assert r.json() == {"latest": None, "history": []}
    s = client.get("/api/market/snapshot")
    assert s.status_code == 200 and s.json()["rows"] == []
    h = client.get("/api/market/heat")
    assert h.status_code == 200 and h.json()["gainers"] == []
    b = client.get("/api/market/batch", params={"symbols": "600519"})
    assert b.status_code == 200 and b.json()["latest"] == []


def test_index_and_sectors_empty_result(client, monkeypatch):
    """查询成功但无数据 → 空列表（index / sectors 空态分支）。"""
    from contextlib import contextmanager

    import polars as pl

    from lquant.server.api import market as market_mod

    class _EmptyCon:
        def execute(self, sql, *a, **k):
            class _R:
                def pl(self):
                    if "dragon_tiger" in sql:
                        # 结构不对的非空帧 → heat 内部炸掉进 except 兜底分支
                        return pl.DataFrame({"x": [1]})
                    return pl.DataFrame()

            return _R()

    @contextmanager
    def fake_reader():
        yield _EmptyCon()

    monkeypatch.setattr(market_mod, "reader", fake_reader)
    assert client.get("/api/market/index").json() == []
    assert client.get("/api/market/sectors").json() == []


def test_limit_ratio_map_by_board_and_st(client):
    """涨跌停阈值来自**规则表**（与回测/模拟盘同源），不再硬编码代码段。

    覆盖：北交所 30% / 创业板·科创板 20% / 主板 10%，
    以及此前完全漏掉的 **主板 ST 5%** 与 **科创 ETF 20%**。
    """
    from lquant.server.api import market as market_mod

    syms = ["830001.BJ", "430047.BJ", "920001.BJ", "300750.SZ", "688111.SH",
            "600519.SH", "588000.SH", "159915.SZ"]
    base, st = market_mod._limit_ratio_map(syms)

    assert base["830001.BJ"] == pytest.approx(0.30)
    assert base["430047.BJ"] == pytest.approx(0.30)
    assert base["920001.BJ"] == pytest.approx(0.30)
    assert base["300750.SZ"] == pytest.approx(0.20)
    assert base["688111.SH"] == pytest.approx(0.20)
    assert base["600519.SH"] == pytest.approx(0.10)
    # 科创 ETF 是 20%（旧硬编码按「其余 10%」处理 → 涨跌停家数偏高）
    assert base["588000.SH"] == pytest.approx(0.20)
    assert base["159915.SZ"] == pytest.approx(0.20)
    # 主板 ST 收窄到 5%（旧实现完全没有 ST 概念 → ST 涨停被漏计）
    assert st["600519.SH"] == pytest.approx(0.05)
    # 创业板/科创板 ST 仍是 20%
    assert st["300750.SZ"] == pytest.approx(0.20)


def test_collect_demo_round(client):
    """手动触发一轮 demo 采集（endpoint 返回采集健康度）。"""
    r = client.post("/api/market/collect", json={"demo": True})
    assert r.status_code == 200, r.text
