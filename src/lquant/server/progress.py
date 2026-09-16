"""任务进度注册表：流式进度条的数据源。

本地降级模式：进程内 dict（容量兜底 + 淘汰）；Redis 模式下任务在 worker 进程
执行，本地 dict 看不到 —— set 时若 Redis 可用同步写一份 Redis key
（`lquant:progress:{job_id}`，TTL 1h），get 优先读 Redis。REST 列表与
WS 流共用同一数据源，协议见 ws.py。
"""
from __future__ import annotations

import threading
import time
from typing import Any

_PROGRESS_MAX = 500
_PROGRESS_TTL = 3600  # Redis key TTL（秒）

_PROGRESS: dict[str, dict[str, Any]] = {}
_NAMES: dict[str, str] = {}
_LOCK = threading.Lock()


def _redis_available() -> bool:
    from lquant.server.jobs import _redis_available as fn

    return fn()


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def set_progress(job_id: str, *, done: int | None = None, total: int | None = None,
                 phase: str = "", message: str | None = None) -> None:
    """更新任务进度。字段可部分更新：None 的字段保留旧值，首次写入补零。"""
    with _LOCK:
        old = _PROGRESS.get(job_id, {})
        cur = {
            "done": done if done is not None else old.get("done", 0),
            "total": total if total is not None else old.get("total", 0),
            "phase": phase or old.get("phase", ""),
            "message": message if message is not None else old.get("message"),
            "ts": time.time(),
        }
        # 容量兜底：先淘汰已结束的旧条目，再按插入序淘汰最旧
        if job_id not in _PROGRESS and len(_PROGRESS) >= _PROGRESS_MAX:
            for jid in list(_PROGRESS):
                del _PROGRESS[jid]
                if len(_PROGRESS) < _PROGRESS_MAX:
                    break
        _PROGRESS[job_id] = cur
    if _redis_available():
        try:
            import json

            r = _get_redis()
            r.set(f"lquant:progress:{job_id}", json.dumps(cur, ensure_ascii=False),
                  ex=_PROGRESS_TTL)
        except Exception:  # noqa: BLE001 - Redis 抖动不炸任务体
            pass


def get_progress(job_id: str) -> dict[str, Any] | None:
    """读进度。Redis 可用优先 Redis（worker 进程写入），否则进程内注册表。"""
    if _redis_available():
        try:
            import json

            raw = _get_redis().get(f"lquant:progress:{job_id}")
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        p = _PROGRESS.get(job_id)
        return dict(p) if p else None


def set_job_name(job_id: str, name: str) -> None:
    """登记任务显示名（enqueue 时调用；Redis 模式写 Redis hash，TTL 1 天）。"""
    with _LOCK:
        if len(_NAMES) >= _PROGRESS_MAX:
            _NAMES.pop(next(iter(_NAMES)), None)
        _NAMES[job_id] = name
    if _redis_available():
        try:
            r = _get_redis()
            r.hset("lquant:job:names", job_id, name)
            r.expire("lquant:job:names", 86400)
        except Exception:  # noqa: BLE001
            pass


def get_job_name(job_id: str) -> str | None:
    with _LOCK:
        n = _NAMES.get(job_id)
    if n is not None:
        return n
    if _redis_available():
        try:
            v = _get_redis().hget("lquant:job:names", job_id)
            if v:
                return v if isinstance(v, str) else v.decode()
        except Exception:  # noqa: BLE001
            pass
    return None
