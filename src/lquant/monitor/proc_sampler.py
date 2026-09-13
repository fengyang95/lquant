"""进程自采样：cpu/mem（psutil，缺失降级 None）→ Redis SETEX 15s。"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from lquant.monitor.types import ProcSample

_LOG = logging.getLogger(__name__)

SAMPLE_TTL_SEC = 15  # 正常采样周期 5s；TTL 3 倍容错

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - 环境无 psutil 时降级
    _psutil = None


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def _redis_ok() -> bool:
    """Redis 可达性检查：优先复用 jobs._redis_available，退化到客户端 ping。"""
    try:
        from lquant.server.jobs import _redis_available

        if _redis_available():
            return True
    except Exception:  # noqa: BLE001 - redis 包缺失等
        pass
    try:
        return bool(_get_redis().ping())
    except Exception:  # noqa: BLE001 - 连不上即不可用
        return False


def sample_once(name: str, pid: int | None = None,
                current_job_fn=None) -> ProcSample:
    import os

    from lquant.monitor.types import ProcSample

    cpu = mem = None
    if _psutil is not None:
        try:
            proc = _psutil.Process(pid) if pid else _psutil.Process()
            cpu = proc.cpu_percent()  # 进程自采样，非进程树
            mem = proc.memory_info().rss / (1024 * 1024)
        except Exception:  # noqa: BLE001
            _LOG.warning("psutil 采样失败", exc_info=True)
    cur = None
    if current_job_fn is not None:
        try:
            cur = current_job_fn()
        except Exception:  # noqa: BLE001
            cur = None
    return ProcSample(ts=time.time(), proc_name=name, pid=pid or os.getpid(),
                      cpu_pct=cpu, mem_rss_mb=mem, current_job=cur)


def build_payload(s: ProcSample) -> dict:
    return {"ts": s.ts, "proc_name": s.proc_name, "pid": s.pid,
            "cpu_pct": s.cpu_pct, "mem_rss_mb": s.mem_rss_mb,
            "current_job": s.current_job}


def _push(r, s: ProcSample, ttl: int = SAMPLE_TTL_SEC) -> None:
    try:
        r.setex(f"lquant:monitor:proc:{s.proc_name}", ttl,
                json.dumps(build_payload(s)))
    except Exception:  # noqa: BLE001 - 推送失败只 log，不影响调用方
        _LOG.warning("proc 样本推送失败", exc_info=True)


def _loop(name: str, stop: threading.Event, interval: float,
          current_job_fn) -> None:
    """单周期采样推送（flusher/测试可直接调用一次）。"""
    try:
        if _redis_ok():
            _push(_get_redis(), sample_once(name, current_job_fn=current_job_fn))
    except Exception:  # noqa: BLE001 - 采集失败只 log
        _LOG.warning("proc 样本推送失败", exc_info=True)


_STOP_FLAGS: dict[str, threading.Event] = {}


def start_sampler(name: str, current_job_fn=None,
                  interval: float | None = None) -> threading.Thread | None:
    """启动 daemon 采样线程；Redis 不可用返回 None（不空转）。"""
    from lquant.core.config import get_settings

    iv = interval if interval is not None else \
        get_settings().monitor_sample_interval_sec
    if not _redis_ok():
        return None
    stop = threading.Event()
    _STOP_FLAGS[name] = stop

    def _run():
        while not stop.wait(iv):
            _loop(name, stop, iv, current_job_fn)

    t = threading.Thread(target=_run, name=f"monitor-sampler-{name}", daemon=True)
    t.start()
    return t


def stop_sampler() -> None:
    for ev in _STOP_FLAGS.values():
        ev.set()
    _STOP_FLAGS.clear()
