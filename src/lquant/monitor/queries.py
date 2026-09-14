"""monitor 查询层：monitor.duckdb 聚合 + Redis 快照 + 主库 collect_log。"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

_LOG = logging.getLogger(__name__)

RANGE_BUCKET = {"1h": 60, "6h": 300, "24h": 900, "7d": 7200}
RANGE_SEC = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 86400}
ONLINE_SEC = 15


def _redis_available() -> bool:
    from lquant.server.jobs import _redis_available

    return _redis_available()


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def _con():
    from lquant.monitor.flusher import _monitor_con

    return _monitor_con()


def range_bucket(range_name: str) -> int:
    if range_name not in RANGE_BUCKET:
        raise ValueError(f"range 必须是 {sorted(RANGE_BUCKET)}")
    return RANGE_BUCKET[range_name]


def range_sec(range_name: str) -> int:
    if range_name not in RANGE_SEC:
        raise ValueError(f"range 必须是 {sorted(RANGE_SEC)}")
    return RANGE_SEC[range_name]


def api_latency_series(range_name: str) -> list[dict]:
    bs = range_bucket(range_name)
    rs = range_sec(range_name)
    con = _con()
    try:
        rows = con.execute(f"""
            WITH grid AS (
                SELECT unnest(generate_series(
                    cast(floor((epoch(cast(now() as timestamp))::BIGINT - {rs}) / {bs}) AS BIGINT) * {bs},
                    cast(floor(epoch(cast(now() as timestamp))::BIGINT / {bs}) AS BIGINT) * {bs}, {bs})) AS e),
            agg AS (
                SELECT floor(epoch(ts) / {bs}) * {bs} AS e,
                       count(*) AS cnt, avg(duration_ms) AS avg_ms,
                       quantile_cont(duration_ms, 0.5) AS p50,
                       quantile_cont(duration_ms, 0.95) AS p95,
                       avg(CASE WHEN dur_category = 'error' THEN 1.0
                                ELSE 0.0 END) AS err_rate
                FROM metrics_api
                WHERE ts > cast(now() as timestamp) - INTERVAL {rs} SECOND
                GROUP BY 1)
            SELECT grid.e, coalesce(agg.cnt, 0) AS cnt, agg.avg_ms,
                   agg.p50, agg.p95, coalesce(agg.err_rate, 0.0) AS err_rate
            FROM grid LEFT JOIN agg USING (e) ORDER BY grid.e
        """).fetchall()
    finally:
        con.close()
    return [{"bucket": datetime.fromtimestamp(r[0], tz=UTC).isoformat(), "count": r[1],
             "avg": r[2], "p50": r[3], "p95": r[4], "err_rate": r[5]}
            for r in rows]


def slowest_routes(range_name: str, limit: int = 10) -> list[dict]:
    rs = range_sec(range_name)
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT route, count(*) AS cnt, avg(duration_ms) AS avg_ms,
                   quantile_cont(duration_ms, 0.95) AS p95,
                   avg(CASE WHEN dur_category = 'error' THEN 1.0
                            ELSE 0.0 END) AS err_rate
            FROM metrics_api
            WHERE ts > cast(now() as timestamp) - INTERVAL {rs} SECOND
            GROUP BY route ORDER BY p95 DESC LIMIT {int(limit)}
        """).fetchall()
    finally:
        con.close()
    return [{"route": r[0], "count": r[1], "avg": r[2], "p95": r[3],
             "err_rate": r[4]} for r in rows]


def task_latency_series(range_name: str) -> list[dict]:
    bs = range_bucket(range_name)
    rs = range_sec(range_name)
    con = _con()
    try:
        rows = con.execute(f"""
            WITH grid AS (
                SELECT unnest(generate_series(
                    cast(floor((epoch(cast(now() as timestamp))::BIGINT - {rs}) / {bs}) AS BIGINT) * {bs},
                    cast(floor(epoch(cast(now() as timestamp))::BIGINT / {bs}) AS BIGINT) * {bs}, {bs})) AS e),
            agg AS (
                SELECT floor(epoch(event_ts) / {bs}) * {bs} AS e,
                       quantile_cont(elapsed_ms, 0.95) AS p95_elapsed,
                       quantile_cont(queue_delay_ms, 0.95) AS p95_delay,
                       count(*) AS cnt
                FROM metrics_task
                WHERE event_ts > cast(now() as timestamp) - INTERVAL {rs} SECOND
                  AND event IN ('finished', 'failed')
                GROUP BY 1)
            SELECT grid.e, agg.p95_delay, agg.p95_elapsed,
                   coalesce(agg.cnt, 0) AS cnt
            FROM grid LEFT JOIN agg USING (e) ORDER BY grid.e
        """).fetchall()
    finally:
        con.close()
    return [{"bucket": datetime.fromtimestamp(r[0], tz=UTC).isoformat(), "p95_delay": r[1],
             "p95_elapsed": r[2], "count": r[3]} for r in rows]


