"""定时同步 + 新采集器测试。

调度逻辑用注入时间测（不 sleep）；collect 用 demo 模式（离线）；
复权因子刷新用假 provider 验证湖内合并。
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

LQ_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("LQ_SYNC_WORKER", "0")  # 本模块直接调 manager，不要后台线程

pytestmark = pytest.mark.usefixtures("sync_env")


def _ensure_cwd():
    """cwd 指向的目录被删（pytest tmp 清理）时，os.getcwd() 会炸 —— 先兜底恢复。"""
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir(os.path.expanduser("~"))


@pytest.fixture(scope="module")
def sync_env(tmp_path_factory):
    """每个模块一份隔离数据环境（拷真实 duckdb + parquet 湖）。"""
    _ensure_cwd()
    if not (LQ_ROOT / "data" / "duckdb" / "lquant.duckdb").exists():
        pytest.skip(
            "需要本地 data/duckdb/lquant.duckdb（不入库，CI 上跳过）", allow_module_level=True
        )
    base = tmp_path_factory.mktemp("sync")
    old_cwd = os.getcwd()
    os.chdir(base)
    (base / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        LQ_ROOT / "data" / "duckdb" / "lquant.duckdb", base / "data" / "duckdb" / "lquant.duckdb"
    )
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
    # 恢复 cwd —— 泄漏的 cwd 会被 pytest tmp 保留策略回收掉，
    # 之后所有模块 os.getcwd() 直接 FileNotFoundError（M9 教训同款）。
    os.chdir(old_cwd)
    get_settings.cache_clear()


# ---------- 默认作业与 CRUD ----------


def test_seed_defaults_and_list():
    from lquant.sync import manager

    # 拷贝来的库可能已被线上服务 seed 过 —— 断言幂等与关键作业存在，不assert首次数量
    manager.seed_defaults()
    assert manager.seed_defaults() == 0  # 幂等
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert {"close", "evening", "daily", "adj"} <= set(jobs)
    assert jobs["close"]["kind"] == "collect"
    assert jobs["daily"]["params"].get("days")


def test_seed_defaults_includes_news_job():
    """资讯必须有定时作业：此前只有 HTTP API 能触发，靠人点 → 随时静默停更
    （实测 news_item 最新一条停在 2026-09-16，当日 0 条）。"""
    from lquant.sync import manager

    manager.seed_defaults()
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert "news" in jobs, "默认作业里必须有 news，否则资讯同步没有自动入口"
    assert jobs["news"]["kind"] == "news"
    assert jobs["news"]["enabled"] is True
    # 日内多档：单档会在快讯流上留下整段空窗
    assert len(jobs["news"]["schedule_time"].split(",")) >= 2


def test_job_crud():
    from lquant.sync import manager

    manager.upsert_job(
        "test_job",
        "测试作业",
        "collect",
        "16:00",
        weekdays="5",
        params={"schedule": "close", "demo": True},
    )
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
    base = {
        "sync_id": "t",
        "name": "t",
        "kind": "collect",
        "schedule_time": "15:05",
        "weekdays": "1,2,3,4,5",
        "params": {},
        "enabled": True,
        "last_run_at": None,
    }
    base.update(kw)
    return base


def test_due_logic():
    from lquant.sync.manager import _is_due

    # 时间未到
    assert not _is_due(_job(), datetime(2026, 9, 8, 10, 0))
    # 时间已到且未跑过 → 到期
    assert _is_due(_job(), datetime(2026, 9, 8, 15, 6))
    # 今天已跑过 → 不重复
    assert not _is_due(_job(last_run_at=datetime(2026, 9, 8, 15, 30)), datetime(2026, 9, 8, 18, 0))
    # 昨天跑过 → 今天到期（补跑）
    assert _is_due(_job(last_run_at=datetime(2026, 9, 7, 18, 0)), datetime(2026, 9, 8, 15, 6))
    # 周末不在 weekdays 内
    assert not _is_due(_job(), datetime(2026, 9, 12, 16, 0))  # 周六
    # 周末作业周六生效
    assert _is_due(_job(weekdays="6"), datetime(2026, 9, 12, 16, 0))
    # 禁用 / 时刻精确命中
    assert not _is_due(_job(enabled=False), datetime(2026, 9, 8, 16, 0))
    assert _is_due(_job(), datetime(2026, 9, 8, 15, 5))


def test_due_catchup_after_multi_day_downtime():
    """宕机跨天补跑：last_run 落后于今天的应触发时刻即到期（不再永久跳过）。"""
    from lquant.sync.manager import _is_due

    # 宕机 3 天后恢复，now 已过当天调度时刻 → 必须补跑
    assert _is_due(
        _job(last_run_at=datetime(2026, 9, 4, 15, 10)), datetime(2026, 9, 8, 15, 6))
    # 当天早些时候手动跑过（在调度时刻之前）不阻塞当晚调度
    assert _is_due(
        _job(last_run_at=datetime(2026, 9, 8, 9, 0)), datetime(2026, 9, 8, 15, 6))
    # 调度时刻之后跑过 → 不重复
    assert not _is_due(
        _job(last_run_at=datetime(2026, 9, 8, 15, 10)), datetime(2026, 9, 8, 18, 0))
    # 时间未到 → 不跑
    assert not _is_due(
        _job(last_run_at=datetime(2026, 9, 4, 15, 10)), datetime(2026, 9, 8, 10, 0))


def test_upsert_accepts_new_kinds():
    """reference/daily_basic/financial/news 四类新作业类型可注册（默认禁用防触网）。"""
    from lquant.sync import manager

    manager.seed_defaults()
    for kind in ("reference", "daily_basic", "financial", "news"):
        manager.upsert_job(f"t-{kind}", "t", kind, "07:00", params={"days": 14},
                           enabled=False)
    ids = {j["sync_id"] for j in manager.list_jobs()}
    assert {"t-reference", "t-daily_basic", "t-financial", "t-news"} <= ids


def test_upsert_accepts_multi_slot_and_rejects_bad_slot():
    """多档 schedule_time：合法档去重后落库；混进非法档必须报错。

    非法档被静默丢弃的话，作业会在那一档永不触发 —— 必须挡住。
    """
    from lquant.sync import manager

    manager.upsert_job("multi", "多档", "news", "09:05, 15:20 ,09:05", enabled=False)
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert jobs["multi"]["schedule_time"] == "09:05,15:20"   # 去空白 + 去重

    with pytest.raises(ValueError):
        manager.upsert_job("bad-multi", "坏档", "news", "09:05,25:00")
    with pytest.raises(ValueError):
        manager.upsert_job("bad-multi2", "坏档", "news", "")


def test_due_logic_multi_slot():
    """日内多档：每个档位各触发一次；跑过当前档位后等到下一档再触发。"""
    from lquant.sync.manager import _due_slot, _is_due, _parse_schedules

    assert _parse_schedules("09:05,15:20,20:00") == ["09:05", "15:20", "20:00"]
    assert _parse_schedules("09:05,25:00,,") == ["09:05"]

    job = _job(schedule_time="09:05,15:20,20:00")
    # 第一档之前 / 第一档到点
    assert not _is_due(job, datetime(2026, 9, 8, 9, 4))
    assert _is_due(job, datetime(2026, 9, 8, 9, 5))
    # 第一档跑过 → 第二档之前不重复
    assert not _is_due(_job(schedule_time="09:05,15:20,20:00",
                            last_run_at=datetime(2026, 9, 8, 9, 6)),
                       datetime(2026, 9, 8, 12, 0))
    # 到第二档 → 再触发
    assert _is_due(_job(schedule_time="09:05,15:20,20:00",
                        last_run_at=datetime(2026, 9, 8, 9, 6)),
                   datetime(2026, 9, 8, 15, 20))
    # 应触发时刻取「已到点里最近的一档」
    assert _due_slot(job, datetime(2026, 9, 8, 16, 0)) == datetime(2026, 9, 8, 15, 20)
    assert _due_slot(job, datetime(2026, 9, 8, 8, 0)) is None


def test_trading_day_filter():
    """日历覆盖的日期按 is_open 过滤；日历缺失该日时回退放行（不静默全跳）。"""
    from datetime import date as _date

    from lquant.sync.manager import _trading_day_ok

    assert _trading_day_ok(_date(2026, 9, 8)) is True   # 日历缺失该日 → 放行
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        con.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?, true, 't')",
                    [_date(2026, 9, 8)])
        con.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?, false, 't')",
                    [_date(2026, 9, 9)])
    assert _trading_day_ok(_date(2026, 9, 8)) is True
    assert _trading_day_ok(_date(2026, 9, 9)) is False  # 假日不跑数据作业


# ---------- 作业执行（demo 离线） ----------


def test_run_collect_demo_persists_and_logs():
    from lquant.sync import manager

    manager.seed_defaults()
    job = {
        "sync_id": "close",
        "name": "收盘采集",
        "kind": "collect",
        "params": {"schedule": "close", "demo": True},
    }
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
    from lquant.core.db import reader
    from lquant.data.store.catalog import upsert
    from lquant.market.collectors import run
    from lquant.market.schema import ensure_market_tables

    df = run("index_daily", demo=True)
    assert {"000001.SH", "000300.SH"} <= set(df["symbol"].unique().to_list())
    assert df["close"].to_list() and all(v > 0 for v in df["close"].to_list())
    # 落库 → 读回。sync_env 拷贝的是真实库，index_daily 可能已有历史数据
    # （如真实指数采集结果），先清空再用例自持 —— 断言的是「本次写入行数」，
    # 不能依赖拷贝库为空的假设。
    with __import__("lquant.core.db", fromlist=["writer"]).writer() as con:
        ensure_market_tables(con)
        con.execute("DELETE FROM index_daily")
    cols = [
        c
        for c in [
            "trade_date",
            "symbol",
            "name",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
            "collected_at",
        ]
        if c in df.columns
    ]
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

    # 空湖 read_daily 现在返回「有 schema 的空帧」，.select 安全；仍先判空跳过，
    # 因为本用例要对真实湖行做复权因子回写。
    lake = read_daily(start="2026-06-01").collect()
    if not len(lake):
        pytest.skip("湖为空")
    keys = lake.select(["symbol", "trade_date"]).head(20)

    class FakeProvider:
        def adj_factors(self, symbols, start, end):
            return keys.select(
                pl.col("symbol"),
                pl.col("trade_date"),
                pl.lit(1.234).alias("factor"),
                pl.lit("fake").alias("source"),
            )

    n = refresh_adj_factors(days=400, provider=FakeProvider())
    assert n == len(keys)
    # 湖里对应行的 adj_factor 已更新为 1.234
    after = (
        read_daily(start="2026-06-01").collect().join(keys, on=["symbol", "trade_date"], how="semi")
    )
    assert set(after["adj_factor"].unique().to_list()) == {1.234}


# ---------- tick 集成 ----------


def test_tick_runs_due_jobs_only():
    from datetime import datetime as _dt

    from lquant.sync import manager

    manager.seed_defaults()
    # 其它作业（backfill/reference 等会触网）一律禁用：测试必须离线
    for sid in ("preopen", "backfill", "reference", "daily_basic", "financial"):
        manager.set_enabled(sid, False)
    # 把 close 作业的时刻改到很早 → 必然到期；daily/adj 改到很晚 → 不到期。
    # weekdays 不能沿用 seed_defaults 的「1,2,3,4,5」/「6」：那是生产节奏，
    # 用例必须与运行日无关（close 显式全周——注意 _is_due 对空串回落
    # 「1,2,3,4,5」，不能传空；adj 选一个非今天的工作日），否则周末跑必挂。
    today = _dt.now().isoweekday()
    other_day = 6 if today != 6 else 7
    manager.upsert_job(
        "close",
        "收盘采集",
        "collect",
        "00:01",
        weekdays="1,2,3,4,5,6,7",
        params={"schedule": "close", "demo": True},
    )
    manager.upsert_job(
        "daily", "日线增量", "daily", "23:59", weekdays="1,2,3,4,5,6,7", params={"days": 3}
    )
    manager.upsert_job(
        "adj", "复权因子", "adj_factor", "23:59", weekdays=str(other_day), params={"days": 60}
    )
    # 用注入时钟取下一个交易日（本周六日历为非交易日，真实时钟会让 tick 全跳）
    res = manager.tick(now=_dt(2026, 9, 21, 9, 0))
    ids = {r["sync_id"] for r in res}
    assert "close" in ids
    assert "daily" not in ids and "adj" not in ids
    # 再 tick 一次：close 今天已跑 → 不再执行
    res2 = manager.tick()
    assert all(r["sync_id"] != "close" for r in res2)


# ---------------------------------------------------------------- news 作业


def test_run_job_news_creates_and_executes_task(monkeypatch):
    """news 作业：建 news_task 并执行，rows/status 透传到 sync_run 记录。"""
    from lquant.core.types import today_cn
    from lquant.news import tasks as news_tasks
    from lquant.sync import manager

    calls: dict = {}
    monkeypatch.setattr(news_tasks, "init_news_task_ddl", lambda con: None)

    def _create(con, kind, params):
        calls["create"] = (kind, dict(params))
        return {"task_id": "news_test_1"}

    def _execute(con, task_id):
        calls["execute"] = task_id
        return {"task_id": task_id, "status": "ok", "rows_written": 7,
                "sources_status": {"eastmoney": {"status": "ok", "rows": 7}}}

    monkeypatch.setattr(news_tasks, "create_task", _create)
    monkeypatch.setattr(news_tasks, "execute_task", _execute)

    res = manager.run_job({"sync_id": "news", "name": "资讯", "kind": "news",
                           "params": {"sources": ["eastmoney"]}})
    assert res["status"] == "ok"
    assert res["rows"] == 7
    assert res["detail"]["task_id"] == "news_test_1"
    assert calls["create"][0] == "daily"
    # 业务日期走 CN 时钟：宿主时区非 Asia/Shanghai 时不能错位一天
    assert calls["create"][1]["date"] == today_cn().isoformat()
    assert calls["create"][1]["sources"] == ["eastmoney"]
    assert calls["execute"] == "news_test_1"


@pytest.mark.parametrize(("task_status", "expected"), [("partial", "partial"),
                                                       ("failed", "failed")])
def test_run_job_news_propagates_task_status(monkeypatch, task_status, expected):
    """单源失败/全挂 → 作业状态跟着降级，不被吞成 ok。"""
    from lquant.news import tasks as news_tasks
    from lquant.sync import manager

    monkeypatch.setattr(news_tasks, "init_news_task_ddl", lambda con: None)
    monkeypatch.setattr(news_tasks, "create_task",
                        lambda con, kind, params: {"task_id": "news_test_2"})
    monkeypatch.setattr(
        news_tasks, "execute_task",
        lambda con, task_id: {"task_id": task_id, "status": task_status,
                              "rows_written": 0, "sources_status": {}},
    )

    res = manager.run_job({"sync_id": "news", "name": "资讯", "kind": "news", "params": {}})
    assert res["status"] == expected
    # news 不在 zero_rows 降级名单里：同源重复采集 0 新增属正常语义
    assert res["detail"].get("zero_rows") is None


def test_run_job_news_conflict_records_skipped(monkeypatch):
    """撞上运行中的手工资讯任务 → skipped（排队语义，不当故障告警）。"""
    from lquant.news import tasks as news_tasks
    from lquant.sync import manager

    monkeypatch.setattr(news_tasks, "init_news_task_ddl", lambda con: None)

    def _boom(con, kind, params):
        raise news_tasks.TaskConflictError("another news task is pending/running: news_x")

    monkeypatch.setattr(news_tasks, "create_task", _boom)

    res = manager.run_job({"sync_id": "news", "name": "资讯", "kind": "news", "params": {}})
    assert res["status"] == "skipped"
    assert res["detail"]["skipped"] is True
    assert "TaskConflictError" in res["detail"]["reason"]


# ---------------------------------------------------------------- 日志接线

def test_run_job_unknown_kind_failed_and_logged(sync_env, tmp_path):
    """未知作业类型 → status failed，且日志带作业 kind。"""
    from loguru import logger

    from lquant.core.logging import setup_logging
    from lquant.sync import manager

    setup_logging(level="DEBUG", log_dir=str(tmp_path))
    records: list = []
    hid = logger.add(records.append, level="DEBUG")
    try:
        res = manager.run_job(
            {"sync_id": "unknown-kind-probe", "name": "探针", "kind": "nope", "params": {}})
        assert res["status"] == "failed"
        assert "未知作业类型" in res["detail"]["error"]
        assert any("nope" in str(m) for m in records)
    finally:
        logger.remove(hid)
        logger.remove()  # 收掉 setup_logging 挂的文件/控制台 sink


def test_emit_sync_error_survives_ring_failure(sync_env, monkeypatch):
    """monitor 错误环挂掉 → 告警留痕（warning 日志）但不抛出。"""
    from loguru import logger

    from lquant.sync import manager

    class _BrokenRing:
        def append(self, *_a, **_kw):
            raise RuntimeError("ring down")

    monkeypatch.setattr("lquant.monitor.ring.error_ring", _BrokenRing())
    records: list = []
    hid = logger.add(_sink := records.append, level="DEBUG")
    try:
        manager._emit_sync_error({"sync_id": "probe"}, "collect", " failed", {"error": "boom"})
        assert any("错误环写入失败" in str(m) for m in records)
    finally:
        logger.remove(hid)


# ---------------------------------------------------------------- 失败自动重试

def test_retry_scheduling_on_repeated_failure():
    """failed 终态按退避排重试：attempt 递增、退避 5→15 分钟、耗尽清零。"""
    from lquant.core.db import reader
    from lquant.sync import manager

    manager.upsert_job("rt", "重试探针", "collect", "03:00", enabled=False)
    job = next(j for j in manager.list_jobs() if j["sync_id"] == "rt")

    res1 = manager.run_job({**job, "kind": "nope", "params": {}})
    assert res1["status"] == "failed"
    assert res1["attempt"] == 1
    assert res1["detail"]["attempt"] == 1
    assert res1["detail"]["next_retry_at"]
    with reader() as con:
        rc, nra = con.execute(
            "SELECT retry_count, next_retry_at FROM sync_job WHERE sync_id = 'rt'"
        ).fetchone()
    assert rc == 1 and nra is not None
    first_retry = datetime.fromisoformat(res1["detail"]["next_retry_at"])

    res2 = manager.run_job({**job, "kind": "nope", "retry_count": 1})
    assert res2["attempt"] == 2
    with reader() as con:
        rc2, _ = con.execute(
            "SELECT retry_count, next_retry_at FROM sync_job WHERE sync_id = 'rt'"
        ).fetchone()
    assert rc2 == 2
    second_retry = datetime.fromisoformat(res2["detail"]["next_retry_at"])
    # 退避 5 → 15 分钟：两次排程间隔必须远大于 9 分钟
    assert (second_retry - first_retry).total_seconds() > 9 * 60

    res3 = manager.run_job({**job, "kind": "nope", "retry_count": 2})
    assert res3["attempt"] == 3
    with reader() as con:
        rc3, nra3 = con.execute(
            "SELECT retry_count, next_retry_at FROM sync_job WHERE sync_id = 'rt'"
        ).fetchone()
    assert (rc3 in (0, None)) and nra3 is None  # 耗尽清零，等下一档调度


def test_retry_due_triggers_even_off_schedule():
    """重试到期忽略 weekdays：非档位日、时刻一到（>=）即触发。"""
    from lquant.sync.manager import _is_due

    base = _job(weekdays="3",
                next_retry_at=datetime(2026, 9, 8, 15, 20),
                last_run_at=datetime(2026, 9, 8, 15, 6))
    # 2026-9-8 是周二（weekdays=3 不含），但重试时刻已到 → 必须触发
    assert _is_due(base, datetime(2026, 9, 8, 15, 21)) is True
    # now >= retry_dt 才触发：15:19 未到、15:20 恰好到
    assert _is_due(base, datetime(2026, 9, 8, 15, 19)) is False
    assert _is_due(base, datetime(2026, 9, 8, 15, 20)) is True


def test_successful_run_resets_retry_state():
    """成功一次后重试态清零（NULL → list_jobs 读出 0）。"""
    from lquant.core.db import reader
    from lquant.sync import manager

    manager.upsert_job("rt2", "重试清零探针", "collect", "03:30", enabled=False)
    job = next(j for j in manager.list_jobs() if j["sync_id"] == "rt2")
    res_fail = manager.run_job({**job, "kind": "nope", "params": {}})
    assert res_fail["status"] == "failed"

    job2 = next(j for j in manager.list_jobs() if j["sync_id"] == "rt2")
    res_ok = manager.run_job({**job2, "params": {"schedule": "close", "demo": True}})
    assert res_ok["status"] in ("ok", "partial")
    assert {j["sync_id"]: j for j in manager.list_jobs()}["rt2"]["retry_count"] == 0
    with reader() as con:
        nra = con.execute(
            "SELECT next_retry_at FROM sync_job WHERE sync_id = 'rt2'").fetchone()[0]
    assert nra is None


def test_partial_from_check_is_retryable(monkeypatch):
    """完备性检查不过 → partial 同样排重试。"""
    import lquant.data.ingest.checkpoint as cp_mod
    from lquant.core.db import reader
    from lquant.data.ingest import daily_basic as db_mod
    from lquant.sync import manager

    class FakeCp:
        def __init__(self, *a, **kw):
            self.done = {"x": 1}

        def covered_window(self, *a, **kw):
            return None

    monkeypatch.setattr(cp_mod, "Checkpoint", FakeCp)
    monkeypatch.setattr(db_mod, "backfill_daily_basic",
                        lambda *a, **kw: {"rows": 0})

    manager.upsert_job("rt5", "检查重试探针", "daily_basic", "03:45", enabled=False)
    job = next(j for j in manager.list_jobs() if j["sync_id"] == "rt5")
    res = manager.run_job({**job, "params": {"days": 1, "merge": False}})
    assert res["status"] == "partial"
    with reader() as con:
        rc, nra = con.execute(
            "SELECT retry_count, next_retry_at FROM sync_job WHERE sync_id = 'rt5'"
        ).fetchone()
    assert rc == 1 and nra is not None
