"""定时同步：后台常驻调度（用户要求的「定时同步数据」）。

三种作业类型：
- collect    : 跑市场采集器组（params.schedule = close/evening/preopen，缺省全跑）
- daily      : 日线增量回填（params.days = 回看天数，只补哨兵池）
- adj_factor : 复权因子增量刷新（params.days = 回看天数）
- backfill   : 大盘看板表缺失检查与补齐（params.days = 回看天数，见 market.backfill）

调度语义（刻意简单，不上 cron）：
- schedule_time「HH:MM」，weekdays「1,2,3,4,5」（ISO，周一=1；空 = 每天）
- 时刻到了且今天没跑过 → 执行；服务重启后 last_run 落在今天之前 → 补跑
- 节假日按周几近似（采集器失败/空结果会记 sync_run，不炸进程）；
  精确交易日历过滤待接 trade_calendar（预留）
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta

from lquant.core.db import reader, writer

__all__ = ["DEFAULT_JOBS", "seed_defaults", "list_jobs", "upsert_job", "delete_job",
           "set_enabled", "run_job", "tick", "loop_forever"]

_DDL = """
CREATE TABLE IF NOT EXISTS sync_job (
    sync_id      VARCHAR PRIMARY KEY,
    name         VARCHAR,
    kind         VARCHAR,
    schedule_time VARCHAR,
    weekdays     VARCHAR,
    params       JSON,
    enabled      BOOLEAN DEFAULT TRUE,
    last_run_at  TIMESTAMP,
    last_status  VARCHAR,
    last_rows    INTEGER,
    created_at   TIMESTAMP,
    updated_at   TIMESTAMP
)
"""

DEFAULT_JOBS: list[dict] = [
    {"sync_id": "close", "name": "收盘采集（涨停/资金流/板块/情绪/指数）",
     "kind": "collect", "schedule_time": "15:05", "weekdays": "1,2,3,4,5",
     "params": {"schedule": "close"}},
    {"sync_id": "evening", "name": "盘后采集（龙虎榜/北向）",
     "kind": "collect", "schedule_time": "18:00", "weekdays": "1,2,3,4,5",
     "params": {"schedule": "evening"}},
    {"sync_id": "preopen", "name": "盘前采集（校验与补采）",
     "kind": "collect", "schedule_time": "09:00", "weekdays": "1,2,3,4,5",
     "params": {"schedule": "preopen"}, "enabled": False},
    {"sync_id": "backfill", "name": "盘前缺口补齐（指数日线缺失检查）",
     "kind": "backfill", "schedule_time": "09:10", "weekdays": "1,2,3,4,5",
     "params": {"days": 90}},
    {"sync_id": "daily", "name": "日线增量同步（全市场）",
     "kind": "daily", "schedule_time": "18:30", "weekdays": "1,2,3,4,5",
     "params": {"days": 10, "market": "all"}},
    {"sync_id": "adj", "name": "复权因子刷新",
     "kind": "adj_factor", "schedule_time": "08:00", "weekdays": "6",
     "params": {"days": 120}},
    {"sync_id": "reference", "name": "标的清单/交易日历同步",
     "kind": "reference", "schedule_time": "07:30", "weekdays": "1,2,3,4,5",
     "params": {"skip_details": False}},
    {"sync_id": "daily_basic", "name": "估值指标同步（daily_basic）",
     "kind": "daily_basic", "schedule_time": "18:45", "weekdays": "1,2,3,4,5",
     "params": {"days": 14, "merge": True}},
    {"sync_id": "financial", "name": "PIT 财务数据同步",
     "kind": "financial", "schedule_time": "19:15", "weekdays": "1,2,3,4,5",
     "params": {"days": 90}},
]


# ---------------------------------------------------------------- CRUD

def _ensure_tables(con) -> None:
    con.execute(_DDL)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sync_run (
            run_id     VARCHAR PRIMARY KEY,
            sync_id    VARCHAR,
            job_name   VARCHAR,
            kind       VARCHAR,
            started_at TIMESTAMP, finished_at TIMESTAMP,
            rows INTEGER, status VARCHAR, detail JSON
        )
    """)


