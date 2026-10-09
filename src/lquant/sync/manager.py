"""定时同步：后台常驻调度（用户要求的「定时同步数据」）。

作业类型：
- collect    : 跑市场采集器组（params.schedule = close/evening/preopen，缺省全跑）
- daily      : 日线增量回填（params.days = 回看天数，只补哨兵池）
- adj_factor : 复权因子增量刷新（params.days = 回看天数）
- backfill   : 大盘看板表缺失检查与补齐（params.days = 回看天数，见 market.backfill）
- news       : 行业资讯采集（params.sources = 来源列表，缺省全部注册源）

调度语义（刻意简单，不上 cron）：
- schedule_time「HH:MM」，支持逗号分隔多时刻（如 "09:05,15:20"），
  weekdays「1,2,3,4,5」（ISO，周一=1；空 = 每天）
- 时刻到了且该时刻之后没跑过 → 执行；服务重启后 last_run 落在应触发时刻
  之前 → 补跑
- 一切「现在几点/今天几号」都走 Asia/Shanghai 墙钟（now_cn_naive/today_cn）：
  服务器时区非 CST 时用 datetime.now() 会让 07:30/08:00 的作业错位到别的
  业务日，并让 run_job 派生出的 start/end 整体偏移一天
- 节假日按周几近似（采集器失败/空结果会记 sync_run，不炸进程）；
  精确交易日历过滤待接 trade_calendar（预留）
"""
from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timedelta
from threading import Lock

from lquant.core.db import reader, writer
from lquant.core.logging import get_logger, run_scope
from lquant.core.types import TZ, now_cn_naive, today_cn

log = get_logger(__name__)

__all__ = ["DEFAULT_JOBS", "seed_defaults", "list_jobs", "upsert_job", "delete_job",
           "set_enabled", "run_job", "tick", "loop_forever", "freshness"]

_KINDS = ("collect", "daily", "adj_factor", "backfill",
          "reference", "daily_basic", "financial", "news")

# 失败自动重试：failed / partial（含完备性检查不过、空返回）终态后按退避
# 排 2 次重试（5/15 分钟）。skipped 是排队语义（撞活跃任务），不重试。
# 重试耗尽 → 清空重试态，等下一档调度时刻自然再跑。
MAX_RETRIES = 2
RETRY_BACKOFF_MIN = (5, 15)

# 进程内互斥：同一 sync_id 的作业不允许并发双跑。此前 HTTP 手动触发与
# tick() 调度（或两个 HTTP 请求）同时进入 run_job 时，作业体会并发执行，
# daily 作业互相撞 data_task 之外，collect/financial 等无互斥的作业会
# 重复写入/交错断点。跨进程互斥靠 DuckDB 写锁与 data_task 状态机兜底。
_JOB_LOCKS: dict[str, Lock] = {}
_JOB_LOCKS_GUARD = Lock()


def _job_lock(sync_id: str) -> Lock:
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(sync_id, Lock())

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
    updated_at   TIMESTAMP,
    retry_count  INTEGER,
    next_retry_at TIMESTAMP
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
    # 资讯此前只有 HTTP API 能触发（无 CLI、无定时作业）——「资讯同步」实际
    # 靠人手动点，随时静默停更。三档时刻覆盖盘前/盘后/晚间：快讯类来源
    # 只返回「最近」条目，同一来源重复采集由 news_id 主键去重（INSERT OR
    # IGNORE），因此多跑几次只增不重。params.sources 留空 = 全部注册源。
    {"sync_id": "news", "name": "行业资讯采集（快讯/公告/研报/榜单）",
     "kind": "news", "schedule_time": "09:05,15:20,20:00", "weekdays": "1,2,3,4,5",
     "params": {"sources": None}},
]


def _parse_schedules(schedule_time: str) -> list[str]:
    """解析 schedule_time → ["HH:MM", ...]（支持逗号分隔多档）。

    单档是历史形态，多档用于「日内多次」的作业（资讯）。非法档位直接跳过
    （由调用方校验并报错），返回空列表表示没有可用时刻 —— 这种作业永不触发，
    必须在写入时挡住（见 upsert_job）。
    """
    out: list[str] = []
    for raw in str(schedule_time or "").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            datetime.strptime(item, "%H:%M")
        except ValueError:
            continue
        if item not in out:
            out.append(item)
    return out


