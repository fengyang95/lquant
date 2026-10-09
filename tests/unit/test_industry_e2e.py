"""行业分析端到端（合成演示库，离线可跑）。

与 `test_industry_analysis.py` 的分工：那边测**纯函数的方向与边界**，这里测
「真实数据湖 → 取数 → 聚合 → 报告/榜单」这条链路真的跑得通。

为什么必须有这一层：取数层的绝大多数分支（全市场日线读取、PIT as-of join、
行业聚合成交额、成员名称映射、缓存）在纯函数测试里永远走不到，而它们恰恰是
「换了 provider、湖里少一列、分类表没同步」时最先炸的地方。
"""
from __future__ import annotations

import os

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

#: 演示区间：需覆盖 RRG 的 320 个交易日预热，否则趋势角度拿不到象限。
START = "2024-01-01"
END = "2026-06-30"


@pytest.fixture(scope="module")
def lake_env(tmp_path_factory, request):
    base = tmp_path_factory.mktemp("industry_e2e")
    prev_root = os.environ.get("LQ_ROOT")
    prev_cwd = os.getcwd()
    os.environ["LQ_ROOT"] = str(base)
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
    generate_demo(start=START, end=END)

    # 面板缓存是进程级：本模块的用例之间共享同一份（同 asof 同 std），
    # 别的模块用别的 tmp 湖，不会串。
    def _cleanup() -> None:
        from lquant.industry import clear_industry_cache

        clear_industry_cache()
        os.chdir(prev_cwd)
        if prev_root is None:
            os.environ.pop("LQ_ROOT", None)
        else:
            os.environ["LQ_ROOT"] = prev_root
        get_settings.cache_clear()

    request.addfinalizer(_cleanup)
    yield base


@pytest.fixture(scope="module")
def client(lake_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------------------------------------------------------- 榜单


def test_rotation_covers_every_demo_industry(lake_env):
    from lquant.core.db import reader
    from lquant.industry import industry_rotation, list_industries
    from lquant.industry.loader import resolve_asof

    day = resolve_asof(None)
    with reader() as con:
        expected = set(list_industries(con, day, "SW")["industry_name"].to_list())
    out = industry_rotation(None, "SW", 20)
    got = {r["industry_name"] for r in out["rows"]}
    assert got == expected
    assert len(out["rows"]) >= 3
    # 排名必须从 1 开始连续（前端靠它排序），分位随之单调
    assert [r["rank"] for r in out["rows"]] == list(range(1, len(out["rows"]) + 1))
    assert out["rows"][0]["percentile"] == pytest.approx(100.0)


def test_rotation_rows_have_stable_columns(lake_env):
    from lquant.industry import industry_rotation

    out = industry_rotation(None, "SW", 20)
    row = out["rows"][0]
    for col in ("industry_code", "industry_name", "n_members", "r20", "r60",
                "amount_20d", "pe_median", "pb_median", "rank", "percentile"):
        assert col in row, f"轮动榜缺列 {col}（前端按列取值，缺列整页白屏）"


def test_list_returns_members_and_last_return(lake_env):
    from lquant.industry import list_industry_names

    out = list_industry_names(None, "SW")
    assert out["std"] == "SW"
    assert out["industries"]
    for row in out["industries"]:
        assert row["n_members"] >= 1
        assert isinstance(row["last_ret"], float)


# ---------------------------------------------------------------- 单行业报告


def test_analyze_industry_full_report(lake_env):
    """演示库自带行情/估值/财务/基准 → 五个角度应当全部可用。"""
    from lquant.industry import analyze_industry

    rep = analyze_industry("银行")
    assert rep["industry"] == "银行"
    assert rep["std"] == "SW"
    assert [a["id"] for a in rep["angles"]] == [
        "trend", "prosperity", "valuation", "capital", "breadth"]

    by_id = {a["id"]: a for a in rep["angles"]}
    # 行情 + 估值 + 财务 + 基准齐备时，这四类必须真的算出来（否则是回归）
    for angle_id in ("trend", "prosperity", "valuation", "breadth"):
        assert by_id[angle_id]["available"], (
            f"{angle_id} 不可用：{by_id[angle_id]['hint']}")
    assert rep["score"]["score"] is not None
    assert rep["score"]["n_scored"] >= 4

    # 趋势角度必须给出 RRG（演示区间足够长）与全行业排名
    trend = {m["key"]: m for m in by_id["trend"]["metrics"]}
    assert trend["rank20"]["value"] is not None
    assert by_id["trend"]["extra"]["rrg"] is not None
    assert trend["rs_ratio"]["value"] is not None


def test_analyze_industry_resolves_code_and_name_identically(lake_env):
    from lquant.industry import analyze_industry

    by_name = analyze_industry("银行")
    by_code = analyze_industry(by_name["industry_code"])
    assert by_code["industry"] == by_name["industry"]
    assert by_code["score"]["score"] == by_name["score"]["score"]


def test_analyze_industry_respects_asof(lake_env):
    """观察日提前，报告口径必须跟着提前（PIT 与取数窗口都随 asof 走）。"""
    from lquant.industry import analyze_industry

    early = analyze_industry("银行", "2025-03-31")
    late = analyze_industry("银行", None)
    assert early["asof"] == "2025-03-31"
    assert late["asof"] > early["asof"]
    # 两个观察日的趋势分不应该恰好相同（否则说明 asof 根本没生效）
    et = next(a for a in early["angles"] if a["id"] == "trend")
    lt = next(a for a in late["angles"] if a["id"] == "trend")
    assert et["score"] is not None and lt["score"] is not None
    assert et["score"] != lt["score"]


def test_analyze_industry_unknown_raises(lake_env):
    from lquant.industry import analyze_industry

    with pytest.raises(KeyError):
        analyze_industry("这个行业不存在")


def test_report_is_json_serializable(lake_env):
    """NaN/Infinity 是非法 JSON，会让前端整页白屏 —— 端到端再验一次。"""
    import json

    from lquant.industry import analyze_industry

    text = json.dumps(analyze_industry("银行"), ensure_ascii=False)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text)["industry"] == "银行"


# ---------------------------------------------------------------- HTTP


def test_http_analysis_and_rotation(client):
    rot = client.get("/api/industry/rotation", params={"window": 20})
    assert rot.status_code == 200
    body = rot.json()
    assert body["rows"]
    name = body["rows"][0]["industry_name"]

    rep = client.get(f"/api/industry/{name}/analysis")
    assert rep.status_code == 200
    assert rep.json()["industry"] == name


def test_http_list_and_angles(client):
    assert client.get("/api/industry/list").status_code == 200
    angles = client.get("/api/industry/angles").json()["angles"]
    assert len(angles) == 5


def test_http_unknown_industry_404_lists_available(client):
    r = client.get("/api/industry/绝对不存在的行业/analysis")
    assert r.status_code == 404
    # 有数据时必须把可用行业列出来，用户不用回代码里翻
    assert "可用行业" in r.json()["detail"]


def test_http_bad_asof_422(client):
    assert client.get("/api/industry/银行/analysis?asof=2026-13-01").status_code == 422


def test_lake_is_not_empty_and_source_is_demo(lake_env):
    """守住前提：本文件的结论只对合成演示库成立。"""
    from lquant.data.store.parquet import read_daily

    lake = read_daily().collect()
    assert lake.height > 0
    assert set(lake["source"].to_list()) == {"demo"}
    assert isinstance(lake, pl.DataFrame)
