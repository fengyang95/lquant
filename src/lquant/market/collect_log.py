"""采集健康度（方案 M9，P0）。

collect_log 表在 DDL 里躺了很久没人写 —— 本模块补齐闭环：
- record()：每个采集器每次执行落一行（成功/失败/空结果都记）；
- health()：按采集器聚合 —— 最近成功时间、30 天成功率、当日缺口。

「易失数据当天没采到 = 永久丢失」，所以缺口检测按采集器自己的
schedule 时点判断：过了时点还没数据就告警，而不是等第二天。
"""
from __future__ import annotations

from datetime import date, datetime

from lquant.core.db import reader, writer

_DDL = """
CREATE TABLE IF NOT EXISTS collect_log (
    job         VARCHAR,
    trade_date  DATE,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    rows        INTEGER,
    status      VARCHAR,
    message     VARCHAR,
    PRIMARY KEY (job, trade_date)
)
"""

# 各采集器的期望时点（与 scheduler.SCHEDULES 对应的小时截止）：
# 过了这个点还没有当日数据 → 缺口
_EXPECTED_BY = {"preopen": 10, "intraday": 15, "close": 16, "evening": 19}


def _ensure(con) -> None:
    con.execute(_DDL)


def record(job: str, trade_date: date, started_at: datetime,
           finished_at: datetime, rows: int, status: str,
           message: str = "") -> None:
    """记录一次采集执行。同 (job, trade_date) 覆盖写（重跑幂等）。

    用先删后插而不是 INSERT OR REPLACE —— 兼容无主键的旧表结构。
    """
    with writer() as con:
        _ensure(con)
        con.execute("DELETE FROM collect_log WHERE job = ? AND trade_date = ?",
                    [job, trade_date])
        con.execute(
            "INSERT INTO collect_log VALUES (?, ?, ?, ?, ?, ?, ?)",
            [job, trade_date, started_at, finished_at, int(rows), status, message],
        )


def record_batch(entries: list[dict]) -> int:
    """批量记录：[{job, trade_date, started_at, finished_at, rows, status, message}]。"""
    if not entries:
        return 0
    with writer() as con:
        _ensure(con)
        for e in entries:
            con.execute("DELETE FROM collect_log WHERE job = ? AND trade_date = ?",
                        [e["job"], e["trade_date"]])
            con.execute(
                "INSERT INTO collect_log VALUES (?, ?, ?, ?, ?, ?, ?)",
                [e["job"], e["trade_date"], e["started_at"], e["finished_at"],
                 int(e["rows"]), e["status"], e.get("message", "")],
            )
    return len(entries)


def health(now: datetime | None = None) -> dict:
    """健康度汇总：每采集器的成功率/最后成功/当日缺口。"""
    now = now or datetime.now()
    today = now.date()
    with reader() as con:
        _ensure(con)
        rows = con.execute(
            "SELECT job, trade_date, rows, status, finished_at FROM collect_log "
            "WHERE trade_date >= ? ORDER BY job, trade_date",
            [today.replace(year=today.year - 1)],
        ).fetchall()
        registered = {r["name"]: r for r in _collector_metas(con)}

    per: dict[str, list] = {}
    for job, td, n, st, fin in rows:
        per.setdefault(job, []).append(
            {"trade_date": td, "rows": n, "status": st, "finished_at": fin})

    out = []
    for job, meta in registered.items():
        logs = per.get(job, [])
        ok = [x for x in logs if x["status"] == "ok"]
        last30 = [x for x in logs][-30:]
        rate = (sum(1 for x in last30 if x["status"] == "ok") / len(last30)) if last30 else None
        last_ok = max((x["trade_date"] for x in ok), default=None)

        # 当日缺口：采集时点已过 + 今日没有 ok 记录
        deadline_hour = _EXPECTED_BY.get(meta.get("schedule", "close"), 16)
        gap = (last_ok is None or last_ok < today) and now.hour >= deadline_hour
        out.append({
            "job": job, "label": meta.get("label", job),
            "schedule": meta.get("schedule"),
            "runs_30d": len(last30),
            "success_rate": round(rate, 3) if rate is not None else None,
            "last_success": str(last_ok) if last_ok else None,
            "last_rows": (ok[-1]["rows"] if ok else None),
            "today_gap": gap,
        })
    out.sort(key=lambda x: (not x["today_gap"], x["job"]))
    return {"checked_at": now.isoformat(timespec="seconds"),
            "jobs": out,
            "any_gap": any(j["today_gap"] for j in out)}


def _collector_metas(con) -> list[dict]:
    """采集器注册表快照（避免 reader 里 import 采集器触发网络模块链）。"""
    from lquant.market.collectors import list_collectors

    return list_collectors()