def _due_slot(job: dict, now: datetime) -> datetime | None:
    """今天已到点且最近的一个应触发时刻；未到任意档位返回 None。"""
    best: datetime | None = None
    for hhmm in _parse_schedules(job.get("schedule_time")):
        h, m = hhmm.split(":")
        cand = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if cand <= now and (best is None or cand > best):
            best = cand
    return best


# ---------------------------------------------------------------- CRUD

def _ensure_tables(con) -> None:
    con.execute(_DDL)
    # 旧库表结构迁移：新列逐个补齐（DuckDB 的 ADD COLUMN 无 IF NOT EXISTS
    # 语义，重复执行会报错 —— 报「列已存在」说明迁移已完成，静默即可）
    for col in ("retry_count INTEGER", "next_retry_at TIMESTAMP"):
        with contextlib.suppress(Exception):  # 列已存在 = 迁移完成，静默
            con.execute(f"ALTER TABLE sync_job ADD COLUMN {col}")  # noqa: S608
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
    """把默认作业写入 sync_job（已存在同名 sync_id 的跳过）。幂等。

    已存在的作业**不覆盖** —— 包括后续新增的默认作业（如 news）也会被
    补进来（sync_id 不存在即插入），但运维改过的排期/开关不会被重置。
    """
    n = 0
    with writer() as con:
        _ensure_tables(con)
        have = {r[0] for r in con.execute("SELECT sync_id FROM sync_job").fetchall()}
        now = now_cn_naive()
        for j in DEFAULT_JOBS:
            if j["sync_id"] in have:
                continue
            con.execute(
                "INSERT INTO sync_job (sync_id, name, kind, schedule_time, weekdays, "
                "params, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [j["sync_id"], j["name"], j["kind"], j["schedule_time"],
                 j.get("weekdays", "1,2,3,4,5"), json.dumps(j.get("params", {})),
                 j.get("enabled", True), now, now])
            n += 1
    return n


def list_jobs() -> list[dict]:
    with reader() as con:
        # 纯 SELECT：reader 连接不做 DDL（写收敛约定，_ensure_tables 留给
        # 写路径 upsert_job / run_job 落库）。表未建（首次访问）返回空清单。
        try:
            rows = con.execute(
                "SELECT sync_id, name, kind, schedule_time, weekdays, params, enabled, "
                "last_run_at, last_status, last_rows, created_at, updated_at, "
                "retry_count, next_retry_at FROM sync_job ORDER BY schedule_time").fetchall()
        except Exception:  # noqa: BLE001 - 表不存在 = 还没有任何作业
            rows = []
    return [{"sync_id": r[0], "name": r[1], "kind": r[2], "schedule_time": r[3],
             "weekdays": r[4], "params": json.loads(r[5]) if r[5] else {},
             "enabled": r[6], "last_run_at": str(r[7]) if r[7] else None,
             "last_status": r[8], "last_rows": r[9],
             "created_at": str(r[10]), "updated_at": str(r[11]),
             "retry_count": int(r[12] or 0),
             "next_retry_at": str(r[13]) if r[13] else None} for r in rows]


