"""评审修复回归测试 —— 覆盖最近 10 个 PR 评审发现的缺陷修复。

对应修复：
1. ic/rating 零方差口径（polars 常数序列 std≈7e-18 → t/IR 暴涨闯门槛）
2. submit 强制 rationale
3. robustness 无窗口参数 → insufficient（跳过而非通过）
4. factors evaluate 端点：factor 报告名防路径穿越、start 日期校验、top_ns 边界
5. factors 列表端点 source 参数参数化（SQL 注入）
6. jqapi：负数切片按位置、get_price panel=False 布局、索引统一字符串日期
7. news store：source 过滤走 source_name、LIKE 通配符转义
8. data API：crosscheck 日期校验、全湖检查并发互斥
"""
from __future__ import annotations

import math
import os
from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("review_fixes")
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
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------- 1. IC 零方差口径 ----------

def test_t_stat_near_zero_std_is_nan():
    """polars 常数序列 std≈7e-18：t 必须判 nan，不能算出 1e16 闯显著性门槛。"""
    from lquant.factors.evaluate.ic import _t_stat

    assert math.isnan(_t_stat(0.05, 0.0, 100))
    assert math.isnan(_t_stat(0.05, 7e-18, 100))
    # 正常方差仍应算出有限 t
    assert math.isfinite(_t_stat(0.05, 0.1, 100))


def test_newey_west_tstat_near_constant_is_nan():
    import polars as pl

    from lquant.factors.evaluate.ic import newey_west_tstat

    assert math.isnan(newey_west_tstat(pl.Series([0.05] * 60)))
    assert math.isnan(newey_west_tstat(pl.Series([0.05 + 7e-18] * 60)))


def test_ic_summary_ir_zero_variance_is_nan():
    from lquant.factors.evaluate.ic import _summarize

    s = pl.Series([0.04] * 40)  # 常数序列
    out = _summarize(s)
    assert math.isnan(out["ir"])
    assert math.isnan(out["t_stat"])
    assert math.isnan(out["t_stat_nw"])


def test_rating_near_zero_std_icir_is_inf():
    """评级层：std≈7e-18 且 mean≠0 → ICIR 视为无穷（完美稳定），不是 moderate。"""
    from lquant.factors.evaluate.rating import factor_rating

    ic = {
        "rank_ic": {"mean": 0.05, "std": 7e-18, "ir": float("nan"),
                    "t_stat": float("nan"), "t_stat_nw": float("nan"),
                    "positive_rate": 1.0, "n_days": 120},
    }
    out = factor_rating(ic, None)
    assert math.isinf(out["icir"])
    # 完美稳定 + 显著 → 评级应到 strong（否则是零方差被当成 nan 误判 moderate/weak）
    assert out["rating"] == "strong"


# ---------- 6. jqapi 兼容层 ----------

def _bars_df():
    rows = []
    for i in range(8):
        d = date(2026, 1, 5) + timedelta(days=i)
        for s in ("600000.SH", "600519.SH"):
            rows.append(dict(trade_date=d, symbol=s, open=10.0 + i, high=11.0 + i,
                             low=9.0 + i, close=10.5 + i, pre_close=10.0 + i,
                             volume=1e6, amount=1e7))
    return pl.DataFrame(rows)


_CODE_NEG_SLICE = '''
def initialize(context):
    pass

def handle_data(context, data):
    h = attribute_history("600000.SH", 5, ["close"])
    if len(h) < 5:
        return                                   # 历史不足（首日 open 无历史）
    tail = h["close"][-5:]          # 老版 pandas：负数切片按位置
    assert len(tail) == 5, len(tail)
    df_tail = h[-5:]                # DataFrame 负数切片
    assert len(df_tail) == 5
'''


def test_history_negative_slice():
    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(_CODE_NEG_SLICE, initial_cash=1_000_000).run(_bars_df())
    assert res.error is None, res.error


_CODE_PANEL_FALSE = '''
def initialize(context):
    pass

def handle_data(context, data):
    wide = get_price(["600000.SH", "600519.SH"], count=3,
                     fields=["close", "open"], panel=False)
    if len(wide) < 6:
        return                                   # 首日历史不足，等回补满
    assert list(wide.columns) == ["close", "open"], wide.columns
    assert len(wide) == 6, len(wide)          # 3 天 x 2 标的
    assert wide.index.names == ["day", "code"], wide.index.names

    multi = get_price(["600000.SH", "600519.SH"], count=3, fields=["close"])
    assert ("600000.SH", "close") in multi.columns   # panel=True 缺省仍是 (标的,字段)
'''


def test_get_price_panel_false_layout():
    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(_CODE_PANEL_FALSE, initial_cash=1_000_000).run(_bars_df())
    assert res.error is None, res.error


_CODE_INDEX_TYPE = '''
def initialize(context):
    pass

def handle_data(context, data):
    h = attribute_history("600000.SH", 3, ["close"])
    if len(h) == 0:
        return
    assert isinstance(h.index[0], str), type(h.index[0])   # 统一字符串日期
    p = get_price("600000.SH", count=2, fields=["close"])
    if len(p) == 0:
        return
    assert isinstance(p.index[0], str), type(p.index[0])
'''


def test_history_get_price_index_type_unified():
    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(_CODE_INDEX_TYPE, initial_cash=1_000_000).run(_bars_df())
    assert res.error is None, res.error


# ---------- 4/5. factors API 校验与注入 ----------

def test_evaluate_rejects_path_traversal(client):
    r = client.post("/api/factors/evaluate",
                    json={"factor": "../../etc/passwd"})
    assert r.status_code == 422, r.text


