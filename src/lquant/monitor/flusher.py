"""flusher：http 进程内唯一写 monitor.duckdb 的落盘线程。"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta

from lquant.monitor.emit import EVENTS_KEY
from lquant.monitor.ring import api_ring, local_events

_LOG = logging.getLogger(__name__)

_MAX_BATCH = 1000  # 单周期每表最多处理条数，防异常源无限 drain
_JOIN_TIMEOUT = 15.0  # stop_flusher join 线程上限秒数

_CLEANUP_STATE = {"last": None}
_STOP = threading.Event()
_thread: threading.Thread | None = None
_started = False  # 是否以启用状态启动过（决定 stop 时是否 final flush）

# 写失败时暂存待重试的数据（下个周期优先重写，成功后清空）
_PENDING_LOCK = threading.Lock()
_PENDING_API: list = []
_PENDING_TASK: list = []


def _redis_available() -> bool:
    from lquant.server.jobs import _redis_available

    return _redis_available()


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def _monitor_con():
    """monitor.duckdb 连接（flusher 写与查询共享同路径同配置实例）。"""
    import duckdb

    from lquant.core.config import get_settings

    return duckdb.connect(get_settings().monitor_db_path)


def ensure_tables(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS metrics_api (
        ts TIMESTAMP, route VARCHAR, method VARCHAR, status INTEGER,
        duration_ms DOUBLE, dur_category VARCHAR,
        PRIMARY KEY (ts, route, method, status, duration_ms))""")
    con.execute("""CREATE TABLE IF NOT EXISTS metrics_task (
        event_ts TIMESTAMP, job_id VARCHAR, job_name VARCHAR, event VARCHAR,
        queue VARCHAR, enqueued_at TIMESTAMP, started_at TIMESTAMP,
        finished_at TIMESTAMP, elapsed_ms DOUBLE, queue_delay_ms DOUBLE,
        message VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS metrics_sys (
        ts TIMESTAMP, proc_name VARCHAR, pid INTEGER, cpu_pct DOUBLE,
        mem_rss_mb DOUBLE, current_job VARCHAR,
        PRIMARY KEY (ts, proc_name))""")


def _ts(v: float | None) -> datetime | None:
    return datetime.fromtimestamp(v) if v is not None else None


def _write_api(con, pts) -> int:
    if not pts:
        return 0
    con.executemany("INSERT OR IGNORE INTO metrics_api VALUES (?, ?, ?, ?, ?, ?)",
                    [(_ts(p.ts), p.route, p.method, p.status,
                      p.duration_ms, p.dur_category) for p in pts])
    return len(pts)


def _write_task(con, evs) -> int:
    if not evs:
        return 0
    # task 表无 UNIQUE/PK 约束，DuckDB 不支持无约束的 OR IGNORE；无冲突可能，用裸 INSERT
    con.executemany("INSERT INTO metrics_task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(_ts(e.event_ts), e.job_id, e.job_name, e.event, e.queue,
                      _ts(e.enqueued_at), _ts(e.started_at), _ts(e.finished_at),
                      e.elapsed_ms, e.queue_delay_ms, e.message) for e in evs])
    return len(evs)


def _pop_redis_events(r) -> list[str]:
    """RPOP 取走原始 payload（FIFO：最旧先出），坏消息直接丢弃。"""
    popped: list[str] = []
    for _ in range(_MAX_BATCH):
        raw = r.rpop(EVENTS_KEY)
        if raw is None:
            break
        popped.append(raw)
    return popped


def _parse_redis_events(popped) -> list:
    from lquant.monitor.types import TaskEvent

    evs: list[TaskEvent] = []
    for raw in popped:
        try:
            d = json.loads(raw)
            evs.append(TaskEvent(
                event_ts=d.get("event_ts") or time.time(), job_id=d["job_id"],
                job_name=d.get("job_name") or d["job_id"],
                event=d.get("event") or "finished", queue=d.get("queue"),
                enqueued_at=d.get("enqueued_at"), started_at=d.get("started_at"),
                finished_at=d.get("finished_at"), elapsed_ms=d.get("elapsed_ms"),
                queue_delay_ms=d.get("queue_delay_ms"), message=d.get("message")))
        except Exception:  # noqa: BLE001 - 坏消息丢弃不炸 flusher
            _LOG.warning("坏事件 payload 丢弃", exc_info=True)
    return evs


def _requeue_redis_events(r, popped) -> None:
    """写失败时把已 RPOP 的 payload 按原 FIFO 顺序 RPUSH 回队尾。"""
    if not popped:
        return
    try:
        for raw in popped:
            r.rpush(EVENTS_KEY, raw)
    except Exception:  # noqa: BLE001 - 回队失败只能丢，仅 log
        _LOG.warning("Redis 事件回队失败", exc_info=True)


