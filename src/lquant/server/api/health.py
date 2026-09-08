"""健康检查：进程存活 + 依赖状态 + 数据覆盖度。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"pong": "health"}


@router.get("")
def health() -> dict:
    """一键启动脚本的健康检查就打这个端点。"""
    out: dict = {"status": "ok", "ts": date.today().isoformat()}
    out["redis"] = _redis_status()
    out["rust"] = _rust_status()
    out["data"] = _data_coverage()
    return out


def _redis_status() -> dict:
    from lquant.server.jobs import _redis_available

    ok = _redis_available()
    return {"available": ok, "mode": "rq" if ok else "local-thread(降级)"}


def _rust_status() -> dict:
    try:
        from lquant._rust.loader import status

        return {"available": True, **status()}
    except Exception:  # noqa: BLE001
        return {"available": False, "mode": "python-fallback"}


def _data_coverage() -> dict:
    """数据覆盖度：地基（日历/标的）缺了要一眼看出来。"""
    try:
        from lquant.core.db import reader

        with reader() as con:
            sec = con.execute("SELECT count(*) FROM security").fetchone()[0]
            cal = con.execute(
                "SELECT count(*) FROM trade_calendar WHERE is_open").fetchone()[0]
            cal_max = con.execute(
                "SELECT max(trade_date) FROM trade_calendar").fetchone()[0]
        return {"securities": int(sec), "calendar_open_days": int(cal),
                "calendar_until": str(cal_max) if cal_max else None}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "hint": "先跑 `lq data reference`"}
