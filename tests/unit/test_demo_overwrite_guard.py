"""演示数据护栏：合成数据绝不覆盖真实观测。

回归背景（2026-09-18 事故，当天发生两次）：`lq data demo` 的落点是 CWD 相对的
./data/parquet，而隔离 worktree 的标准姿势是把 data/parquet **软链到主仓真实
湖** —— 于是在 worktree 里跑一次演示数据生成，就沿软链把 20 只真代码股票 +
10 只真代码 ETF 的真实行覆盖成合成值。实测 21,300 行 / 3 个年分区 /
2024-01-01~2026-09-18，且**没有任何检查报警**（schema 一致、质量断言全过）。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest


@pytest.fixture
def lake_env(tmp_path, monkeypatch):
    """隔离湖：LQ_ROOT + chdir + cache_clear（沿用 test_backfill_task_e2e 姿势）。

    建好 DDL：generate_demo 要写 trade_calendar / security / etf_meta。
    """
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS
    from lquant.market.schema import ensure_market_tables

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    ensure_market_tables(con)
    con.close()

    yield tmp_path
    get_settings.cache_clear()


def _bars(symbols: list[str], dates: list[date], source: str, price: float) -> pl.DataFrame:
    rows = [(s, d) for s in symbols for d in dates]
    return pl.DataFrame({
        "symbol": [s for s, _ in rows],
        "trade_date": pl.Series([d for _, d in rows], dtype=pl.Date),
        "open": [price] * len(rows), "high": [price] * len(rows),
        "low": [price] * len(rows), "close": [price] * len(rows),
        "pre_close": [price] * len(rows), "volume": [1000.0] * len(rows),
        "amount": [price * 1000] * len(rows), "adj_factor": [1.0] * len(rows),
        "sec_type": ["stock"] * len(rows),
        "source": [source] * len(rows),
    })


D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def test_write_daily_rejects_demo_over_real(lake_env):
    """演示行要覆盖真实行 → 拒绝，且湖内容不变。"""
    from lquant.core.errors import DataQualityError
    from lquant.data.store.parquet import read_daily, write_daily

    write_daily(_bars(["000001.SZ"], [D1], "baostock", 10.0))
    before = read_daily().collect()
    assert len(before) == 1

    with pytest.raises(DataQualityError, match="拒绝用演示数据覆盖真实数据"):
        write_daily(_bars(["000001.SZ"], [D1], "demo", 99.0))

    after = read_daily().collect()
    assert len(after) == 1
    assert after["close"].to_list() == [10.0]          # 真实值原样保留
    assert after["source"].to_list() == ["baostock"]


def test_write_daily_allows_demo_over_empty_and_demo(lake_env):
    """空湖 / 湖里只有演示数据 → 允许（幂等重跑演示数据是正常用法）。"""
    from lquant.data.store.parquet import read_daily, write_daily

    write_daily(_bars(["000001.SZ"], [D1], "demo", 10.0))
    assert len(read_daily().collect()) == 1
    write_daily(_bars(["000001.SZ"], [D1, D2], "demo", 11.0))
    got = read_daily().collect()
    assert len(got) == 2
    assert set(got["close"].to_list()) == {11.0}


def test_write_daily_allows_real_over_demo(lake_env):
    """真实数据覆盖演示数据 → 允许（`lq data sync` 覆盖演示湖是设计路径）。"""
    from lquant.data.store.parquet import read_daily, write_daily

    write_daily(_bars(["000001.SZ"], [D1], "demo", 10.0))
    write_daily(_bars(["000001.SZ"], [D1], "baostock", 20.0))
    got = read_daily().collect()
    assert got["close"].to_list() == [20.0]
    assert got["source"].to_list() == ["baostock"]


def test_write_daily_demo_on_new_dates_is_allowed(lake_env):
    """演示数据只新增日期（不覆盖真实键）→ 不拦：护栏按 (symbol, date) 精确判定。"""
    from lquant.data.store.parquet import read_daily, write_daily

    write_daily(_bars(["000001.SZ"], [D1], "baostock", 10.0))
    write_daily(_bars(["000001.SZ"], [D2], "demo", 99.0))
    got = read_daily().collect()
    assert len(got) == 2
    assert set(got["source"].to_list()) == {"baostock", "demo"}


def test_reject_demo_overwrite_early_returns(lake_env):
    """护栏自身的兜底分支：判不了就别拦（宁可漏也不误杀正常写入）。

    三种「判不了」都必须直接放行 —— 否则会在缺列/空帧的正常路径上炸掉回填。
    """
    from lquant.data.store.parquet import _reject_demo_overwrite

    real = _bars(["000001.SZ"], [D1], "baostock", 10.0)
    demo = _bars(["000001.SZ"], [D1], "demo", 99.0)
    path = lake_env / "dummy.parquet"

    # ① 缺 source 列（老 schema / 非日线其它 writer 复用本函数）
    _reject_demo_overwrite(real.drop("source"), demo, path)
    # ② 任一侧空帧
    _reject_demo_overwrite(real.head(0), demo, path)
    _reject_demo_overwrite(real, demo.head(0), path)
    # ③ 缺 (symbol, trade_date) 键列
    _reject_demo_overwrite(real.drop("symbol"), demo, path)
    _reject_demo_overwrite(real, demo.drop("trade_date"), path)
    # ④ 新帧里没有演示行 → 与真实/演示无关，放行
    _reject_demo_overwrite(real, _bars(["000002.SZ"], [D1], "baostock", 10.0), path)


def test_refuse_guard_degrades_quietly(lake_env, monkeypatch):
    """粗筛读不动湖时不炸 —— 逐键的精确护栏在写入层兜底。

    读路径上抛异常会把「生成演示数据」变成不可用功能（比如湖正在被别的
    进程写、或年文件坏了一个），而真正的保护已经在 write_daily 上。
    """
    from lquant.data.ingest.demo import _refuse_if_lake_has_real_data
    from lquant.data.store import parquet as parquet_mod

    def _boom(*a, **kw):
        raise OSError("lake unreadable")

    monkeypatch.setattr(parquet_mod, "read_daily", _boom)
    _refuse_if_lake_has_real_data()          # 不抛即通过


def test_refuse_guard_allows_pure_demo_lake(lake_env):
    """湖里只有演示数据 → 允许重跑（幂等刷新演示环境是正常用法）。"""
    from lquant.data.ingest.demo import _refuse_if_lake_has_real_data
    from lquant.data.store.parquet import write_daily

    write_daily(_bars(["000001.SZ"], [D1], "demo", 10.0))
    _refuse_if_lake_has_real_data()          # 不抛即通过


def test_generate_demo_refuses_on_lake_with_real_data(lake_env):
    """generate_demo 在写日历/标的/ETF 元数据之前就拒绝（不给 duckdb 留演示残留）。"""
    from lquant.core.errors import DataQualityError
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.parquet import write_daily

    write_daily(_bars(["000001.SZ"], [D1], "baostock", 10.0))
    with pytest.raises(DataQualityError, match="拒绝写入演示数据"):
        generate_demo(start="2024-01-01", end="2024-12-31")

    # 演示行一行都没落湖
    from lquant.data.store.parquet import read_daily
    lake = read_daily().collect()
    assert lake["symbol"].n_unique() == 1
    assert set(lake["source"].to_list()) == {"baostock"}


def test_generate_demo_still_works_on_empty_lake(lake_env):
    """空湖照常生成演示数据（别把功能改没了）。"""
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.parquet import read_daily

    out = generate_demo(start="2024-01-01", end="2024-03-31")
    assert out["daily_rows"] > 0
    lake = read_daily().collect()
    assert len(lake) > 0
    assert set(lake["source"].to_list()) == {"demo"}
    assert lake["symbol"].n_unique() == 30      # 20 股 + 10 ETF


def test_generate_demo_is_self_consistent_for_downstream(lake_env):
    """演示环境必须自带下游要用的**全部**输入。

    缺 `daily_basic` / `financial_pit` / `index_daily` 的表现不是报错，而是
    个股与行业分析的估值 / 景气度 / 相对强度角度**安静地**变成
    ``available=false`` —— 从页面上分不出「演示环境没有这个数据」还是
    「代码坏了」。这是 float_mv / industry_classify 当初被补进来的同一个理由。
    """
    from lquant.core.db import reader
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.parquet import read_daily_basic

    out = generate_demo(start="2024-01-01", end="2024-03-31")
    assert out["daily_basic_rows"] > 0
    assert out["financial_rows"] > 0
    assert out["index_rows"] > 0

    basic = read_daily_basic()
    assert basic.height > 0
    # 估值列必须像真实 provider 一样可正可算，而不是全 NULL
    assert basic["pe_ttm"].drop_nulls().len() > 0
    assert basic["pb_mrq"].drop_nulls().len() > 0

    with reader() as con:
        items = {r[0] for r in con.execute(
            "SELECT DISTINCT item FROM financial_pit").fetchall()}
        assert "indicator.roe" in items
        assert "indicator.netprofit_yoy" in items
        # PIT 语义：每条财务都有公告日，否则前视门禁形同虚设
        assert con.execute(
            "SELECT count(*) FROM financial_pit WHERE pub_date IS NULL"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT count(*) FROM index_daily WHERE symbol = '000300.SH'"
        ).fetchone()[0] > 0