def _drain_sys_samples(r) -> list:
    from lquant.monitor.types import ProcSample

    out: list[ProcSample] = []
    try:
        for key in r.scan_iter(match="lquant:monitor:proc:*"):
            raw = r.get(key)
            if not raw:
                continue
            d = json.loads(raw)
            out.append(ProcSample(
                ts=d.get("ts") or time.time(),
                proc_name=d.get("proc_name") or key.decode().rsplit(":", 1)[-1],
                pid=int(d.get("pid") or 0), cpu_pct=d.get("cpu_pct"),
                mem_rss_mb=d.get("mem_rss_mb"), current_job=d.get("current_job")))
    except Exception:  # noqa: BLE001
        _LOG.warning("proc 样本读取失败", exc_info=True)
    return out


def _write_sys(con, samples) -> int:
    if not samples:
        return 0
    con.executemany("INSERT OR IGNORE INTO metrics_sys VALUES (?, ?, ?, ?, ?, ?)",
                    [(_ts(s.ts), s.proc_name, s.pid, s.cpu_pct, s.mem_rss_mb,
                      s.current_job) for s in samples])
    return len(samples)


def _should_cleanup_today() -> bool:
    """只判断不更新状态；清理成功后由 _mark_cleanup_done 记账。"""
    return _CLEANUP_STATE["last"] != date.today()


def _mark_cleanup_done() -> None:
    _CLEANUP_STATE["last"] = date.today()


def _cleanup(con, retention_days: int, now: float | None = None) -> None:
    cutoff = datetime.fromtimestamp(now) if now is not None else datetime.now()
    cutoff -= timedelta(days=int(retention_days))
    con.execute("DELETE FROM metrics_api WHERE ts < ?", [cutoff])
    con.execute("DELETE FROM metrics_task WHERE event_ts < ?", [cutoff])
    con.execute("DELETE FROM metrics_sys WHERE ts < ?", [cutoff])


def _stash(api: list, task: list) -> None:
    """写失败时把已 drain 的数据暂存，下个周期优先重写。"""
    with _PENDING_LOCK:
        _PENDING_API.extend(api)
        _PENDING_TASK.extend(task)


def _take_pending() -> tuple[list, list]:
    with _PENDING_LOCK:
        api, task = list(_PENDING_API), list(_PENDING_TASK)
        _PENDING_API.clear()
        _PENDING_TASK.clear()
        return api, task


def flush_once(now: float | None = None) -> dict:
    """单周期：返回各表写入行数；失败时数据暂存/回队，待重试不丢弃。"""
    out = {"api": 0, "task": 0, "sys": 0}
    from lquant.core.config import get_settings

    if not get_settings().monitor_enabled:
        return out  # 关闭监控：不建库不写库
    r = None
    if _redis_available():
        try:
            r = _get_redis()
        except Exception:  # noqa: BLE001
            r = None
    try:
        con = _monitor_con()
    except Exception:  # noqa: BLE001 - 连接失败时源数据未被消费，天然保留
        _LOG.warning("monitor.duckdb 连接失败，数据保留待重试", exc_info=True)
        return out
    try:
        ensure_tables(con)
        # 先取出全部数据到本地（此后源已被消费，失败必须归还）
        pend_api, pend_task = _take_pending()
        api = pend_api + list(api_ring.drain())
        task = pend_task + list(local_events.drain())
        popped: list[str] = []
        if r is not None:
            popped = _pop_redis_events(r)
            task.extend(_parse_redis_events(popped))
        samples = _drain_sys_samples(r) if r is not None else []
        try:
            out["api"] = _write_api(con, api)
            out["task"] = _write_task(con, task)
            if r is not None:
                out["sys"] = _write_sys(con, samples)
            from lquant.core.config import get_settings

            do_cleanup = _should_cleanup_today()
            if do_cleanup:
                _cleanup(con, get_settings().monitor_retention_days, now)
                _mark_cleanup_done()
            con.commit()
        except Exception:
            _stash(api, task)  # 未落盘数据暂存待重试
            _requeue_redis_events(r, popped)  # Redis 侧按 FIFO 回队
            raise
    except Exception:  # noqa: BLE001
        _LOG.warning("flusher 落盘失败，数据保留待重试", exc_info=True)
        return out
    finally:
        with contextlib.suppress(Exception):
            con.close()
    return out


def _cycle(interval: float) -> None:
    while not _STOP.wait(interval):
        flush_once()


def start_flusher() -> threading.Thread | None:
    global _thread, _started
    from lquant.core.config import get_settings

    if not get_settings().monitor_enabled:
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _STOP.clear()
    _thread = threading.Thread(target=_cycle,
                               args=(get_settings().monitor_flush_interval_sec,),
                               name="monitor-flusher", daemon=True)
    _thread.start()
    _started = True
    return _thread


def stop_flusher() -> None:
    global _thread, _started
    _STOP.set()
    t = _thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=_JOIN_TIMEOUT)
    _thread = None
    if not _started:
        return  # 未启动或 enabled=False：不 final flush（避免创建/写 monitor.duckdb）
    _started = False
    flush_once()  # final flush：环缓冲剩余 + Redis 样本剩余（join 后无并发写）
