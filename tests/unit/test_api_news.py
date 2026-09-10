"""API 层：/api/news 路由（items/industries/sources/tasks/summary）。

同 test_api_data_tasks.py 模式：tmp 目录自包含环境，全链路离线。
POST tasks 的采集 runner（lquant.news.tasks._default_runner）打桩为空 ——
网络行为在 Task 3/4 采集器测试覆盖，这里只测 HTTP 契约。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_news")
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer

    with writer() as con:
        from lquant.news.store import init_news_ddl
        from lquant.news.tasks import init_news_task_ddl

        init_news_ddl(con)
        init_news_task_ddl(con)
    yield base
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_runner(monkeypatch):
    """采集 runner 与 link 词表 no-op：不触网，只测 HTTP 契约。

    _load_link_inputs 在函数内 import AkShareProvider —— patch 必须落在源模块
    属性上（打桩 lquant.news.tasks.build_name_to_code 挡不住它），否则有网环境
    会真实请求 akshare。securities() 假数据返回空 DataFrame，link 管线全降级。
    """
    import polars as pl

    class _FakeProvider:
        def securities(self):
            return pl.DataFrame({"name": [], "symbol": []})

    monkeypatch.setattr("lquant.news.tasks._default_runner", lambda day, src: [])
    monkeypatch.setattr("lquant.news.tasks.build_name_to_code", lambda rows: {})
    monkeypatch.setattr(
        "lquant.data.providers.akshare.AkShareProvider", _FakeProvider)


def _seed_news(n: int = 1, **overrides) -> None:
    from datetime import datetime

    from lquant.core.db import writer
    from lquant.news.model import NewsItem
    from lquant.news.store import insert_news

    items = []
    for i in range(n):
        items.append(NewsItem(
            source=overrides.get("source", "em_news"),
            source_name=overrides.get("source_name", "em"),
            external_id=overrides.get("external_id", f"ext-{i}"),
            title=overrides.get("title", f"标题{i}"),
            content=overrides.get("content", f"正文{i}"),
            url="https://example.com",
            symbols=overrides.get("symbols", ()),
            industry_code=overrides.get("industry_code"),
            published_at=overrides.get("published_at",
                                       datetime(2026, 9, 10, 8, 0, i)),
        ))
    with writer() as con:
        from lquant.news.store import init_news_ddl

        init_news_ddl(con)
        insert_news(con, items)


def _seed_task(task_id: str, status: str, sources_status: dict) -> None:
    import json

    from lquant.core.db import writer
    from lquant.news.tasks import init_news_task_ddl

    with writer() as con:
        init_news_task_ddl(con)
        con.execute(
            "INSERT OR IGNORE INTO news_task VALUES (?, 'manual', ?, ?, ?, 0,"
            " NULL, NULL, NULL)",
            [task_id, json.dumps({"sources": ["em_news"]}),
             status, json.dumps(sources_status)],
        )


# ---------- items ----------

def test_items_empty_ok(client):
    r = client.get("/api/news/items")
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body == {"total": 0, "items": []}


def test_items_validation(client):
    assert client.get("/api/news/items", params={"limit": 201}).status_code == 422
    assert client.get("/api/news/items",
                      params={"keyword": "k" * 101}).status_code == 422
    assert client.get("/api/news/items", params={"day": "not-a-date"}).status_code == 422


def test_items_filters(client):
    _seed_news(2)
    _seed_news(1, source="cls_telegraph", source_name="cls", title="特别关键词")
    r = client.get("/api/news/items", params={"keyword": "特别关键词"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["total"] == 1 and body["items"][0]["source"] == "cls_telegraph"

    r2 = client.get("/api/news/items", params={"source": "em_news", "limit": 1})
    assert r2.json()["data"]["total"] == 2
    assert len(r2.json()["data"]["items"]) == 1

    r3 = client.get("/api/news/items", params={"day": "2026-09-10"})
    assert r3.json()["data"]["total"] == 3
    r4 = client.get("/api/news/items", params={"day": "2020-01-01"})
    assert r4.json()["data"]["total"] == 0


# ---------- industries ----------

def test_industries_with_name(client):
    _seed_news(1, external_id="ind-1", industry_code="BK101010")
    _seed_news(1, external_id="ind-2", industry_code="BK999999")
    rows = client.get("/api/news/industries").json()["data"]
    assert rows, "应有行业统计"
    by_code = {r["industry_code"]: r for r in rows}
    assert by_code["BK101010"]["industry_name"] == "锂电"
    assert by_code["BK999999"]["industry_name"] is None
    assert by_code["BK101010"]["count"] >= 1


# ---------- sources ----------

def test_sources_union_registry(client):
    _seed_news(1)
    rows = client.get("/api/news/sources").json()["data"]
    by_source = {r["source"]: r for r in rows}
    # 注册表里 0 行的来源也要出现
    for src in ("cls_telegraph", "sina_7x24", "em_news"):
        assert src in by_source, f"{src} 应出现在来源清单"
    assert by_source["em_news"]["count"] >= 1
    # cls_telegraph 可能有 items_filters 留下的行，这里只断言"注册表并集"语义
    assert isinstance(by_source["cls_telegraph"]["count"], int)


# ---------- summary ----------

def test_summary_counts(client):
    _seed_news(2, published_at=None,  # published_at 缺 → 用采集时间兜底(今天)
               external_id="sum-seed")
    r = client.get("/api/news/summary")
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert {"day", "by_source", "by_category", "top_industries"} <= set(body)
    srcs = {r["source"]: r["count"] for r in body["by_source"]}
    assert srcs.get("em_news", 0) >= 1
    cats = {r["category"] for r in body["by_category"]}
    assert "news" in cats  # em_news 的注册表 category

    r2 = client.get("/api/news/summary", params={"day": "2020-01-01"})
    body2 = r2.json()["data"]
    assert body2["by_source"] == [] and body2["top_industries"] == []


# ---------- tasks ----------

def test_post_task_ok_then_conflict(client):
    r = client.post("/api/news/tasks", json={"kind": "manual"})
    assert r.status_code == 200, r.text
    task = r.json()["data"]
    assert task["status"] == "ok"
    assert task["rows_written"] == 0
    task_id = task["task_id"]

    # ok 任务不可 retry → 409
    assert client.post(f"/api/news/tasks/{task_id}/retry").status_code == 409

    # 存量 pending/running → 互斥 409
    _seed_task("news_pending_x", "pending", {})
    r2 = client.post("/api/news/tasks", json={"kind": "manual"})
    assert r2.status_code == 409, r2.text
    # 存量 running → 409
    _seed_task("news_running_x", "running", {})
    assert client.post("/api/news/tasks", json={"kind": "manual"}).status_code == 409


def test_post_task_default_all_sources(client):
    """不传 sources → 默认全注册表，params 里可查到两个采集器。"""
    from lquant.core.db import writer

    # 清掉前面用例种下的 pending/running 残留，避免互斥 409
    with writer() as con:
        con.execute("DELETE FROM news_task WHERE task_id LIKE 'news_%_x'")
    r = client.post("/api/news/tasks", json={"kind": "manual"})
    assert r.status_code == 200, r.text
    task_id = r.json()["data"]["task_id"]
    rows = client.get("/api/news/tasks").json()["data"]
    row = next(x for x in rows if x["task_id"] == task_id)
    assert set(row["params"]["sources"]) >= {"cls_telegraph", "em_news"}


def test_post_task_422_bad_params(client):
    assert client.post("/api/news/tasks",
                       json={"kind": "bogus"}).status_code == 422
    assert client.post("/api/news/tasks",
                       json={"kind": "manual", "date": "not-a-date"}).status_code == 422
    assert client.post("/api/news/tasks", json={
        "kind": "manual", "sources": ["no_such_source"]}).status_code == 422


def test_retry_partial_task(client):
    _seed_task("news_partial_x", "partial",
               {"em_news": {"status": "failed", "rows": 0, "error": "boom"}})
    r = client.post("/api/news/tasks/news_partial_x/retry")
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["status"] == "ok"
    assert body["task_id"] == "news_partial_x"


def test_task_not_found_404(client):
    assert client.post("/api/news/tasks/ghost/retry").status_code == 404


def test_tasks_list(client):
    rows = client.get("/api/news/tasks").json()["data"]
    assert isinstance(rows, list) and rows
    assert {"task_id", "kind", "status", "rows_written"} <= set(rows[0])