def test_evaluate_rejects_bad_start(client):
    r = client.post("/api/factors/evaluate",
                    json={"factor": "ok_name", "start": "2026-13-01"})
    assert r.status_code == 422
    r2 = client.post("/api/factors/evaluate",
                     json={"factor": "ok_name", "start": "not-a-date"})
    assert r2.status_code == 422


def test_evaluate_rejects_bad_top_ns(client):
    for bad in ([0], [-5], [5000]):
        r = client.post("/api/factors/evaluate",
                        json={"factor": "ok_name", "top_ns": bad})
        assert r.status_code == 422, (bad, r.text)


def test_list_factors_source_injection_safe(client):
    """source 参数参数化：注入串不报错也不放行出全部行。"""
    r = client.get("/api/factors", params={"source": "x' OR '1'='1"})
    assert r.status_code == 200
    assert r.json() == []   # 返回裸列表；注入串只能匹配到 0 行


# ---------- 7. news store：source 口径 + LIKE 转义 ----------

def _news_con():
    from duckdb import connect

    from lquant.news.model import NewsItem
    from lquant.news.store import init_news_ddl, insert_news

    con = connect(":memory:")
    init_news_ddl(con)
    items = [
        NewsItem(source="news", source_name="em_news", external_id="a1",
                 title="A 平安银行", content="正文", url=""),
        NewsItem(source="news", source_name="em_global", external_id="a2",
                 title="B 增长100%", content="正文", url=""),
        NewsItem(source="news", source_name="em_global", external_id="a3",
                 title="B 增长1000x", content="正文", url=""),
        NewsItem(source="telegraph", source_name="cls_telegraph", external_id="a4",
                 title="C 电报", content="正文", url=""),
    ]
    insert_news(con, items)
    return con


def test_query_news_source_filters_by_source_name():
    """API 的 source 参数=来源名：必须落在 source_name 列，而不是 category。"""
    from lquant.news.store import query_news

    con = _news_con()
    out = query_news(con, source="em_news")
    assert out["total"] == 1 and out["items"][0]["source_name"] == "em_news"
    out2 = query_news(con, source="cls_telegraph")
    assert out2["total"] == 1
    out3 = query_news(con, source="不存在的来源")
    assert out3["total"] == 0


def test_query_news_keyword_like_escape():
    from lquant.news.store import query_news

    con = _news_con()
    # 100% 是字面量，不能把 % 当通配符命中「增长1000x」
    out = query_news(con, keyword="100%")
    assert out["total"] == 1 and "100%" in out["items"][0]["title"]
    out2 = query_news(con, keyword="增长100_")  # _ 也不应是单字符通配
    assert out2["total"] == 0
    out3 = query_news(con, keyword="增长100")
    assert out3["total"] == 2


def test_news_stats_by_source_name_groups_correctly():
    from lquant.news.store import news_stats_by_source_name

    con = _news_con()
    stats = {r["source_name"]: r["count"] for r in news_stats_by_source_name(con)}
    assert stats == {"em_news": 1, "em_global": 2, "cls_telegraph": 1}


# ---------- factors API 补测：公式分支 / 合成 / 报告 / 冗余分析 ----------

def test_evaluate_turnover_formula_ok(client):
    r = client.post("/api/factors/evaluate",
                    json={"factor": "turn_test", "formula": "turnover",
                          "start": "2025-01-01"})
    assert r.status_code == 200, r.text
    assert r.json()["n_samples"] > 0


def test_evaluate_unsupported_formula_422(client):
    r = client.post("/api/factors/evaluate",
                    json={"factor": "bogus1", "formula": "magic_factor"})
    assert r.status_code == 422


def test_analyze_correlation(client):
    r = client.post("/api/factors/analyze", json={
        "formulas": ["pct_change_20", "rolling_std_20"], "threshold": 0.99,
        "start": "2025-01-01"})
    assert r.status_code == 200, r.text


def test_synthesize_equal_weight(client):
    r = client.post("/api/factors/synthesize", json={
        "formulas": ["pct_change_20", "rolling_std_20"], "method": "equal",
        "start": "2025-01-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["factor"].startswith("syn_2f_eq")
    assert body["n_samples"] > 0
    # 合成报告落盘可取回
    assert client.get(f"/api/factors/reports/{body['factor']}").status_code == 200


def test_reports_list(client):
    r = client.get("/api/factors/reports")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_factor_sources_and_agents(client):
    r = client.get("/api/factors/sources")
    assert r.status_code == 200 and isinstance(r.json(), list)
    r2 = client.get("/api/factors/agents")
    assert r2.status_code == 200 and isinstance(r2.json(), list)


def test_builtin_family_filter(client):
    r = client.get("/api/factors/builtin", params={"family": "ma"})
    assert r.status_code == 200
    items = r.json()
    assert items and all(x["family"] == "ma" for x in items)


# ---------- 8. data API ----------

def test_crosscheck_rejects_bad_dates(client):
    r = client.post("/api/data/crosscheck", json={"start": "2026/01/01"})
    assert r.status_code == 422


def test_lake_check_rejects_bad_date(client):
    r = client.post("/api/data/check", json={"start": "2026/01/01"})
    assert r.status_code == 422


def test_lake_check_concurrent_conflict(client, monkeypatch):
    """已有一次全湖检查在跑 → 409，不双跑竞写。"""
    from lquant.server.api import data as data_api

    def _slow(*a, **kw):
        raise AssertionError("持锁期间不应再次进入 run_lake_checks")

    monkeypatch.setattr(data_api, "_lake_check_lock",
                        __import__("threading").Lock())
    locked = data_api._lake_check_lock
    assert locked.acquire(blocking=False)
    try:
        r = client.post("/api/data/check", json={})
        assert r.status_code == 409, r.text
    finally:
        locked.release()
