"""定时同步 + 新采集器测试。

调度逻辑用注入时间测（不 sleep）；collect 用 demo 模式（离线）；
复权因子刷新用假 provider 验证湖内合并。
"""
from __future__ import annotations

import os
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

LQ_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("LQ_SYNC_WORKER", "0")   # 本模块直接调 manager，不要后台线程

pytestmark = pytest.mark.usefixtures("sync_env")


@pytest.fixture(scope="module")
def sync_env(tmp_path_factory):
    """每个模块一份隔离数据环境（拷真实 duckdb + parquet 湖）。"""
    if not (LQ_ROOT / "data" / "duckdb" / "lquant.duckdb").exists():
        pytest.skip("需要本地 data/duckdb/lquant.duckdb（不入库，CI 上跳过）",
                    allow_module_level=True)
    base = tmp_path_factory.mktemp("sync")
    os.chdir(base)
    (base / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    shutil.copy2(LQ_ROOT / "data" / "duckdb" / "lquant.duckdb",
                 base / "data" / "duckdb" / "lquant.duckdb")
    # data/ 全 gitignore：全新 checkout 常见「duckdb 已被采集期创建、
    # parquet 湖仍缺」。parquet 缺失时给隔离环境建空目录，让 store 优雅返回
    # 空帧而非 shutil.copytree 抛 FileNotFoundError。
    src_parquet = LQ_ROOT / "data" / "parquet"
    dst_parquet = base / "data" / "parquet"
    if src_parquet.is_dir():
        shutil.copytree(src_parquet, dst_parquet, dirs_exist_ok=True)
    else:
        dst_parquet.mkdir(parents=True, exist_ok=True)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield base
    get_settings.cache_clear()


# ---------- 默认作业与 CRUD ----------

def test_seed_defaults_and_list():
    from lquant.sync import manager

    # 拷贝来的库可能已被线上服务 seed 过 —— 断言幂等与关键作业存在，不assert首次数量
    manager.seed_defaults()
    assert manager.seed_defaults() == 0                  # 幂等
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert {"close", "evening", "daily", "adj"} <= set(jobs)
    assert jobs["close"]["kind"] == "collect"
    assert jobs["daily"]["params"].get("days")


def test_job_crud():
    from lquant.sync import manager

    manager.upsert_job("test_job", "测试作业", "collect", "16:00",
                       weekdays="5", params={"schedule": "close", "demo": True})
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert jobs["test_job"]["schedule_time"] == "16:00"
    manager.set_enabled("test_job", False)
    assert not {j["sync_id"]: j for j in manager.list_jobs()}["test_job"]["enabled"]
    manager.delete_job("test_job")
    assert "test_job" not in {j["sync_id"] for j in manager.list_jobs()}


def test_upsert_validation():
    from lquant.sync import manager

    with pytest.raises(ValueError):
        manager.upsert_job("bad", "时间格式错", "collect", "25:00")
    with pytest.raises(ValueError):
        manager.upsert_job("bad", "类型错", "whatever", "10:00")


# ---------- 到期判定 ----------

def _job(**kw):
    base = {"sync_id": "t", "name": "t", "kind": "collect",
            "schedule_time": "15:05", "weekdays": "1,2,3,4,5",
            "params": {}, "enabled": True, "last_run_at": None}
    base.update(kw)
    return base


def test_due_logic():
    from lquant.sync.manager import _is_due

    # 时间未到
    assert not _is_due(_job(), datetime(2026, 9, 8, 10, 0))
    # 时间已到且未跑过 → 到期
    assert _is_due(_job(), datetime(2026, 9, 8, 15, 6))
    # 今天已跑过 → 不重复
    assert not _is_due(_job(last_run_at=datetime(2026, 9, 8, 15, 30)),
                       datetime(2026, 9, 8, 18, 0))
    # 昨天跑过 → 今天到期（补跑）
    assert _is_due(_job(last_run_at=datetime(2026, 9, 7, 18, 0)),
                   datetime(2026, 9, 8, 15, 6))
    # 周末不在 weekdays 内
    assert not _is_due(_job(), datetime(2026, 9, 12, 16, 0))    # 周六
    # 周末作业周六生效
    assert _is_due(_job(weekdays="6"), datetime(2026, 9, 12, 16, 0))
    # 禁用 / 时刻精确命中
    assert not _is_due(_job(enabled=False), datetime(2026, 9, 8, 16, 0))
    assert _is_due(_job(), datetime(2026, 9, 8, 15, 5))


# ---------- 作业执行（demo 离线） ----------

def test_run_collect_demo_persists_and_logs():
    from lquant.sync import manager

    manager.seed_defaults()
    job = {"sync_id": "close", "name": "收盘采集", "kind": "collect",
           "params": {"schedule": "close", "demo": True}}
    res = manager.run_job(job)
    assert res["status"] in ("ok", "partial")
    assert res["rows"] >= 0
    hist = manager.history(limit=5)
    assert any(h["sync_id"] == "close" for h in hist)
    # sync_job 状态被更新
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert jobs["close"]["last_run_at"] is not None
    assert jobs["close"]["last_status"] == res["status"]


def test_index_daily_demo_schema_and_persist():
    from lquant.market.collectors import run
    from lquant.market.schema import ensure_market_tables
    from lquant.data.store.catalog import upsert
    from lquant.core.db import reader

    df = run("index_daily", demo=True)
    assert {"000001.SH", "000300.SH"} <= set(df["symbol"].unique().to_list())
    assert df["close"].to_list() and all(v > 0 for v in df["close"].to_list())
    # 落库 → 读回
    with __import__("lquant.core.db", fromlist=["writer"]).writer() as con:
        ensure_market_tables(con)
    cols = [c for c in ["trade_date", "symbol", "name", "open", "high", "low",
                        "close", "pre_close", "volume", "amount", "collected_at"]
            if c in df.columns]
    upsert("index_daily", df.select(cols))
    with reader() as con:
        n = con.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert n == len(df)


def test_dragon_tiger_demo_schema():
    from lquant.market.collectors import run

    df = run("dragon_tiger", demo=True)
    assert len(df) > 0
    for c in ("trade_date", "symbol", "net_buy", "buy_amount", "reason"):
        assert c in df.columns


# ---------- 复权因子刷新（假 provider） ----------

def test_refresh_adj_factors_merges_into_lake():
    import polars as pl
    from lquant.data.ingest.adj import refresh_adj_factors
    from lquant.data.store.parquet import read_daily

    # 先 collect 再判空：空湖 read_daily 返回无 schema 的空帧，直接 .select
    # 会抛 ColumnNotFoundError（没有 "symbol" 列）。先取行数，0 行即跳过。
    lake = read_daily(start="2026-06-01").collect()
    if not len(lake):
        pytest.skip("湖为空")
    keys = lake.select(["symbol", "trade_date"]).head(20)

    class FakeProvider:
        def adj_factors(self, symbols, start, end):
            return keys.select(
                pl.col("symbol"), pl.col("trade_date"),
                pl.lit(1.234).alias("factor"), pl.lit("fake").alias("source"))

    n = refresh_adj_factors(days=400, provider=FakeProvider())
    assert n == len(keys)
    # 湖里对应行的 adj_factor 已更新为 1.234
    after = (read_daily(start="2026-06-01").collect()
             .join(keys, on=["symbol", "trade_date"], how="semi"))
    assert set(after["adj_factor"].unique().to_list()) == {1.234}


# ---------- tick 集成 ----------

def test_tick_runs_due_jobs_only():
    from lquant.sync import manager

    manager.seed_defaults()
    # 把 close 作业的时刻改到很早 → 必然到期；daily/adj 改到很晚 → 不到期
    manager.upsert_job("close", "收盘采集", "collect", "00:01",
                       params={"schedule": "close", "demo": True})
    manager.upsert_job("daily", "日线增量", "daily", "23:59", params={"days": 3})
    manager.upsert_job("adj", "复权因子", "adj_factor", "23:59", weekdays="6",
                       params={"days": 60})
    res = manager.tick()
    ids = {r["sync_id"] for r in res}
    assert "close" in ids
    assert "daily" not in ids and "adj" not in ids
    # 再 tick 一次：close 今天已跑 → 不再执行
    res2 = manager.tick()
    assert all(r["sync_id"] != "close" for r in res2)