def upsert_job(sync_id: str, name: str, kind: str, schedule_time: str,
               weekdays: str = "1,2,3,4,5", params: dict | None = None,
               enabled: bool = True) -> dict:
    """新建/更新作业（按 sync_id 覆盖；时间格式 HH:MM，支持逗号分隔多档）。"""
    # 逐档校验：_parse_schedules 会静默丢弃非法档，多档里混进一个 "25:00"
    # 会让整条作业在那档永不触发；这里让非法输入直接报错
    slots = [s.strip() for s in str(schedule_time or "").split(",") if s.strip()]
    if not slots:
        raise ValueError(f"schedule_time 需为 HH:MM（可逗号分隔多档），收到: {schedule_time!r}")
    for item in slots:
        datetime.strptime(item, "%H:%M")
    if kind not in _KINDS:
        raise ValueError(f"未知作业类型: {kind}")
    if kind == "daily" and (params or {}).get("market") not in (
            None, "all", "sentinel"):
        raise ValueError(
            f"daily 作业 params.market 只接受 all/sentinel，收到: "
            f"{params['market']!r}")
    # 规范化：去掉重复档位（"09:05,09:05" 会让 _is_due 认为还有下一档）
    schedule_time = ",".join(dict.fromkeys(slots))
    now = now_cn_naive()
    with writer() as con:
        _ensure_tables(con)
        # 更新场景保留运行轨迹：DELETE+INSERT 若清空 last_run_at/last_status，
        # _is_due 会误判「今天还没跑」→ 编辑开关立即触发一轮补跑。
        # 但 schedule_time 变了 → 旧轨迹对应旧调度，必须重置（否则改到更晚的
        # 时间后当天不再跑）。
        old = con.execute(
            "SELECT last_run_at, last_status, last_rows, retry_count, next_retry_at "
            "FROM sync_job WHERE sync_id = ? AND schedule_time = ?",
            [sync_id, schedule_time]).fetchone()
        con.execute("DELETE FROM sync_job WHERE sync_id = ?", [sync_id])
        con.execute(
            "INSERT INTO sync_job (sync_id, name, kind, schedule_time, weekdays, "
            "params, enabled, last_run_at, last_status, last_rows, "
            "created_at, updated_at, retry_count, next_retry_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [sync_id, name, kind, schedule_time, weekdays,
             json.dumps(params or {}), enabled,
             old[0] if old else None, old[1] if old else None,
             old[2] if old else None, now, now,
             old[3] if old else None, old[4] if old else None])
    return {"sync_id": sync_id}


def delete_job(sync_id: str) -> None:
    with writer() as con:
        _ensure_tables(con)
        con.execute("DELETE FROM sync_job WHERE sync_id = ?", [sync_id])


def set_enabled(sync_id: str, enabled: bool) -> None:
    with writer() as con:
        _ensure_tables(con)
        con.execute("UPDATE sync_job SET enabled = ?, updated_at = ? WHERE sync_id = ?",
                    [enabled, now_cn_naive(), sync_id])


# ---------------------------------------------------------------- 执行

def _checkpoint_lag_check(cp_name: str, biz_day, max_lag: int = 2) -> dict:
    """checkpoint 覆盖对账：max(covered_end) 落后业务日 ≤ max_lag 个交易日 → 通过。

    检查挂了 → ok（守门员不能成为炸点：一个检查挂掉让作业永远 partial
    比漏报一次缺口更糟）。但「有 checkpoint 而覆盖明显滞后」如实返回不通过。
    """
    try:
        from lquant.data.ingest.checkpoint import Checkpoint

        cp = Checkpoint(cp_name)
        spans = [cp.covered_window(s) for s in cp.done]
        spans = [s for s in spans if s]
        if not spans:
            return {"ok": False, "reason": "no_checkpoint"}
        covered_end = max(b for _, b in spans)
        from lquant.data.store.catalog import TradeCalendarRepo

        gap = TradeCalendarRepo().range(covered_end, biz_day)
        lag = max(len(gap) - 1, 0)
        return {"ok": lag <= max_lag, "covered_end": covered_end.isoformat(),
                "lag_trading_days": lag}
    except Exception as e:  # noqa: BLE001 - 检查降级，不误报
        return {"ok": True, "reason": f"check_degraded: {type(e).__name__}: {e}"}


def _post_sync_check(kind: str, status: str, detail: dict, params: dict) -> tuple[str, dict]:
    """同步后完备性检查。返回 (status, checks)；检查不过 → partial（触发重试）。

    - daily       : 覆盖度对账（scan_coverage，repair=True 自动建补齐任务）
    - daily_basic : checkpoint 覆盖滞后对账（落后 >2 交易日 → 不过）
    - financial   : checkpoint 覆盖滞后对账（同上）
    - collect/news: 无独立检查（源错误已在 detail.errors/sources_status 暴露，
      0 行降级 partial 已在 run_job 主体处理）
    """
    checks: dict = {}
    if kind == "daily" and status != "failed":
        try:
            from lquant.data.quality.coverage import scan_coverage

            cov = scan_coverage(days=int(params.get("coverage_days", 5)), repair=True)
            daily_tbl = cov.get("tables", {}).get("daily", {})
            daily_missing = daily_tbl.get("missing_dates", []) if daily_tbl else []
            checks["coverage"] = {
                "daily_missing_days": len(daily_missing),
                "repair_created": bool(cov.get("repair", {}).get("created")),
                "issues_recorded": cov.get("issues_recorded"),
            }
            if daily_missing:
                status = "partial"
        except Exception as cov_err:  # noqa: BLE001 - 检查降级不误报
            checks["coverage"] = {"error": f"{type(cov_err).__name__}: {cov_err}"}
    elif kind == "daily_basic":
        c = _checkpoint_lag_check("daily_basic", today_cn())
        checks["checkpoint"] = c
        if not c.get("ok"):
            status = "partial"
    elif kind == "financial":
        # checkpoint 键含源名（financial_pit_{provider.name}）：tushare 缺
        # token 回落 baostock 时实际写的是 financial_pit_baostock。检查名
        # 从作业返回值取实际生效的源，猜错名会让检查恒 no_checkpoint →
        # 每轮作业都被误判 partial（假故障刷屏，真缺口反而被淹没）。
        cp = detail.get("checkpoint") if isinstance(detail, dict) else None
        if not cp:
            cp = "financial_pit_tushare"  # 兼容异常路径（detail 无 checkpoint）
        c = _checkpoint_lag_check(cp, today_cn(), max_lag=3)
        checks["checkpoint"] = c
        if not c.get("ok"):
            status = "partial"
    return status, checks