def seed_defaults() -> int:
    """把默认作业写入 sync_job（已存在同名 sync_id 的跳过）。幂等。"""
    n = 0
    with writer() as con:
        _ensure_tables(con)
        have = {r[0] for r in con.execute("SELECT sync_id FROM sync_job").fetchall()}
        now = datetime.now()
        for j in DEFAULT_JOBS:
            if j["sync_id"] in have:
                continue
            con.execute(
                "INSERT INTO sync_job VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                [j["sync_id"], j["name"], j["kind"], j["schedule_time"],
                 j.get("weekdays", "1,2,3,4,5"), json.dumps(j.get("params", {})),
                 j.get("enabled", True), now, now])
            n += 1
    return n


def list_jobs() -> list[dict]:
    with reader() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT sync_id, name, kind, schedule_time, weekdays, params, enabled, "
            "last_run_at, last_status, last_rows, created_at, updated_at "
            "FROM sync_job ORDER BY schedule_time").fetchall()
    return [{"sync_id": r[0], "name": r[1], "kind": r[2], "schedule_time": r[3],
             "weekdays": r[4], "params": json.loads(r[5]) if r[5] else {},
             "enabled": r[6], "last_run_at": str(r[7]) if r[7] else None,
             "last_status": r[8], "last_rows": r[9],
             "created_at": str(r[10]), "updated_at": str(r[11])} for r in rows]


def upsert_job(sync_id: str, name: str, kind: str, schedule_time: str,
               weekdays: str = "1,2,3,4,5", params: dict | None = None,
               enabled: bool = True) -> dict:
    """新建/更新作业（按 sync_id 覆盖；时间格式 HH:MM 校验）。"""
    datetime.strptime(schedule_time, "%H:%M")
    if kind not in ("collect", "daily", "adj_factor", "backfill",
                    "reference", "daily_basic", "financial"):
        raise ValueError(f"未知作业类型: {kind}")
    if kind == "daily" and (params or {}).get("market") not in (
            None, "all", "sentinel"):
        raise ValueError(
            f"daily 作业 params.market 只接受 all/sentinel，收到: "
            f"{params['market']!r}")
    now = datetime.now()
    with writer() as con:
        _ensure_tables(con)
        con.execute("DELETE FROM sync_job WHERE sync_id = ?", [sync_id])
        con.execute(
            "INSERT INTO sync_job VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            [sync_id, name, kind, schedule_time, weekdays,
             json.dumps(params or {}), enabled, now, now])
    return {"sync_id": sync_id}


def delete_job(sync_id: str) -> None:
    with writer() as con:
        _ensure_tables(con)
        con.execute("DELETE FROM sync_job WHERE sync_id = ?", [sync_id])


def set_enabled(sync_id: str, enabled: bool) -> None:
    with writer() as con:
        _ensure_tables(con)
        con.execute("UPDATE sync_job SET enabled = ?, updated_at = ? WHERE sync_id = ?",
                    [enabled, datetime.now(), sync_id])


# ---------------------------------------------------------------- 执行

