"""行业分析的降级路径：**空库 / 缺表 / 少列**时必须给出可执行的说明。

单独一个文件而不是塞进 `test_industry_e2e.py`：两者都需要切换进程级 CWD，
放在同一个模块里两个 module 级夹具会互相打架。

为什么值得单独测：这些路径在真实环境里天天发生（新装的库、只同步了行情的库、
换 provider 少一列），而它们的正确行为不是「报错」，是**如实说明缺什么、
怎么补**。写错了表现是「页面空白且无提示」，比 500 更难查。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")


@pytest.fixture(scope="module")
def bare_env(tmp_path_factory, request):
    """一个**建了表但一行数据都没有**的库 + 空湖（模拟刚装好的环境）。"""
    base = tmp_path_factory.mktemp("industry_bare")
    prev_root = os.environ.get("LQ_ROOT")
    prev_cwd = os.getcwd()
    os.environ["LQ_ROOT"] = str(base)
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
        ensure_market_tables(con)

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


def test_build_universe_on_empty_lake_explains_what_is_missing(bare_env):
    from lquant.industry import clear_industry_cache
    from lquant.industry.loader import build_universe, resolve_asof

    clear_industry_cache()
    # build_universe 的契约是「已解析的观察日」；asof=None 由上层 resolve_asof 处理
    uni = build_universe(resolve_asof(None), None, 720)
    assert uni.std is None
    assert uni.industries.is_empty()
    assert uni.bars.is_empty()
    joined = " ".join(uni.notes)
    assert "industry_classify" in joined        # 缺行业分类 → 告诉用户跑什么
    assert "日线湖为空" in joined                # 缺行情 → 单独一条，不混为一谈


def test_analyze_industry_on_empty_lake_raises_keyerror(bare_env):
    """没有任何行业时不是「返回一份全 null 的报告」，而是让上层转 404。"""
    from lquant.industry import analyze_industry, clear_industry_cache

    clear_industry_cache()
    with pytest.raises(KeyError):
        analyze_industry("银行")


def test_rotation_and_list_on_empty_lake_return_empty(bare_env):
    from lquant.industry import (
        clear_industry_cache,
        industry_rotation,
        list_industry_names,
    )

    clear_industry_cache()
    rot = industry_rotation(None, None, 20)
    assert rot["rows"] == []
    assert rot["std"] is None
    assert any("industry_classify" in n for n in rot["notes"])

    clear_industry_cache()
    lst = list_industry_names(None, None)
    assert lst["industries"] == []


def test_http_404_on_empty_lake_is_actionable(bare_env):
    from fastapi.testclient import TestClient

    from lquant.industry import clear_industry_cache
    from lquant.server.main import create_app

    clear_industry_cache()
    with TestClient(create_app()) as client:
        r = client.get("/api/industry/银行/analysis")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "industry_classify" in detail or "lq data industry" in detail


# ---------------------------------------------------------------- 取数层细分分支


def test_build_universe_warns_when_requested_std_falls_back(bare_env, monkeypatch):
    """显式请求了湖里没有的行业标准 → 回退到首选，但必须**报出来**。

    申万与中信的行业划分不同，静默换标准等于让同一份报告的口径在用户不知情的
    情况下变了。
    """
    import polars as pl

    from lquant.industry import clear_industry_cache
    from lquant.industry import loader as L

    rows = pl.DataFrame({
        "symbol": ["600000.SH"], "std": ["SW"], "code": ["801780.SI"],
        "name": ["银行"], "std_date": [__import__("datetime").date(2010, 1, 1)],
    })
    monkeypatch.setattr(L, "_read_classify", lambda con, asof: rows)
    clear_industry_cache()
    uni = L.build_universe(L.resolve_asof(None), "CICS", 720)
    assert uni.std == "SW"
    assert any("已回退到" in n for n in uni.notes)


def test_build_universe_with_bars_but_no_classification(bare_env, monkeypatch):
    """有行情、没行业分类 → 明确说「行业归属为空」，而不是给一份空报告。"""
    import polars as pl

    from lquant.industry import clear_industry_cache
    from lquant.industry import loader as L

    bars = pl.DataFrame({
        "trade_date": [L.resolve_asof(None)],
        "symbol": ["600000.SH"], "close": [10.0],
    })
    monkeypatch.setattr(L, "_read_classify", lambda con, asof: pl.DataFrame())
    monkeypatch.setattr(L, "_read_market_bars", lambda asof, days: bars)
    clear_industry_cache()
    uni = L.build_universe(L.resolve_asof(None), None, 720)
    assert any("行业归属为空" in n for n in uni.notes)


def test_build_universe_without_close_column(bare_env, monkeypatch):
    """日线湖缺 close 列 → 单独一条提示，不混进「归属为空」。"""
    import datetime as dt

    import polars as pl

    from lquant.industry import clear_industry_cache
    from lquant.industry import loader as L

    today = L.resolve_asof(None)
    bars = pl.DataFrame({"trade_date": [today], "symbol": ["600000.SH"]})
    ic = pl.DataFrame({
        "symbol": ["600000.SH"], "std": ["SW"], "code": ["801780.SI"],
        "name": ["银行"], "std_date": [dt.date(2010, 1, 1)],
    })
    monkeypatch.setattr(L, "_read_classify", lambda con, asof: ic)
    monkeypatch.setattr(L, "_read_market_bars", lambda asof, days: bars)
    clear_industry_cache()
    uni = L.build_universe(today, None, 720)
    assert any("缺 close 列" in n for n in uni.notes)


def test_load_benchmark_empty_index_table(bare_env):
    """index_daily 表在、但没有候选基准的数据 → 明确返回不可用。"""
    from lquant.industry import clear_industry_cache
    from lquant.industry import loader as L

    clear_industry_cache()
    bench, symbol = L._load_benchmark(L.resolve_asof(None))
    assert bench.is_empty()
    assert symbol is None


def test_industry_daily_without_ret_column():
    import polars as pl

    from lquant.industry import loader as L

    assert L._industry_daily(pl.DataFrame({"symbol": ["a"]})).is_empty()


def test_load_valuation_history_degrades_when_read_raises(monkeypatch):
    from lquant.data.store import parquet as pq
    from lquant.industry import loader as L

    def boom(*a, **k):
        raise RuntimeError("daily_basic 读失败")

    monkeypatch.setattr(pq, "read_daily_basic", boom)
    assert L.load_valuation_history(["600000.SH"],
                                    __import__("datetime").date(2025, 6, 30)).is_empty()