def run_job(job: dict, *, demo: bool | None = None) -> dict:
    """执行一个作业并记录 sync_run / 更新 sync_job 状态。失败不抛出。

    时间基：started/finished 与派生的 start/end 全部取 Asia/Shanghai 墙钟。
    服务器时区非 CST 时，本机 datetime.now() 会让 collect 的 trade_date、
    daily/adj/financial 的 (today - days) 窗口整体偏移一天 —— 采到错日的数据
    且看不出任何异常。
    """
    sync_id = str(job.get("sync_id") or job.get("kind") or "unknown")
    lock = _job_lock(sync_id)
    if not lock.acquire(blocking=False):
        # 调度与手动触发并发：排队语义直接跳过（与 data_task 冲突一致），
        # 不阻塞调用线程；本轮未执行任何工作，不写 sync_run
        log.warning(f"sync 作业并发触发，跳过本轮 sync_id={sync_id}")
        return {"run_id": None, "status": "skipped", "rows": 0,
                "elapsed_sec": 0.0, "attempt": 0,
                "detail": {"skipped": True, "reason": "already_running"}}
    try:
        return _run_job_locked(job, demo=demo)
    finally:
        lock.release()


def _run_job_locked(job: dict, *, demo: bool | None = None) -> dict:
    """run_job 的执行体：调用方必须已持有该 sync_id 的进程内锁。"""
    with run_scope() as run_id:
        started = now_cn_naive()
        # 业务日单独取（不依赖墙钟的日期部分，避免 tz 归一逻辑分散在多处）
        biz_day = today_cn()
        # 重试轮次：来自 sync_job 的 retry_count（初始跑=1，重试跑=2/3）
        attempt = int(job.get("retry_count") or 0) + 1
        params = job.get("params") or {}
        kind = job["kind"]
        rows, detail, status = 0, {}, "ok"
        try:
            if kind == "collect":
                from lquant.market.scheduler import collect_and_save

                d = params.get("demo", False) if demo is None else demo
                res = collect_and_save(schedule=params.get("schedule"),
                                       trade_date=biz_day, demo=d)
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
                        start=(biz_day
                               - timedelta(days=int(params.get("days", 10)))).isoformat())
                else:
                    # 全市场增量：建 data_task（历史可查）后走执行器
                    from lquant.data.ingest import tasks as data_tasks

                    # auto_crosscheck 透传 job params（缺省 True），运维可在 sync_job 里配置关闭
                    try:
                        t = data_tasks.create_task(
                            "daily_update", {"days": int(params.get("days", 10)),
                                             "auto_crosscheck": params.get("auto_crosscheck", True)})
                    except data_tasks.TaskConflictError as e:
                        # 手动任务/上次作业还没跑完：跳过本轮并如实记录，不算失败
                        # （此前直接 failed，监控页每轮都告警一次假故障）
                        status = "skipped"
                        detail = {"skipped": True, "reason": f"{type(e).__name__}: {e}"}
                        log.warning(
                            f"daily 作业跳过（存在未完成数据任务）sync_id={job.get('sync_id')}: {e}")
                    else:
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
                    start=(biz_day
                           - timedelta(days=int(params.get("days", 14)))).isoformat(),
                    merge=bool(params.get("merge", True)))
                rows = int(res.get("rows", 0))
                detail = res if isinstance(res, dict) else {}
            elif kind == "financial":
                from lquant.data.ingest.financial import backfill_financial
                from lquant.data.store.catalog import SecurityRepo

                syms = SecurityRepo().stock_symbols(include_delisted=False)
                win_start = (biz_day
                             - timedelta(days=int(params.get("days", 90)))).isoformat()
                res = backfill_financial(
                    symbols=syms, start=win_start, end=biz_day.isoformat())
                # done/skipped_covered/rows 分开暴露 —— 只看一个总数时
                # 「全被断点跳过」与「真的没有新财报」长得一模一样
                rows = int(res.get("done", 0))
                detail = res
            elif kind == "news":
                # 资讯作业此前不存在：只有 HTTP API 能触发，无自动同步。
                # 复用 news_task 状态机（有互斥与 cancel/retry 语义），
                # 撞上运行中的手工任务时记 skipped 而不是 failed。
                from lquant.news import tasks as news_tasks

                srcs = params.get("sources") or None
                try:
                    with writer() as con:
                        news_tasks.init_news_task_ddl(con)
                        t = news_tasks.create_task(
                            con, "daily", {"sources": srcs, "date": biz_day.isoformat()})
                        task = news_tasks.execute_task(con, t["task_id"])
                except news_tasks.TaskConflictError as e:
                    status = "skipped"
                    detail = {"skipped": True, "reason": f"{type(e).__name__}: {e}"}
                    log.warning(
                        f"news 作业跳过（存在未完成资讯任务）"
                        f"sync_id={job.get('sync_id')}: {e}")
                else:
                    rows = int(task.get("rows_written") or 0)
                    detail = {"task_id": task["task_id"], "task_status": task["status"],
                              "sources_status": task.get("sources_status")}
                    if task["status"] == "partial":
                        status = "partial"
                    elif task["status"] == "failed":
                        status = "failed"
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
                log.error(f"未知作业类型 kind={kind} sync_id={job.get('sync_id')}")
        except Exception as e:  # noqa: BLE001
            log.exception(f"sync 作业执行失败 kind={kind} sync_id={job.get('sync_id')}")
            status = "failed"
            detail = {"error": f"{type(e).__name__}: {e}"}

        # 完备性检查：作业本体成功（ok/partial）才检查；检查不过 → partial
        # （落入重试/告警链路）。检查自身挂了不改变作业状态（只记录），
        # 检查是守门员不是炸点。
        if status in ("ok", "partial"):
            status, checks = _post_sync_check(kind, status, detail, params)
            detail = {**detail, "checks": checks}
        else:
            detail = {**detail, "checks": {"skipped": f"status={status}"}}

        # 检查完成后统一取终态时间：告警/落库/重试排程共用
        finished = now_cn_naive()

        # 0 行不算健康：日线/采集类作业空返回记 partial，避免静默缺口被掩盖
        # （skipped 不参与该判定：它发生在建任务之前，rows 本就为 0）
        # financial 同样纳入：全市场窗口内 0 只被处理时，要么断点全跳过、
        # 要么标的池为空 —— 两种都必须有人看见（此前静默报 ok）。
        # news 不纳入：同一来源重复采集由主键去重，0 新增是正常语义。
        # 该判定必须在 emit 之前：partial 化之后才会进告警环 —— 顺序反了
        # 的话零行作业落库是 partial、告警环却看不到（监控假绿）
        if rows == 0 and kind in ("daily", "collect", "adj_factor", "financial") \
                and status == "ok":
            status = "partial"
            detail = {**detail, "zero_rows": True}

        # 告警：终态非 ok/skipped → 监控错误环（monitor 错误环 → error_logs 可查）
        # skipped（撞活跃任务）是正常排队语义，不告警
        if status not in ("ok", "skipped"):
            _emit_sync_error(job, kind, status, detail)
        try:
            with writer() as con:
                _ensure_tables(con)
                con.execute(
                    "INSERT OR REPLACE INTO sync_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, job.get("sync_id"), job.get("name"), kind,
                     started, finished, rows, status, json.dumps(detail, default=str)])
                # 失败/部分失败排自动重试（skipped 不重试 —— 排队语义）。
                # 重试耗尽 → 清空重试态，等下一档调度时刻自然再跑。
                if status in ("failed", "partial") and attempt <= MAX_RETRIES:
                    backoff_min = RETRY_BACKOFF_MIN[min(attempt, len(RETRY_BACKOFF_MIN)) - 1]
                    next_retry = finished + timedelta(minutes=backoff_min)
                    detail = {**detail, "attempt": attempt,
                              "next_retry_at": next_retry.isoformat()}
                    con.execute(
                        "UPDATE sync_job SET last_run_at = ?, last_status = ?, "
                        "last_rows = ?, retry_count = ?, next_retry_at = ?, "
                        "updated_at = ? WHERE sync_id = ?",
                        [finished, status, rows, attempt, next_retry, finished,
                         job.get("sync_id")])
                else:
                    con.execute(
                        "UPDATE sync_job SET last_run_at = ?, last_status = ?, "
                        "last_rows = ?, retry_count = NULL, next_retry_at = NULL, "
                        "updated_at = ? WHERE sync_id = ?",
                        [finished, status, rows, finished, job.get("sync_id")])
        except Exception:  # noqa: BLE001  记录失败不影响主流程
            log.exception(f"sync_run 记录失败 sync_id={job.get('sync_id')} run_id={run_id}")
        return {"run_id": run_id, "status": status, "rows": rows,
                "elapsed_sec": round((finished - started).total_seconds(), 1),
                "attempt": attempt, "detail": detail}


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
        # 静默降级但留痕：ring 不可用/构造失败在日志里可见，排查告警断链时有用
        log.opt(exception=True).warning(
            "sync 错误环写入失败 sync_id={}", job.get("sync_id", kind))