def run_job(job: dict, *, demo: bool | None = None) -> dict:
    """执行一个作业并记录 sync_run / 更新 sync_job 状态。失败不抛出。"""
    started = datetime.now()
    params = job.get("params") or {}
    kind = job["kind"]
    rows, detail, status = 0, {}, "ok"
    try:
        if kind == "collect":
            from lquant.market.scheduler import collect_and_save

            d = params.get("demo", False) if demo is None else demo
            res = collect_and_save(schedule=params.get("schedule"),
                                   trade_date=started.date(), demo=d)
            rows = sum(res.get("persisted", {}).values())
            detail = {"collected": res.get("collected", {}), "errors": res.get("errors", {})}
            if res.get("errors"):
                status = "partial"
        elif kind == "daily":
            market = params.get("market", "all")
            if market not in (None, "all", "sentinel"):
                raise ValueError(
                    f"daily 作业 params.market 只接受 all/sentinel，收到: {market!r}")
            if market == "sentinel":
                # 兼容旧路径：哨兵池增量（不建 data_task）
                from lquant.data.ingest.daily import backfill_daily

                rows = backfill_daily(
                    full=False,
                    start=(started.date()
                           - timedelta(days=int(params.get("days", 10)))).isoformat())
            else:
                # 全市场增量：建 data_task（历史可查）后走执行器
                from lquant.data.ingest import tasks as data_tasks

                # auto_crosscheck 透传 job params（缺省 True），运维可在 sync_job 里配置关闭
                t = data_tasks.create_task(
                    "daily_update", {"days": int(params.get("days", 10)),
                                     "auto_crosscheck": params.get("auto_crosscheck", True)})
                task = data_tasks.execute_task(t["task_id"])
                rows = int(task.get("rows_written") or 0)
                detail = {"task_id": t["task_id"], "task_status": task["status"],
                          "message": task.get("message")}
                if task["status"] == "partial":
                    status = "partial"
                elif task["status"] == "failed":
                    status = "failed"
        elif kind == "adj_factor":
            from lquant.data.ingest.adj import refresh_adj_factors

            rows = refresh_adj_factors(days=int(params.get("days", 120)))
        elif kind == "reference":
            from lquant.data.ingest.reference import sync_reference

            res = sync_reference(
                skip_details=bool(params.get("skip_details", False)))
            detail = res if isinstance(res, dict) else {"result": str(res)}
            rows = int(sum(v for v in detail.values() if isinstance(v, int))) \
                if isinstance(res, dict) else 0
        elif kind == "daily_basic":
            from lquant.data.ingest.daily_basic import backfill_daily_basic

            res = backfill_daily_basic(
                start=(started.date()
                       - timedelta(days=int(params.get("days", 14)))).isoformat(),
                merge=bool(params.get("merge", True)))
            rows = int(res.get("rows", 0))
            detail = res if isinstance(res, dict) else {}
        elif kind == "financial":
            from lquant.data.ingest.financial import backfill_financial
            from lquant.data.store.catalog import SecurityRepo

            syms = SecurityRepo().stock_symbols(include_delisted=False)
            rows = backfill_financial(
                symbols=syms,
                start=(started.date()
                       - timedelta(days=int(params.get("days", 90)))).isoformat())
            detail = {"symbols": len(syms)}
        elif kind == "backfill":
            from lquant.market.backfill import ensure_market_coverage

            res = ensure_market_coverage(days=int(params.get("days", 90)))
            # 日线湖覆盖度对账：日历×标的×湖内日期差集，缺口落 issue 并
            # 自动建 daily_update 补齐任务（此前只有 index_daily 有对账）。
            from lquant.data.quality.coverage import scan_coverage

            cov = scan_coverage(days=int(params.get("coverage_days", 30)),
                                repair=True)
            res["coverage"] = cov
            rows = int(res.get("persisted") or 0)
            detail = res
            if cov.get("repair", {}).get("created"):
                status = "partial"
        else:
            status = "failed"
            detail = {"error": f"未知作业类型 {kind}"}
    except Exception as e:  # noqa: BLE001
        status = "failed"
        detail = {"error": f"{type(e).__name__}: {e}"}

    finished = datetime.now()
    # 告警：终态非 ok → 监控错误环（monitor 错误环 → error_logs 可查）
    if status != "ok":
        _emit_sync_error(job, kind, status, detail)
    # 0 行不算健康：日线/采集类作业空返回记 partial，避免静默缺口被掩盖
    if rows == 0 and kind in ("daily", "collect", "adj_factor") and status == "ok":
        status = "partial"
        detail = {**detail, "zero_rows": True}
    run_id = uuid.uuid4().hex[:12]
    try:
        with writer() as con:
            _ensure_tables(con)
            con.execute(
                "INSERT OR REPLACE INTO sync_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, job.get("sync_id"), job.get("name"), kind,
                 started, finished, rows, status, json.dumps(detail, default=str)])
            con.execute(
                "UPDATE sync_job SET last_run_at = ?, last_status = ?, last_rows = ?, "
                "updated_at = ? WHERE sync_id = ?",
                [finished, status, rows, finished, job.get("sync_id")])
    except Exception as e:  # noqa: BLE001  记录失败不影响主流程
        print(f"[warn] sync_run 记录失败: {e}")
    return {"run_id": run_id, "status": status, "rows": rows,
            "elapsed_sec": round((finished - started).total_seconds(), 1),
            "detail": detail}


