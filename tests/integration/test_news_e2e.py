"""行业资讯全链路 e2e：akshare 全 mock → POST /api/news/tasks 采集 →
入库（去重）→ link 管线（个股词表 + 行业关键词）→ GET /api/news/items /
summary 命中。

隔离方式沿用 test_backfill_task_e2e.py 结论：LQ_ROOT env + chdir +
get_settings.cache_clear（不 patch get_settings 模块属性）。

mock 锚点（与 test_api_news.py 一致，patch 必须落在源模块属性上）：
- 采集：lquant.news.sources.telegraph 的 akshare 调用（cls 电报假 DataFrame，
  列名按 akshare 1.18.94 实测：标题/内容/发布日期/发布时间）
- 词表：lquant.data.providers.akshare.AkShareProvider.securities（假 DataFrame
  含 平安银行→000001.SZ），link 管线真实跑，不打桩 build_name_to_code
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd
import polars as pl
import pytest
from fastapi.testclient import TestClient

import lquant.server.api.news as news_api
from lquant.core.config import get_settings
from lquant.server.main import create_app


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离；拷入真实 config（news.yaml 的
    industry_keywords / industry_names 参与 link 断言）。"""
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "config", tmp_path / "config")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    # news API 的 _schema_ready 以 duckdb_path 字符串为 key，duckdb_path 相对时
    # 跨测试目录会命中旧 key 跳过建表 → 每个测试强制重置
    news_api._schema_ready.clear()
    yield tmp_path
    get_settings.cache_clear()
    news_api._schema_ready.clear()


@pytest.fixture
def client(fake_env):
    """TestClient + 惰性建表（news_item / news_task DDL 由 API 首访建立）。"""
    with TestClient(create_app()) as c:
        yield c


def _cls_df() -> pd.DataFrame:
    """财联社电报假 DataFrame：一条命中平安银行 + 半导体关键词，
    一条无关（不应被 000001.SZ 过滤命中）。发布日期取今天，summary 按
    published_at 当日计数才命中。"""
    today = date.today().isoformat()
    # 时间必须是常量：若每次调用取 datetime.now()，第二次采集跨秒时
    # external_id（发布日期+发布时间）变化，去重失效（测试曾因此翻倍）
    return pd.DataFrame({
        "标题": ["平安银行发布年报", "某锂矿公司公告"],
        "内容": [
            "平安银行今日披露年度报告，半导体板块表现活跃。",
            "与银行无关的快讯内容。",
        ],
        "发布日期": [today, today],
        # 两行时间必须不同：cls external_id = 发布日期+发布时间，相同会被去重合并
        "发布时间": ["09:30:00", "00:00:01"],
    })


@pytest.fixture
def mock_akshare(monkeypatch):
    """akshare 全 mock：电报采集 + securities 词表。"""
    monkeypatch.setattr(ak, "stock_info_global_cls", _cls_df)

    class _FakeProvider:
        def securities(self):  # noqa: ANN201, ANN001
            return pl.DataFrame({
                "name": ["平安银行", "万科A"],
                "symbol": ["000001.SZ", "000002.SZ"],
            })

    monkeypatch.setattr(
        "lquant.data.providers.akshare.AkShareProvider", _FakeProvider)


def test_news_e2e_collect_link_api(client, mock_akshare):
    """POST tasks（ok）→ items?symbol=000001.SZ 命中 → summary 计数正确
    → link 管线真实跑（symbols/industry_code 非空）。"""
    # 1. 创建并同步执行采集任务
    resp = client.post("/api/news/tasks", json={"kind": "manual", "sources": ["cls_telegraph"]})
    assert resp.status_code == 200, resp.text
    task = resp.json()["data"]
    assert task["status"] == "ok", task
    assert task["rows_written"] == 2
    assert task["sources_status"]["cls_telegraph"]["status"] == "ok"
    assert task["sources_status"]["cls_telegraph"]["rows"] == 2

    # 2. 个股过滤命中：link_symbols 用假词表真实匹配出 000001.SZ
    resp = client.get("/api/news/items", params={"symbol": "000001.SZ"})
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    hit = items[0]
    assert hit["title"] == "平安银行发布年报"
    assert "000001.SZ" in hit["symbols"]
    # 行业关键词「半导体」在正文里命中 → industry_code 来自 news.yaml
    assert hit["industry_code"] == "BK103010"

    # 3. 全量 items：第二条未被关联到 000001.SZ
    resp = client.get("/api/news/items")
    all_items = resp.json()["data"]["items"]
    assert len(all_items) == 2
    other = next(i for i in all_items if i["news_id"] != hit["news_id"])
    assert "000001.SZ" not in (other["symbols"] or [])

    # 4. summary：当日按 source 计数 + 行业 top
    resp = client.get("/api/news/summary")
    assert resp.status_code == 200
    summary = resp.json()["data"]
    by_source = {r["source"]: r["count"] for r in summary["by_source"]}
    assert by_source.get("telegraph") == 2
    top = {r["industry_code"]: r["count"] for r in summary["top_industries"]}
    assert top.get("BK103010") == 1

    # 5. 任务列表可见
    resp = client.get("/api/news/tasks")
    assert resp.status_code == 200
    assert any(t["task_id"] == task["task_id"] and t["status"] == "ok"
               for t in resp.json()["data"])


def test_news_e2e_idempotent_recollect(client, mock_akshare):
    """重复采集不翻倍（external_id 去重），任务计数反映真实新增 0。"""
    resp = client.post("/api/news/tasks", json={"kind": "manual", "sources": ["cls_telegraph"]})
    assert resp.status_code == 200
    assert resp.json()["data"]["rows_written"] == 2

    resp = client.post("/api/news/tasks", json={"kind": "manual", "sources": ["cls_telegraph"]})
    assert resp.status_code == 200
    assert resp.json()["data"]["rows_written"] == 0

    resp = client.get("/api/news/items", params={"limit": 200})
    assert len(resp.json()["data"]["items"]) == 2