def history(limit: int = 50) -> list[dict]:
    with reader() as con:
        # 纯 SELECT（同 list_jobs）：表未建返回空历史，不在 reader 上建表
        try:
            rows = con.execute(
                "SELECT run_id, sync_id, job_name, kind, started_at, finished_at, "
                "rows, status, detail FROM sync_run ORDER BY started_at DESC LIMIT ?",
                [int(limit)]).fetchall()
        except Exception:  # noqa: BLE001 - 表不存在 = 还没有运行记录
            rows = []
    return [{"run_id": r[0], "sync_id": r[1], "job_name": r[2], "kind": r[3],
             "started_at": str(r[4]), "finished_at": str(r[5]), "rows": r[6],
             "status": r[7], "detail": json.loads(r[8]) if r[8] else {}} for r in rows]


# ---------------------------------------------------------------- 新鲜度

def freshness() -> dict:
    """各类数据「最新到哪一天」——用于一眼看出同步是不是真的在跑。

    每一项都独立降级：表不存在/查不动就返回 None，绝不因为一个模块没就位
    就让整个状态视图报错（同步状态查不了本身会掩盖真问题）。

    注意 last_status=ok 不代表数据是新的：作业空转（无到期、断点全跳过、
    源零返回）都会记 ok。只有看新鲜度才知道数据有没有跟上。
    """
    out: dict = {"daily_lake": None, "news": None, "financial_pit": None,
                 "lag_days": None}

    # 日线湖：最新交易日 + 相对今天落后几个交易日
    try:
        import polars as pl  # noqa: PLC0415

        # 必须走 read_daily：它带 missing_columns/extra_columns 容错。
        # 直接 scan_parquet(lake_glob(...)) 会在年文件 schema 漂移时炸
        # （实测 extra column 'year'）——状态视图不能因读取方式而失效
        from lquant.data.store.parquet import read_daily

        latest = read_daily().select(pl.col("trade_date").max()).collect().item()
        out["daily_lake"] = str(latest) if latest else None
        if latest:
            from lquant.data.store.catalog import TradeCalendarRepo

            gap = TradeCalendarRepo().range(latest, today_cn())
            # range 含首尾 → 减 1 才是「比日历落后几个交易日」
            out["lag_days"] = max(len(gap) - 1, 0)
    except Exception as e:  # noqa: BLE001 - 空湖/库不可用都降级
        log.debug(f"日线湖新鲜度不可得: {e}")

    try:
        from lquant.core.db import reader as _reader

        with _reader() as con:
            row = con.execute(
                "SELECT max(published_at), count(*) FILTER "
                "(WHERE CAST(published_at AS DATE) = CAST(? AS DATE)) "
                "FROM news_item", [today_cn()]).fetchone()
        if row:
            out["news"] = {"latest": str(row[0]) if row[0] else None,
                           "today_rows": int(row[1] or 0)}
    except Exception as e:  # noqa: BLE001 - 表未建/库不可用
        log.debug(f"资讯新鲜度不可得: {e}")

    try:
        from lquant.data.ingest.checkpoint import Checkpoint

        cp = Checkpoint("financial_pit_tushare")
        spans = [cp.covered_window(s) for s in cp.done]
        spans = [s for s in spans if s]
        if spans:
            out["financial_pit"] = {
                "covered_start": min(a for a, _ in spans).isoformat(),
                "covered_end": max(b for _, b in spans).isoformat(),
                "symbols": len(spans),
                "marked": len(cp),
            }
    except Exception as e:  # noqa: BLE001
        log.debug(f"财务覆盖区间不可得: {e}")
    return out