def _emit_sync_error(job: dict, kind: str, status: str, detail: dict) -> None:
    """同步失败/部分失败 → monitor 错误环（error_logs 路由 sync://{sync_id}）。

    采集失败绝不影响主流程：ring 不可用时静默降级（stdout 已有日志）。
    """
    try:
        from time import time as _time

        from lquant.monitor.ring import error_ring
        from lquant.monitor.types import ApiErrorPoint

        err = (detail or {}).get("error") or (detail or {}).get("message")
        error_ring.append(ApiErrorPoint(
            ts=_time(), route=f"sync://{job.get('sync_id', kind)}",
            method="JOB", status=500 if status == "failed" else 207,
            error_type=f"SyncJob{status.capitalize()}",
            message=(str(err)[:500] if err else f"{kind} {status}"),
            traceback_tail=None))
    except Exception:  # noqa: BLE001 - 告警失败不影响主流程
        pass


def history(limit: int = 50) -> list[dict]:
    with reader() as con:
        _ensure_tables(con)
        rows = con.execute(
            f"SELECT run_id, sync_id, job_name, kind, started_at, finished_at, "
            f"rows, status, detail FROM sync_run ORDER BY started_at DESC LIMIT {limit}"
        ).fetchall()
    return [{"run_id": r[0], "sync_id": r[1], "job_name": r[2], "kind": r[3],
             "started_at": str(r[4]), "finished_at": str(r[5]), "rows": r[6],
             "status": r[7], "detail": json.loads(r[8]) if r[8] else {}} for r in rows]


# ---------------------------------------------------------------- 调度

def _is_due(job: dict, now: datetime) -> bool:
    if not job.get("enabled", False):
        return False
    weekdays = str(job.get("weekdays") or "1,2,3,4,5")
    if weekdays.strip() and str(now.isoweekday()) not in [w.strip() for w in weekdays.split(",")]:
        return False
    hhmm = now.strftime("%H:%M")
    if hhmm < job["schedule_time"]:
        return False
    # 跨天补跑：last_run 落后于「今天的应触发时刻」即到期。此前按
    # 「今天是否跑过」判断，宕机跨天后作业被永久跳过 —— 窗口滑过即缺口。
    try:
        h, m = job["schedule_time"].split(":")
        scheduled = now.replace(hour=int(h), minute=int(m),
                                second=0, microsecond=0)
    except ValueError:
        return False
    last = job.get("last_run_at")
    if last:
        last_dt = last if isinstance(last, datetime) else datetime.fromisoformat(str(last))
        if last_dt >= scheduled:               # 本次应触发时刻之后已跑过
            return False
    return True


def _trading_day_ok(d) -> bool:
    """日历覆盖该日时按 is_open 过滤；日历缺失该日时放行（回退周几近似）。

    日历为空/未同步不能让所有作业静默全跳 —— 那会制造新的「静默缺口」。
    """
    try:
        # is_trading_day 查不到该日返回 False —— 不能直接用，否则日历
        # 未同步会让所有作业静默全跳。先显式查该日是否在日历里。
        with reader() as con:
            con.execute("CREATE TABLE IF NOT EXISTS trade_calendar ("
                        "trade_date DATE PRIMARY KEY, is_open BOOLEAN, "
                        "source VARCHAR)")
            row = con.execute(
                "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
                [d]).fetchone()
        return bool(row[0]) if row else True
    except Exception:  # noqa: BLE001 - 日历不可用 → 放行
        return True


def tick(now: datetime | None = None) -> list[dict]:
    """跑一遍所有到期作业。返回执行结果列表。"""
    now = now or datetime.now()
    # 非交易日（日历覆盖判断）跳过数据作业：跑也不产数据还污染 sync_run。
    if not _trading_day_ok(now.date()):
        return []
    out = []
    for job in list_jobs():
        if not _is_due(job, now):
            continue
        res = run_job(job)
        out.append({"sync_id": job["sync_id"], **res})
    return out


def loop_forever(interval: int = 30) -> None:
    """常驻循环：每 interval 秒 tick 一次。放进 daemon 线程即可。"""
    while True:
        try:
            done = tick()
            for r in done:
                print(f"[sync] {r['sync_id']} → {r['status']} rows={r['rows']}")
        except Exception as e:  # noqa: BLE001  调度循环绝不退出
            print(f"[warn] sync tick 异常: {e}")
        time.sleep(interval)
