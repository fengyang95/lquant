"""监控记录类型：不可变（frozen dataclass）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiMetricPoint:
    ts: float                 # 采集时刻 epoch 秒
    route: str                # FastAPI 路由模板；无匹配 'unmatched'
    method: str
    status: int
    duration_ms: float
    dur_category: str         # fast / normal / slow / error


@dataclass(frozen=True)
class ApiErrorPoint:
    ts: float                 # 采集时刻 epoch 秒
    route: str
    method: str
    status: int
    error_type: str | None    # 异常类名；HTTP 5xx 响应（无异常对象）时为 None
    message: str | None       # 异常消息 / HTTP detail
    traceback_tail: str | None  # 堆栈尾段（截断），响应路径无堆栈


@dataclass(frozen=True)
class ProcSample:
    ts: float
    proc_name: str            # http / general-0 / backtest-0..3
    pid: int
    cpu_pct: float | None     # psutil 缺失时 None
    mem_rss_mb: float | None
    current_job: str | None


@dataclass(frozen=True)
class TaskEvent:
    event_ts: float
    job_id: str
    job_name: str
    event: str                # started / finished / failed
    queue: str | None
    enqueued_at: float | None
    started_at: float | None
    finished_at: float | None
    elapsed_ms: float | None
    queue_delay_ms: float | None
    message: str | None