# ---------------------------------------------------------------- 调度

def _is_due(job: dict, now: datetime) -> bool:
    """是否到期。now 必须是 Asia/Shanghai 墙钟（naive）。

    多档 schedule_time：取「今天已到点且最近的那一档」作为本次应触发时刻，
    last_run 早于它即到期。单档时与旧行为完全一致（唯一的档位就是它）。
    """
    if not job.get("enabled", False):
        return False
    # 重试到期：next_retry_at 已到 → 必须重试（忽略 weekdays —— 重试发生的
    # 当天基本就是业务日；跨天残留在节假日时 tick 的交易日过滤会兜底跳过）
    next_retry = job.get("next_retry_at")
    if next_retry:
        retry_dt = next_retry if isinstance(next_retry, datetime) \
            else datetime.fromisoformat(str(next_retry))
        if retry_dt.tzinfo is not None:
            retry_dt = retry_dt.astimezone(TZ).replace(tzinfo=None)
        if now >= retry_dt:
            return True
    weekdays = str(job.get("weekdays") or "1,2,3,4,5")
    if weekdays.strip() and str(now.isoweekday()) not in [w.strip() for w in weekdays.split(",")]:
        return False
    # 跨天补跑：last_run 落后于「今天的应触发时刻」即到期。此前按
    # 「今天是否跑过」判断，宕机跨天后作业被永久跳过 —— 窗口滑过即缺口。
    scheduled = _due_slot(job, now)
    if scheduled is None:
        return False
    last = job.get("last_run_at")
    if last:
        last_dt = last if isinstance(last, datetime) else datetime.fromisoformat(str(last))
        if last_dt.tzinfo is not None:         # 兼容历史写入的 tz-aware 值
            last_dt = last_dt.astimezone(TZ).replace(tzinfo=None)
        if last_dt >= scheduled:               # 本次应触发时刻之后已跑过
            return False
    return True