def queue_depths() -> list[dict]:
    from lquant.server.jobs import QUEUES

    out: list[dict] = []
    if not _redis_available():
        return [{"queue": qname, "pending": None, "failed": None}
                for qname in QUEUES]
    try:
        from rq.queue import Queue
        from rq.registry import FailedJobRegistry, StartedJobRegistry

        r = _get_redis()
        for qname in QUEUES:
            queue = Queue(qname, connection=r)
            pending = queue.count + StartedJobRegistry(qname, connection=r).count
            failed = FailedJobRegistry(qname, connection=r).count
            out.append({"queue": qname, "pending": int(pending),
                        "failed": int(failed)})
    except Exception:  # noqa: BLE001
        _LOG.warning("队列深度查询失败", exc_info=True)
        return [{"queue": qname, "pending": None, "failed": None}
                for qname in QUEUES]
    return out


def proc_statuses() -> list[dict]:
    if not _redis_available():
        return []
    try:
        r = _get_redis()
        out = []
        for key in r.scan_iter(match="lquant:monitor:proc:*"):
            raw = r.get(key)
            if not raw:
                continue
            d = json.loads(raw)
            age = time.time() - float(d.get("ts") or 0)
            out.append({"proc_name": d.get("proc_name") or
                        key.decode().rsplit(":", 1)[-1],
                        "pid": d.get("pid"), "cpu_pct": d.get("cpu_pct"),
                        "mem_rss_mb": d.get("mem_rss_mb"),
                        "current_job": d.get("current_job"),
                        "online": age < ONLINE_SEC, "age_sec": round(age, 1)})
        return sorted(out, key=lambda x: x["proc_name"])
    except Exception:  # noqa: BLE001
        _LOG.warning("proc 状态查询失败", exc_info=True)
        return []


def api_live() -> dict:
    from lquant.monitor.ring import api_ring

    now = time.time()
    pts = [p for p in api_ring.snapshot() if now - p.ts <= 300]
    if not pts:
        return {"count": 0, "p50": None, "p95": None, "err_rate": 0.0}
    durs = sorted(p.duration_ms for p in pts)
    errs = sum(1 for p in pts if p.status >= 400)

    def pct(v: list[float], qf: float) -> float:
        i = max(0, min(len(v) - 1, int(qf * len(v)) - 1))
        return v[i]

    return {"count": len(pts), "p50": pct(durs, 0.5), "p95": pct(durs, 0.95),
            "err_rate": errs / len(pts)}


def recent_task_events(limit: int = 50) -> list[dict]:
    if not _redis_available():
        return []
    try:
        r = _get_redis()
        return [json.loads(x) for x in
                r.lrange("lquant:monitor:recent", 0, limit - 1)]
    except Exception:  # noqa: BLE001
        return []


def data_pulls() -> dict:
    """主库 collect_log：recent 200 + by_job 聚合（运维视角）。"""
    from lquant.core.db import reader

    with reader() as con:
        recent = con.execute("""
            SELECT job, trade_date, started_at, finished_at, rows, status, message
            FROM collect_log ORDER BY started_at DESC LIMIT 200
        """).fetchall()
        by_job = con.execute("""
            SELECT job, count(*) AS cnt,
                   avg(epoch(finished_at) - epoch(started_at)) * 1000 AS avg_ms,
                   sum(CASE WHEN status NOT IN ('ok', 'success') THEN 1 ELSE 0 END)
                       AS failed
            FROM collect_log
            WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
            GROUP BY job ORDER BY avg_ms DESC
        """).fetchall()
    return {
        "recent": [{"job": r[0], "trade_date": str(r[1]), "started_at": str(r[2]),
                    "finished_at": str(r[3]), "rows": r[4], "status": r[5],
                    "duration_ms": (int((r[3] - r[2]).total_seconds() * 1000)
                                    if r[2] and r[3] else None),
                    "message": r[6]} for r in recent],
        "by_job": [{"job": r[0], "count": r[1], "avg_duration_ms": round(r[2], 1)
                    if r[2] is not None else None, "failed": int(r[3] or 0)}
                   for r in by_job],
    }


def summary() -> dict:
    return {"procs": proc_statuses(), "queues": queue_depths(),
            "api_live": api_live(), "task_recent": recent_task_events(10)}


def error_logs(range_name: str, route: str | None = None,
               limit: int = 200) -> dict:
    """错误日志查询：monitor.duckdb metrics_api_error，按 ts 倒序。

    DuckDB 不可用/表不存在时降级返回空集（首次运行尚无错误表属正常态）。
    """
    rs = range_sec(range_name)
    con = None
    try:
        con = _con()  # 连接失败同样降级为空集（此前在 try 外会炸 500）
        params: list = []
        where = f"WHERE ts > cast(now() as timestamp) - INTERVAL {rs} SECOND"
        if route:
            where += " AND route LIKE ?"
            params.append(f"%{route}%")
        total = con.execute(
            f"SELECT count(*) FROM metrics_api_error {where}", params).fetchone()[0]
        rows = con.execute(f"""
            SELECT ts, route, method, status, error_type, message, traceback_tail
            FROM metrics_api_error
            {where}
            ORDER BY ts DESC LIMIT {int(limit)}
        """, params).fetchall()
    except Exception:  # noqa: BLE001 - 表不存在时降级为空；其余真实故障记日志便于发现
        _LOG.warning("error_logs 查询失败，降级为空集", exc_info=True)
        return {"items": [], "total": 0}
    finally:
        if con is not None:
            con.close()
    items = [{"ts": str(r[0]), "route": r[1], "method": r[2], "status": r[3],
              "error_type": r[4], "message": r[5], "traceback_tail": r[6]}
             for r in rows]
    return {"items": items, "total": total}