def _trading_day_ok(d) -> bool:
    """日历覆盖该日时按 is_open 过滤；日历缺失该日时放行（回退周几近似）。

    日历为空/未同步不能让所有作业静默全跳 —— 那会制造新的「静默缺口」。
    """
    try:
        # 纯 SELECT：reader 连接不做 DDL（写收敛约定 —— 表的创建归写路径）。
        # 表不存在时 SELECT 抛异常 → 放行，与「日历为空放行」同语义。
        with reader() as con:
            row = con.execute(
                "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
                [d]).fetchone()
        return bool(row[0]) if row else True
    except Exception:  # noqa: BLE001 - 日历不可用 → 放行
        return True


def tick(now: datetime | None = None) -> list[dict]:
    """跑一遍所有到期作业。返回执行结果列表。

    now 缺省取 Asia/Shanghai 墙钟。作业的 schedule_time/weekdays 都是
    运维眼里的 CST 时刻，用本机 datetime.now() 判断到期会在非 CST 服务器上
    整体错位；显式传入 tz-aware 值时统一换算成 CN 墙钟再比较。
    """
    now = now or now_cn_naive()
    if now.tzinfo is not None:
        now = now.astimezone(TZ).replace(tzinfo=None)
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
                log.info(f"[sync] {r['sync_id']} → {r['status']} rows={r['rows']}")
        except Exception as e:  # noqa: BLE001  调度循环绝不退出
            log.exception(f"[warn] sync tick 异常: {e}")
        time.sleep(interval)
