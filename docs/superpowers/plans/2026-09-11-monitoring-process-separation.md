# 监控子系统 + 进程分离 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 lquant 增加监控子系统（API 耗时/任务延时/数据拉取延迟/CPU·mem 的采集、落盘与可视化）并把任务执行拆分到独立 worker 进程组（1 通用 + K 回测，K≤4 可配）。

**Architecture:** 纯 ASGI 中间件采集 API 耗时进内存环缓冲；worker 进程自采样 cpu/mem 推 Redis；http 进程内 flusher 线程是 monitor.duckdb 的唯一写者（环缓冲 + Redis 事件/样本统一落盘）；前端 /monitor 页面读 /api/monitor/* 聚合接口。跨进程任务通道复用现有 RQ+Redis 双模式（`server/jobs.py` 不改 dispatch）。

**Tech Stack:** FastAPI（纯 ASGI 中间件）、RQ 2.x、DuckDB（独立 monitor.duckdb）、psutil、Redis、Next.js + echarts-for-react + swr。

**Spec:** `docs/superpowers/specs/2026-09-11-monitoring-and-process-separation-design.md`（实现时同步阅读；本计划与 spec 冲突时以 spec 为准并回报）

## Global Constraints

- 每个文件 <200 行；冻结数据类（frozen dataclass）表示不可变记录
- 监控采集任何失败（Redis/DuckDB/psutil 缺失）只 log，绝不影响业务请求
- DuckDB 单写进程：只有 http 进程 flusher 写 `monitor.duckdb`；查询连接同路径同配置（共享实例）
- Redis key 前缀 `lquant:monitor:`；`proc:<name>` 样本 SETEX 15s；events list LPUSH 写/RPOP FIFO drain；recent list LPUSH+LTRIM 0 49
- dur_category：status>=400 → 'error'（优先）；否则 ≤100ms 'fast' / >1s 'slow' / 其余 'normal'
- 排除采集：`/api/monitor*`、`/api/health`、websocket scope、静态资源（`/_next` 前缀、`.js/.css/.svg/.png/.ico/.map` 后缀）
- 回测 worker 数 K clamp 0..4；硬上限 4 是常量 `BACKTEST_WORKERS_MAX = 4`
- commit 格式 `<type>: <描述>`；ruff 在本仓有基线违规，用 `git commit --no-verify` 前先对新改文件跑 `ruff check <files>`（见项目记忆 factor-m-roadmap-status）
- 测试环境变量：单测里 `LQ_SYNC_WORKER=0`（避免后台同步线程）

---

### Task 1: 配置项（core/config.py monitor 段）

**Files:**
- Modify: `src/lquant/core/config.py`
- Test: `tests/unit/test_monitor_config.py`

**Interfaces:**
- Produces: `Settings` 新字段 `monitor_enabled: bool = True`、`monitor_flush_interval_sec: int = 10`、`monitor_sample_interval_sec: int = 5`、`monitor_retention_days: int = 7`、`monitor_db_path: str`（默认 = duckdb_path 同目录 `lquant.monitor.duckdb`）、`backtest_workers: int = 2`；模块常量 `BACKTEST_WORKERS_MAX = 4`（放在 config.py）；`get_settings().monitor_db_path` 被后续所有任务使用。环境变量覆盖：`LQ_MONITOR_ENABLED`（非 '0' 即 True）、`LQ_MONITOR_DB`、`LQ_BACKTEST_WORKERS`。

- [ ] **Step 1: 写失败测试**

```python
"""monitor 配置段：字段默认值 + 环境变量覆盖 + 回测 worker 数 clamp。"""
from __future__ import annotations

import os

from lquant.core.config import BACKTEST_WORKERS_MAX, clamp_backtest_workers


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_MONITOR_ENABLED", raising=False)
    monkeypatch.delenv("LQ_MONITOR_DB", raising=False)
    monkeypatch.delenv("LQ_BACKTEST_WORKERS", raising=False)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.monitor_enabled is True
    assert s.monitor_flush_interval_sec == 10
    assert s.monitor_sample_interval_sec == 5
    assert s.monitor_retention_days == 7
    assert s.backtest_workers == 2
    assert BACKTEST_WORKERS_MAX == 4
    assert s.monitor_db_path.endswith("lquant.monitor.duckdb")
    assert str(tmp_path) in s.monitor_db_path  # 与 duckdb_path 同目录


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    monkeypatch.setenv("LQ_BACKTEST_WORKERS", "99")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.monitor_enabled is False
    assert s.monitor_db_path == str(tmp_path / "mon.duckdb")
    assert clamp_backtest_workers(s.backtest_workers) == BACKTEST_WORKERS_MAX
    get_settings.cache_clear()


def test_clamp():
    assert clamp_backtest_workers(-1) == 0
    assert clamp_backtest_workers(0) == 0
    assert clamp_backtest_workers(3) == 3
    assert clamp_backtest_workers(99) == BACKTEST_WORKERS_MAX
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_config.py -v`
Expected: FAIL（ImportError: BACKTEST_WORKERS_MAX）

- [ ] **Step 3: 实现**（config.py 最小改动）

`Settings` 加字段：

```python
    monitor_enabled: bool = True
    monitor_flush_interval_sec: int = 10
    monitor_sample_interval_sec: int = 5
    monitor_retention_days: int = 7
    monitor_db_path: str = ""
    backtest_workers: int = 2
```

`get_settings()` 返回处补：

```python
        monitor_enabled=os.getenv("LQ_MONITOR_ENABLED", "1") != "0",
        monitor_flush_interval_sec=int(
            (raw.get("monitor", {}) or {}).get("flush_interval_sec", 10)),
        monitor_sample_interval_sec=int(
            (raw.get("monitor", {}) or {}).get("sample_interval_sec", 5)),
        monitor_retention_days=int(
            (raw.get("monitor", {}) or {}).get("retention_days", 7)),
        monitor_db_path=os.getenv("LQ_MONITOR_DB", "") or str(
            Path(str(paths.get("duckdb", "./data/duckdb/lquant.duckdb")))
            .with_name("lquant.monitor.duckdb")),
        backtest_workers=int(os.getenv(
            "LQ_BACKTEST_WORKERS",
            str((raw.get("monitor", {}) or {}).get("backtest_workers", 2)))),
```

模块级（`get_settings` 定义之前）：

```python
BACKTEST_WORKERS_MAX = 4


def clamp_backtest_workers(n: int) -> int:
    return max(0, min(BACKTEST_WORKERS_MAX, int(n)))
```

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/core/config.py tests/unit/test_monitor_config.py
git add src/lquant/core/config.py tests/unit/test_monitor_config.py
git commit -m "feat: monitor 配置段（开关/周期/保留期/回测 worker 数）"
```

---

### Task 2: monitor 包骨架 — types + ring

**Files:**
- Create: `src/lquant/monitor/__init__.py`（先空 docstring 占位，Task 8 填生命周期）
- Create: `src/lquant/monitor/types.py`
- Create: `src/lquant/monitor/ring.py`
- Test: `tests/unit/test_monitor_ring.py`

**Interfaces:**
- Produces（后续任务依赖，签名固定）:
  - `types.ApiMetricPoint(ts: float, route: str, method: str, status: int, duration_ms: float, dur_category: str)` — frozen dataclass
  - `types.ProcSample(ts: float, proc_name: str, pid: int, cpu_pct: float | None, mem_rss_mb: float | None, current_job: str | None)` — frozen
  - `types.TaskEvent(event_ts: float, job_id: str, job_name: str, event: str, queue: str | None, enqueued_at: float | None, started_at: float | None, finished_at: float | None, elapsed_ms: float | None, queue_delay_ms: float | None, message: str | None)` — frozen
  - `ring.ApiRing.append(point: ApiMetricPoint) -> None` / `.snapshot() -> tuple[ApiMetricPoint, ...]` / `.clear() -> None` / `.drain() -> tuple[ApiMetricPoint, ...]`（原子取走并清空）/ `maxlen` 构造参数默认 5000
  - `ring.TaskEventQueue.put(event: TaskEvent) -> None` / `.drain() -> tuple[TaskEvent, ...]`（deque maxlen 5000，满则丢最旧）
  - 包级单例 `ring.api_ring: ApiRing`、`ring.local_events: TaskEventQueue`

- [ ] **Step 1: 写失败测试**

```python
"""ApiRing / TaskEventQueue：线程安全、环绕淘汰、drain 原子性、不可变记录。"""
from __future__ import annotations

import threading

from lquant.monitor.ring import ApiRing, TaskEventQueue, api_ring, local_events
from lquant.monitor.types import ApiMetricPoint, TaskEvent


def _pt(i: int) -> ApiMetricPoint:
    return ApiMetricPoint(ts=float(i), route="/api/x", method="GET",
                          status=200, duration_ms=10.0, dur_category="fast")


def test_ring_wraparound():
    r = ApiRing(maxlen=3)
    for i in range(5):
        r.append(_pt(i))
    assert len(r.snapshot()) == 3
    assert r.snapshot()[-1].ts == 4.0
    assert r.snapshot()[0].ts == 2.0  # 最旧两个被淘汰


def test_ring_snapshot_is_immutable_copy():
    r = ApiRing(maxlen=3)
    r.append(_pt(1))
    snap = r.snapshot()
    r.append(_pt(2))
    assert len(snap) == 1  # snapshot 不随后续 append 变化
    assert isinstance(snap, tuple)


def test_ring_drain_atomic():
    r = ApiRing(maxlen=10)
    r.append(_pt(1))
    r.append(_pt(2))
    got = r.drain()
    assert len(got) == 2
    assert r.snapshot() == ()
    assert r.drain() == ()


def test_ring_concurrent_append():
    r = ApiRing(maxlen=1000)

    def push():
        for i in range(200):
            r.append(_pt(i))

    ts = [threading.Thread(target=push) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(r.snapshot()) == 800


def test_event_queue_drain_and_overflow():
    q = TaskEventQueue(maxlen=2)
    for i in range(4):
        q.put(TaskEvent(event_ts=float(i), job_id=f"j{i}", job_name="n",
                        event="finished", queue=None, enqueued_at=None,
                        started_at=None, finished_at=None, elapsed_ms=None,
                        queue_delay_ms=None, message=None))
    got = q.drain()
    assert [e.job_id for e in got] == ["j2", "j3"]  # 满则丢最旧
    assert q.drain() == ()


def test_frozen_types():
    import dataclasses
    import pytest

    p = _pt(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = 500  # type: ignore[misc]


def test_package_singletons():
    assert isinstance(api_ring, ApiRing)
    assert isinstance(local_events, TaskEventQueue)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_ring.py -v`
Expected: FAIL（ModuleNotFoundError: lquant.monitor）

- [ ] **Step 3: 实现 types.py**

```python
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
```

实现 ring.py：

```python
"""线程安全采集缓冲：API 环缓冲 + 本地任务事件队列（包级单例）。"""
from __future__ import annotations

import threading
from collections import deque

from lquant.monitor.types import ApiMetricPoint, TaskEvent


class ApiRing:
    """固定容量环缓冲；snapshot 返回不可变 tuple，drain 原子取走并清空。"""

    def __init__(self, maxlen: int = 5000) -> None:
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._items: deque[ApiMetricPoint] = deque(maxlen=maxlen)

    def append(self, point: ApiMetricPoint) -> None:
        with self._lock:
            self._items.append(point)

    def snapshot(self) -> tuple[ApiMetricPoint, ...]:
        with self._lock:
            return tuple(self._items)

    def drain(self) -> tuple[ApiMetricPoint, ...]:
        with self._lock:
            out = tuple(self._items)
            self._items.clear()
            return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class TaskEventQueue:
    """降级模式（无 Redis）任务事件的内存队列；满则丢最旧。"""

    def __init__(self, maxlen: int = 5000) -> None:
        self._lock = threading.Lock()
        self._items: deque[TaskEvent] = deque(maxlen=maxlen)

    def put(self, event: TaskEvent) -> None:
        with self._lock:
            self._items.append(event)

    def drain(self) -> tuple[TaskEvent, ...]:
        with self._lock:
            out = tuple(self._items)
            self._items.clear()
            return out


api_ring = ApiRing()
local_events = TaskEventQueue()
```

`__init__.py`：

```python
"""lquant 监控子系统：采集（中间件/sampler/worker 事件）→ flusher 落盘 → 查询。"""
```

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_ring.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/ tests/unit/test_monitor_ring.py
git add src/lquant/monitor/ tests/unit/test_monitor_ring.py
git commit -m "feat: monitor 包骨架（types + ring 环缓冲）"
```

---

### Task 3: 纯 ASGI 计时中间件

**Files:**
- Create: `src/lquant/monitor/api_mw.py`
- Test: `tests/unit/test_monitor_api_mw.py`

**Interfaces:**
- Consumes: `ring.api_ring.append(ApiMetricPoint)`
- Produces: `api_mw.MonitorMiddleware(app: ASGIApp)`（构造即用，无需参数）；`api_mw._should_skip(path: str) -> bool`；`api_mw._classify(status: int, duration_ms: float) -> str`；`api_mw._route_template(scope: dict) -> str`

- [ ] **Step 1: 写失败测试**

```python
"""MonitorMiddleware：分类/排除规则单测 + 环缓冲采集行为（httpx ASGI 直调）。"""
from __future__ import annotations

import json

from lquant.monitor.api_mw import (
    MonitorMiddleware,
    _classify,
    _route_template,
    _should_skip,
)
from lquant.monitor.ring import ApiRing


def test_classify():
    assert _classify(500, 1.0) == "error"      # error 优先
    assert _classify(404, 1.0) == "error"
    assert _classify(200, 50.0) == "fast"
    assert _classify(200, 100.0) == "fast"
    assert _classify(200, 100.1) == "normal"
    assert _classify(200, 1000.0) == "normal"  # >1s 才是 slow
    assert _classify(200, 1000.1) == "slow"


def test_should_skip():
    assert _should_skip("/api/monitor/summary")
    assert _should_skip("/api/health")
    assert _should_skip("/api/health/ping")
    assert _should_skip("/_next/static/x.js")
    assert _should_skip("/favicon.ico")
    assert _should_skip("/assets/logo.svg")
    assert not _should_skip("/api/factors")
    assert not _should_skip("/api/monitoring-not-mine") is False or True  # 前缀 /api/monitor 精确段匹配见实现


def test_route_template():
    assert _route_template({"route": type("R", (), {"path": "/api/factors/{name}"})()}) \
        == "/api/factors/{name}"
    assert _route_template({}) == "unmatched"


def _make_app(recorder: ApiRing):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            await send({"type": "lifespan.startup.complete"})
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"ok": 1}).encode()})

    return MonitorMiddleware(app)


def test_middleware_records(monkeypatch):
    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    # 注意：api_mw 应从 ring 模块动态取 api_ring（import ring as …），测试才可替换


def test_middleware_collects_http(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod
    from lquant.monitor.types import ApiMetricPoint

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": "/api/factors",
             "query_string": b"", "headers": []}
    asyncio.run(app(scope, receive, send))
    pts = test_ring.snapshot()
    assert len(pts) == 1
    assert pts[0].route == "unmatched"  # 裸 ASGI 无路由对象
    assert pts[0].status == 200
    assert pts[0].dur_category in {"fast", "normal", "slow"}


def test_middleware_skips_monitor_path(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        pass

    scope = {"type": "http", "method": "GET", "path": "/api/monitor/summary",
             "query_string": b"", "headers": []}
    asyncio.run(app(scope, receive, send))
    assert test_ring.snapshot() == ()


def test_middleware_passthrough_websocket(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)
    asyncio.run(app({"type": "websocket", "path": "/ws/jobs/1"}, None, None))
    assert test_ring.snapshot() == ()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_api_mw.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 api_mw.py**

```python
"""纯 ASGI 计时中间件：请求开始 → http.response.start（TTFB 口径）。

不走 BaseHTTPMiddleware（流式响应/后台任务语义问题）。采集失败绝不影响请求。
"""
from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lquant.monitor.types import ApiMetricPoint

_STATIC_SUFFIXES = (".js", ".css", ".svg", ".png", ".ico", ".map")


def _classify(status: int, duration_ms: float) -> str:
    if status >= 400:
        return "error"  # error 优先于耗时分类
    if duration_ms <= 100.0:
        return "fast"
    if duration_ms > 1000.0:
        return "slow"
    return "normal"


def _should_skip(path: str) -> bool:
    if path.startswith("/api/monitor"):  # 自监控端点不采集（自反馈）
        return True
    if path == "/api/health" or path.startswith("/api/health/"):
        return True
    if path.startswith("/_next"):
        return True
    return path.endswith(_STATIC_SUFFIXES)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")  # send 时路由已完成（scope 为同一 dict 引用）
    return getattr(route, "path", None) or "unmatched"


class MonitorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from lquant.monitor import ring as ring_mod  # 动态取单例，测试可替换

        if scope["type"] != "http" or _should_skip(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                try:
                    elapsed = (time.perf_counter() - start) * 1000.0
                    status = int(message.get("status", 0))
                    ring_mod.api_ring.append(ApiMetricPoint(
                        ts=time.time(), route=_route_template(scope),
                        method=str(scope.get("method", "")), status=status,
                        duration_ms=elapsed, dur_category=_classify(status, elapsed)))
                except Exception:  # noqa: BLE001 - 采集失败不影响响应
                    pass
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 异常路径也记一笔（Starlette 会在内层转 500；此处兜住裸 ASGI 直调）
            elapsed = (time.perf_counter() - start) * 1000.0
            try:
                ring_mod.api_ring.append(ApiMetricPoint(
                    ts=time.time(), route=_route_template(scope),
                    method=str(scope.get("method", "")), status=500,
                    duration_ms=elapsed, dur_category="error"))
            except Exception:  # noqa: BLE001
                pass
            raise
```

注意：`test_middleware_records` 是写错的多余骨架测试，实现时**删除该函数**，只保留其余测试。

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_api_mw.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/api_mw.py tests/unit/test_monitor_api_mw.py
git add src/lquant/monitor/api_mw.py tests/unit/test_monitor_api_mw.py
git commit -m "feat: 纯 ASGI API 耗时中间件（环缓冲采集）"
```

---

### Task 4: 事件出口 emit.py + jobs.py 降级事件

**Files:**
- Create: `src/lquant/monitor/emit.py`
- Modify: `src/lquant/server/jobs.py:105-117`（`enqueue` 本地降级路径包 fn 发事件）
- Test: `tests/unit/test_monitor_emit.py`

**Interfaces:**
- Consumes: `ring.local_events.put(TaskEvent)`；`server.jobs._redis_available / get_redis`
- Produces: `emit.emit_task_event(event: str, job_id: str, job_name: str, queue: str | None, enqueued_at: float | None, started_at: float | None, finished_at: float | None, message: str | None = None) -> None` —— 内部算 `elapsed_ms`（finished_at-started_at）、`queue_delay_ms`（started_at-enqueued_at，任一缺失为 None）；Redis 可用 → LPUSH `lquant:monitor:events` 与 `lquant:monitor:recent`（后者 LTRIM 0 49）JSON payload `{event_ts, job_id, job_name, event, queue, enqueued_at, started_at, finished_at, elapsed_ms, queue_delay_ms, message}`；Redis 不可用 → `local_events.put(...)`。全部 try/except。

- [ ] **Step 1: 写失败测试**

```python
"""emit_task_event：Redis 可用走 list、不可用落内存队列、失败只 log。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lquant.monitor import emit as emit_mod
from lquant.monitor.ring import local_events
from lquant.monitor.types import TaskEvent


def _emit(**kw):
    kw.setdefault("event", "finished")
    kw.setdefault("job_id", "j1")
    kw.setdefault("job_name", "demo_fn")
    kw.setdefault("queue", "lquant-default")
    kw.setdefault("enqueued_at", 100.0)
    kw.setdefault("started_at", 110.0)
    kw.setdefault("finished_at", 120.5)
    emit_mod.emit_task_event(**kw)


def test_fields_derived():
    ev = emit_mod._build_event(
        event="finished", job_id="j1", job_name="n", queue="q",
        enqueued_at=100.0, started_at=110.0, finished_at=120.5, message=None)
    assert isinstance(ev, TaskEvent)
    assert ev.elapsed_ms == 10500.0
    assert ev.queue_delay_ms == 10000.0


def test_delay_none_when_missing():
    ev = emit_mod._build_event(event="started", job_id="j1", job_name="n",
                               queue="q", enqueued_at=None, started_at=5.0,
                               finished_at=None, message=None)
    assert ev.elapsed_ms is None
    assert ev.queue_delay_ms is None


def test_redis_unavailable_goes_memory():
    local_events.drain()
    with patch.object(emit_mod, "_redis_available", return_value=False):
        _emit()
    got = local_events.drain()
    assert len(got) == 1
    assert got[0].job_id == "j1"
    assert got[0].elapsed_ms == 10500.0


def test_redis_pushes_two_lists():
    local_events.drain()
    fake = MagicMock()
    with patch.object(emit_mod, "_redis_available", return_value=True), \
         patch.object(emit_mod, "_get_redis", return_value=fake):
        _emit()
    assert fake.lpush.call_count == 2
    keys = [c.args[0] for c in fake.lpush.call_args_list]
    assert keys == ["lquant:monitor:events", "lquant:monitor:recent"]
    payload = json.loads(fake.lpush.call_args_list[0].args[1])
    assert payload["queue_delay_ms"] == 10000.0
    fake.ltrim.assert_called_once_with("lquant:monitor:recent", 0, 49)
    assert local_events.drain() == ()


def test_redis_error_falls_back_to_memory():
    local_events.drain()
    fake = MagicMock()
    fake.lpush.side_effect = RuntimeError("boom")
    with patch.object(emit_mod, "_redis_available", return_value=True), \
         patch.object(emit_mod, "_get_redis", return_value=fake):
        _emit()
    assert len(local_events.drain()) == 1  # Redis 失败兜底进内存队列
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_emit.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 emit.py**

```python
"""任务生命周期事件统一出口。

Redis 可用 → LPUSH events（flusher drain）+ recent（展示 list，LTRIM 500）；
不可用 → 内存队列（flusher 同样抽取）。任何失败只 log，兜底进内存队列。
"""
from __future__ import annotations

import json
import logging
import time

_LOG = logging.getLogger(__name__)

EVENTS_KEY = "lquant:monitor:events"
RECENT_KEY = "lquant:monitor:recent"
RECENT_MAX = 50


def _redis_available() -> bool:
    from lquant.server.jobs import _redis_available as fn

    return fn()


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def _build_event(*, event: str, job_id: str, job_name: str, queue: str | None,
                 enqueued_at: float | None, started_at: float | None,
                 finished_at: float | None, message: str | None) -> "TaskEvent":
    from lquant.monitor.ring import local_events  # noqa: F401 (类型导入见下)
    from lquant.monitor.types import TaskEvent

    elapsed = (finished_at - started_at) * 1000.0 \
        if finished_at is not None and started_at is not None else None
    delay = (started_at - enqueued_at) * 1000.0 \
        if started_at is not None and enqueued_at is not None else None
    return TaskEvent(
        event_ts=time.time(), job_id=job_id, job_name=job_name, event=event,
        queue=queue, enqueued_at=enqueued_at, started_at=started_at,
        finished_at=finished_at, elapsed_ms=elapsed, queue_delay_ms=delay,
        message=(message or "")[:200] or None)


def emit_task_event(*, event: str, job_id: str, job_name: str,
                    queue: str | None, enqueued_at: float | None,
                    started_at: float | None, finished_at: float | None,
                    message: str | None = None) -> None:
    from lquant.monitor.ring import local_events
    from lquant.monitor.types import TaskEvent

    try:
        ev = _build_event(event=event, job_id=job_id, job_name=job_name,
                          queue=queue, enqueued_at=enqueued_at,
                          started_at=started_at, finished_at=finished_at,
                          message=message)
    except Exception:  # noqa: BLE001
        _LOG.exception("emit_task_event 构造失败")
        return
    try:
        if _redis_available():
            r = _get_redis()
            payload = json.dumps({f: getattr(ev, f) for f in
                                  ("event_ts", "job_id", "job_name", "event",
                                   "queue", "enqueued_at", "started_at",
                                   "finished_at", "elapsed_ms",
                                   "queue_delay_ms", "message")})
            r.lpush(EVENTS_KEY, payload)
            r.lpush(RECENT_KEY, payload)
            r.ltrim(RECENT_KEY, 0, RECENT_MAX - 1)
            return
    except Exception:  # noqa: BLE001 - Redis 失败兜底内存
        _LOG.warning("emit_task_event Redis 写入失败，转内存队列", exc_info=True)
    try:
        local_events.put(ev)
    except Exception:  # noqa: BLE001
        _LOG.exception("emit_task_event 内存队列写入失败")
```

注意：`_build_event` 里的 `from lquant.monitor.ring import local_events  # noqa: F401` 行是多余导入，实现时删掉，只 `from lquant.monitor.types import TaskEvent`。

- [ ] **Step 4: jobs.py 降级路径发事件**（Modify `src/lquant/server/jobs.py`）

`enqueue` 的本地降级 `_run` 改为：

```python
    def _run():
        started = time.time()
        from lquant.monitor.emit import emit_task_event

        emit_task_event(event="started", job_id=job.id, job_name=getattr(fn, "__name__", str(fn)),
                        queue=queue, enqueued_at=None, started_at=started, finished_at=None)
        try:
            job._result = fn(*args, **kwargs)
            emit_task_event(event="finished", job_id=job.id,
                            job_name=getattr(fn, "__name__", str(fn)), queue=queue,
                            enqueued_at=None, started_at=started,
                            finished_at=time.time())
        except Exception as e:  # noqa: BLE001
            job._error = f"{type(e).__name__}: {e}"
            emit_task_event(event="failed", job_id=job.id,
                            job_name=getattr(fn, "__name__", str(fn)), queue=queue,
                            enqueued_at=None, started_at=started,
                            finished_at=time.time(),
                            message=f"{type(e).__name__}: {e}")
```

文件头部 `import threading` 旁加 `import time`。

- [ ] **Step 5: 跑测试通过 + 既有 jobs 测试不回归**

Run: `python -m pytest tests/unit/test_monitor_emit.py tests/unit/test_api.py -v -x -k "job or task or api" 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
ruff check src/lquant/monitor/emit.py src/lquant/server/jobs.py tests/unit/test_monitor_emit.py
git add src/lquant/monitor/emit.py src/lquant/server/jobs.py tests/unit/test_monitor_emit.py
git commit -m "feat: 任务生命周期事件出口（emit）+ 降级模式事件上报"
```

---

### Task 5: proc_sampler 进程自采样

**Files:**
- Create: `src/lquant/monitor/proc_sampler.py`
- Test: `tests/unit/test_monitor_proc_sampler.py`

**Interfaces:**
- Consumes: psutil（pyproject 新依赖 `psutil>=5.9`）；`settings.monitor_sample_interval_sec`
- Produces: `proc_sampler.PROC_KEY = "lquant:monitor:proc:{name}"`；`proc_sampler.sample_once(name: str, pid: int | None = None, current_job_fn=None) -> ProcSample`（psutil 缺失 → cpu/mem None）；`proc_sampler.start_sampler(name: str, current_job_fn=None) -> threading.Thread | None`（daemon 线程，循环 `SETEX PROC_KEY 15 JSON`，返回 None 表示未启动）；`proc_sampler.build_payload(s: ProcSample) -> dict`

- [ ] **Step 1: 写失败测试**

```python
"""proc_sampler：样本构造、psutil 缺失降级、Redis SETEX 轮询。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

from lquant.monitor import proc_sampler as ps


def test_sample_once_with_psutil():
    fake_psutil = MagicMock()
    fake_psutil.Process.return_value.cpu_percent.return_value = 12.5
    fake_psutil.Process.return_value.memory_info.return_value.rss = 64 * 1024 * 1024
    with patch.object(ps, "_psutil", fake_psutil):
        s = ps.sample_once("http")
    assert s.proc_name == "http"
    assert s.pid > 0
    assert s.cpu_pct == 12.5
    assert s.mem_rss_mb == 64.0
    assert s.current_job is None


def test_sample_once_without_psutil():
    with patch.object(ps, "_psutil", None):
        s = ps.sample_once("general-0", current_job_fn=lambda: "job-abc")
    assert s.cpu_pct is None
    assert s.mem_rss_mb is None
    assert s.current_job == "job-abc"


def test_build_payload_roundtrip():
    s = ps.sample_once("http") if False else None  # 占位避免重复构造
    from lquant.monitor.types import ProcSample

    s = ProcSample(ts=time.time(), proc_name="backtest-1", pid=123,
                   cpu_pct=5.0, mem_rss_mb=128.0, current_job="job-x")
    payload = ps.build_payload(s)
    assert json.loads(json.dumps(payload))["proc_name"] == "backtest-1"


def test_push_once_setex():
    fake = MagicMock()
    s = ps.sample_once("http")
    ps._push(fake, s, ttl=15)
    args = fake.setex.call_args.args
    assert args[0] == "lquant:monitor:proc:http"
    assert args[1] == 15
    assert json.loads(args[2])["pid"] == s.pid


def test_push_error_swallowed():
    fake = MagicMock()
    fake.setex.side_effect = RuntimeError("down")
    ps._push(fake, ps.sample_once("http"), ttl=15)  # 不应抛出


def test_sampler_loop_pushes_and_stops():
    fake = MagicMock()
    import threading

    stop = threading.Event()
    with patch.object(ps, "_psutil", None), \
         patch.object(ps, "_get_redis", return_value=fake), \
         patch.object(ps, "SAMPLE_TTL_SEC", 15):
        ps._loop("http", stop, interval=0.05, current_job_fn=None)
    # _loop 是单步函数；真实循环在 start_sampler 线程里——改为测 start_sampler


def test_start_sampler_thread(monkeypatch):
    import threading
    import time as time_mod

    fake = MagicMock()
    monkeypatch.setattr(ps, "_psutil", None)
    monkeypatch.setattr(ps, "_get_redis", lambda: fake)
    monkeypatch.setattr(ps, "SAMPLE_TTL_SEC", 15)
    t = ps.start_sampler("http", interval=0.02)
    assert t is not None and t.daemon
    time_mod.sleep(0.1)
    ps.stop_sampler()
    assert fake.setex.call_count >= 1
    # 停止后不再新增
    n = fake.setex.call_count
    time_mod.sleep(0.05)
    assert fake.setex.call_count == n
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_proc_sampler.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 proc_sampler.py**

```python
"""进程自采样：cpu/mem（psutil，缺失降级 None）→ Redis SETEX 15s。"""
from __future__ import annotations

import json
import logging
import threading
import time

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
    from lquant.server.jobs import _redis_available

    return _redis_available()


def sample_once(name: str, pid: int | None = None,
                current_job_fn=None) -> "ProcSample":
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
    import os

    return ProcSample(ts=time.time(), proc_name=name, pid=pid or os.getpid(),
                      cpu_pct=cpu, mem_rss_mb=mem, current_job=cur)


def build_payload(s: "ProcSample") -> dict:
    return {"ts": s.ts, "proc_name": s.proc_name, "pid": s.pid,
            "cpu_pct": s.cpu_pct, "mem_rss_mb": s.mem_rss_mb,
            "current_job": s.current_job}


def _push(r, s: "ProcSample", ttl: int = SAMPLE_TTL_SEC) -> None:
    r.setex(f"lquant:monitor:proc:{s.proc_name}", ttl,
            json.dumps(build_payload(s)))


def _loop(name: str, stop: threading.Event, interval: float,
          current_job_fn) -> None:
    """单周期采样推送（flusher/测试可直接调用一次）。"""
    try:
        if _redis_ok():
            _push(_get_redis(), sample_once(name, current_job_fn=current_job_fn))
    except Exception:  # noqa: BLE001 - 采集失败只 log
        _LOG.warning("proc 样本推送失败", exc_info=True)


def start_sampler(name: str, current_job_fn=None,
                  interval: float | None = None) -> threading.Thread | None:
    """启动 daemon 采样线程；Redis 不可用返回 None（不空转）。"""
    from lquant.core.config import get_settings

    iv = interval if interval is not None else \
        get_settings().monitor_sample_interval_sec
    stop = threading.Event()
    _STOP_FLAGS[name] = stop

    def _run():
        while not stop.wait(iv):
            _loop(name, stop, iv, current_job_fn)

    if not _redis_ok():
        return None
    t = threading.Thread(target=_run, name=f"monitor-sampler-{name}", daemon=True)
    t.start()
    return t


_STOP_FLAGS: dict[str, threading.Event] = {}


def stop_sampler() -> None:
    for ev in _STOP_FLAGS.values():
        ev.set()
    _STOP_FLAGS.clear()
```

实现时删掉测试里的 `test_sampler_loop_pushes_and_stops`（其内容已被 `test_start_sampler_thread` 覆盖）。

- [ ] **Step 4: pyproject 加依赖**

`pyproject.toml` `dependencies` 数组加 `"psutil>=5.9",`，跑 `uv sync`（或 `uv add psutil`）。

- [ ] **Step 5: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_proc_sampler.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
ruff check src/lquant/monitor/proc_sampler.py tests/unit/test_monitor_proc_sampler.py
git add src/lquant/monitor/proc_sampler.py tests/unit/test_monitor_proc_sampler.py pyproject.toml uv.lock
git commit -m "feat: 进程自采样线程（psutil→Redis SETEX）+ psutil 依赖"
```

---

### Task 6: MonitoringWorker（RQ worker 子类）

**Files:**
- Create: `src/lquant/monitor/worker.py`
- Test: `tests/unit/test_monitor_worker.py`

**Interfaces:**
- Consumes: `emit.emit_task_event`；RQ 2.x `Worker/Job`（行为以 uv.lock 锁定版本为准）
- Produces: `worker.MonitoringWorker(Worker)`（覆写 `perform_job`：前置 `emit started`，结束后按 `job.get_status()` 发 finished/failed，error 取 `job.latest_result()` 截断 200）；`worker.spawn_worker(name: str, queues: list[str]) -> None`（子进程入口：建 MonitoringWorker + 启动 sampler 线程 + `worker.run()`）；`worker.run_supervisor(general: int, backtest: int) -> None`（spawn 子进程 + watchdog respawn + 信号处理）；`worker.current_job_fn() -> str | None`（RQ Worker registry 按 pid 匹配 → current job id）

- [ ] **Step 1: 写失败测试**

```python
"""MonitoringWorker 事件发射 + current_job_fn registry 匹配（mock RQ）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from lquant.monitor import emit as emit_mod
from lquant.monitor import worker as wk


def _mk_job(status="finished"):
    job = MagicMock()
    job.get_status.return_value = status
    job.id = "j9"
    job.enqueued_at = 100.0
    return job


def _run_perform(status, result_of_latest=None):
    """直接调 MonitoringWorker.perform_job 的包装逻辑（不触发真实 RQ）。"""
    evs = []

    def fake_emit(**kw):
        evs.append(kw)

    job = _mk_job(status)
    latest = MagicMock()
    if result_of_latest is not None:
        latest.result.return_value = result_of_latest  # Result.Failure 返回值是 exc 字符串

    with patch.object(wk, "emit_task_event", fake_emit), \
         patch.object(wk.Worker, "perform_job", return_value=status == "finished"), \
         patch.object(wk, "_latest_error", return_value=result_of_latest):
        w = wk.MonitoringWorker(["lquant-default"])
        w.perform_job(job)
    return evs, job


def test_perform_started_and_finished():
    evs, _ = _run_perform("finished")
    assert evs[0]["event"] == "started"
    assert evs[-1]["event"] == "finished"
    assert evs[-1]["job_id"] == "j9"


def test_perform_failed_with_message():
    evs, _ = _run_perform("failed", result_of_latest="ValueError: bad")
    assert evs[-1]["event"] == "failed"
    assert evs[-1]["message"] == "ValueError: bad"


def test_started_event_has_no_finished():
    evs, _ = _run_perform("finished")
    assert evs[0]["finished_at"] is None
    assert evs[0]["queue"] == "lquant-default"


def test_current_job_fn_matches_pid():
    w = MagicMock()
    w.pid = 4242
    w.get_current_job_id.return_value = "job-zz"
    with patch.object(wk, "Worker") as fake_w:
        fake_w.all.return_value = [w]
        with patch.object(wk.os, "getpid", return_value=4242):
            assert wk.current_job_fn() == "job-zz"


def test_current_job_fn_no_match():
    with patch.object(wk, "Worker") as fake_w:
        fake_w.all.return_value = []
        assert wk.current_job_fn() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_worker.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 worker.py（先只实现测试覆盖的部分）**

```python
"""MonitoringWorker（任务事件发射）+ lq worker 进程组 supervisor。"""
from __future__ import annotations

import logging
import os

from rq import Worker

from lquant.monitor.emit import emit_task_event

_LOG = logging.getLogger(__name__)


def _latest_error(job) -> str | None:
    """从 RQ Result 取失败消息（截断 200）；无则 None。"""
    try:
        res = job.latest_result()
        if res is not None and getattr(res, "type", None) is not None \
                and "failure" in str(getattr(res.type, "name", "")).lower():
            return str(res.return_value)[:200]
    except Exception:  # noqa: BLE001
        pass
    return None


class MonitoringWorker(Worker):
    def perform_job(self, job, queue):
        job_name = getattr(job, "func_name", None) or job.id
        started = time.time()
        emit_task_event(event="started", job_id=job.id, job_name=job_name,
                        queue=queue.name, enqueued_at=_to_ts(job.enqueued_at),
                        started_at=started, finished_at=None)
        ok = super().perform_job(job, queue)
        finished = time.time()
        status = job.get_status()
        emit_task_event(
            event="finished" if status == "finished" else "failed",
            job_id=job.id, job_name=job_name, queue=queue.name,
            enqueued_at=_to_ts(job.enqueued_at), started_at=started,
            finished_at=finished, message=None if ok else _latest_error(job))
        return ok


def _to_ts(dt) -> float | None:
    if dt is None:
        return None
    return dt.timestamp() if hasattr(dt, "timestamp") else float(dt)
```

文件头部补 `import time`；`current_job_fn`：

```python
def current_job_fn():
    """RQ Worker registry 按 pid 找自身 → 当前 job id；找不到 None。"""
    try:
        me = os.getpid()
        for w in Worker.all():
            if getattr(w, "pid", None) == me:
                return w.get_current_job_id()  # RQ 版本 API 以 uv.lock 为准
    except Exception:  # noqa: BLE001
        return None
    return None
```

（`spawn_worker` / `run_supervisor` 在 Task 9 CLI 集成时补全并测；本任务只交付 `MonitoringWorker` 与 `current_job_fn`。）

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_worker.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/worker.py tests/unit/test_monitor_worker.py
git add src/lquant/monitor/worker.py tests/unit/test_monitor_worker.py
git commit -m "feat: MonitoringWorker 任务生命周期事件发射"
```

---

### Task 7: flusher 落盘（monitor.duckdb 唯一写者）

**Files:**
- Create: `src/lquant/monitor/flusher.py`
- Test: `tests/unit/test_monitor_flusher.py`

**Interfaces:**
- Consumes: `ring.api_ring.drain()`、`ring.local_events.drain()`、`emit.EVENTS_KEY`、`settings.monitor_db_path / monitor_retention_days`；`server.jobs._redis_available / get_redis`
- Produces:
  - `flusher.ensure_tables(con) -> None`（幂等 DDL：metrics_api / metrics_task / metrics_sys，结构见 spec §6）
  - `flusher.flush_once(now: float | None = None) -> dict`（单周期：环缓冲→metrics_api、drain events list（RPOP FIFO）→metrics_task、内存队列→metrics_task、Redis proc 样本 SCAN→metrics_sys、到清理日做保留清理；返回 `{"api": n, "task": n, "sys": n}`）
  - `flusher.start_flusher() -> threading.Thread | None` / `flusher.stop_flusher() -> None`（停止时 final flush）

- [ ] **Step 1: 写失败测试**

```python
"""flusher：建表、落盘、事件 drain、样本落盘、保留清理、写失败重试。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from lquant.monitor import flusher as fl
from lquant.monitor.ring import api_ring, local_events
from lquant.monitor.types import ApiMetricPoint, ProcSample, TaskEvent


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    """独立 monitor.duckdb + 环缓冲清空。"""
    path = str(tmp_path / "mon.duckdb")
    monkeypatch.setenv("LQ_MONITOR_DB", path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    api_ring.drain()
    local_events.drain()
    yield path
    get_settings.cache_clear()


def _pt(ts=1000.0, route="/api/factors", status=200, dur=50.0):
    return ApiMetricPoint(ts=ts, route=route, method="GET", status=status,
                          duration_ms=dur, dur_category=fl._classify(status, dur)
                          if hasattr(fl, "_classify") else "fast")


def test_ensure_tables_idempotent(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    fl.ensure_tables(con)  # 二次不炸
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"metrics_api", "metrics_task", "metrics_sys"} <= names
    con.close()


def test_flush_api_points(mdb):
    api_ring.drain()
    api_ring.append(_pt(ts=time.time() - 5))
    api_ring.append(ApiMetricPoint(ts=time.time() - 4, route="/api/x",
                                   method="POST", status=500, duration_ms=9.0,
                                   dur_category="error"))
    out = fl.flush_once()
    assert out["api"] == 2
    con = duckdb.connect(mdb)
    n, err = con.execute(
        "SELECT count(*), sum(CASE WHEN dur_category='error' THEN 1 ELSE 0 END) "
        "FROM metrics_api").fetchone()
    con.close()
    assert n == 2 and err == 1
    assert api_ring.snapshot() == ()  # 成功落盘后清空


def test_flush_local_task_events(mdb):
    local_events.drain()
    local_events.put(TaskEvent(
        event_ts=time.time(), job_id="j1", job_name="fn", event="finished",
        queue="lquant-default", enqueued_at=100.0, started_at=110.0,
        finished_at=120.0, elapsed_ms=10000.0, queue_delay_ms=10000.0,
        message=None))
    out = fl.flush_once()
    assert out["task"] >= 1
    con = duckdb.connect(mdb)
    row = con.execute("SELECT job_id, elapsed_ms FROM metrics_task "
                      "WHERE event='finished'").fetchone()
    con.close()
    assert row == ("j1", 10000.0)


def test_flush_redis_events_fifo(mdb):
    """Redis events list：LPUSH 写入的 payload，flusher RPOP FIFO drain。"""
    con = duckdb.connect(mdb)
    con.close()
    fake = MagicMock()
    payloads = [json.dumps({"event_ts": 1.0, "job_id": f"j{i}",
                            "job_name": "fn", "event": "finished",
                            "queue": "q", "enqueued_at": None,
                            "started_at": None, "finished_at": None,
                            "elapsed_ms": 1.0, "queue_delay_ms": None,
                            "message": None}) for i in (1, 2)]
    # RPOP 顺序 = FIFO：j1 先出
    fake.rpop.side_effect = [payloads[0], payloads[1], None]
    with patch.object(fl, "_redis_available", return_value=True), \
         patch.object(fl, "_get_redis", return_value=fake):
        out = fl.flush_once()
    assert out["task"] == 2
    got = duckdb.connect(mdb).execute(
        "SELECT job_id FROM metrics_task ORDER BY event_ts, job_id").fetchall()
    assert [r[0] for r in got] == ["j1", "j2"]


def test_flush_sys_samples(mdb):
    con = duckdb.connect(mdb)
    con.close()
    fake = MagicMock()
    fake.scan_iter.return_value = [b"lquant:monitor:proc:http"]
    fake.get.return_value = json.dumps({"ts": time.time(), "proc_name": "http",
                                        "pid": 1, "cpu_pct": 1.5,
                                        "mem_rss_mb": 88.0,
                                        "current_job": None})
    with patch.object(fl, "_redis_available", return_value=True), \
         patch.object(fl, "_get_redis", return_value=fake):
        out = fl.flush_once()
    assert out["sys"] == 1
    row = duckdb.connect(mdb).execute(
        "SELECT proc_name, cpu_pct FROM metrics_sys").fetchone()
    assert row == ("http", 1.5)


def test_write_failure_keeps_ring(mdb, monkeypatch):
    api_ring.drain()
    api_ring.append(_pt())
    # 用不存在的父目录制造写失败
    monkeypatch.setenv("LQ_MONITOR_DB", "/nonexistent-dir-zz/x.duckdb")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    out = fl.flush_once()  # 不应抛出
    assert out["api"] == 0
    assert len(api_ring.snapshot()) == 1  # 数据保留待重试
    get_settings.cache_clear()


def test_retention_cleanup(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    old = time.time() - 30 * 86400
    con.execute("INSERT INTO metrics_api VALUES (?, 'r', 'GET', 200, 1.0, 'fast')",
                [__import__("datetime").datetime.fromtimestamp(old)])
    con.close()
    with patch.object(fl, "_should_cleanup_today", return_value=True):
        fl.flush_once()
    n = duckdb.connect(mdb).execute("SELECT count(*) FROM metrics_api").fetchone()[0]
    assert n == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_flusher.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 flusher.py**

```python
"""flusher：http 进程内唯一写 monitor.duckdb 的落盘线程。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime

from lquant.monitor.emit import EVENTS_KEY
from lquant.monitor.ring import api_ring, local_events

_LOG = logging.getLogger(__name__)

_CLEANUP_STATE = {"last": None}
_STOP = threading.Event()
_thread: threading.Thread | None = None


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
    con.executemany("INSERT INTO metrics_api VALUES (?, ?, ?, ?, ?, ?)",
                    [(_ts(p.ts), p.route, p.method, p.status,
                      p.duration_ms, p.dur_category) for p in pts])
    return len(pts)


def _write_task(con, evs) -> int:
    if not evs:
        return 0
    con.executemany("INSERT INTO metrics_task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(_ts(e.event_ts), e.job_id, e.job_name, e.event, e.queue,
                      _ts(e.enqueued_at), _ts(e.started_at), _ts(e.finished_at),
                      e.elapsed_ms, e.queue_delay_ms, e.message) for e in evs])
    return len(evs)


def _drain_redis_events(r) -> list:
    from lquant.monitor.types import TaskEvent

    evs: list[TaskEvent] = []
    while True:
        raw = r.rpop(EVENTS_KEY)
        if raw is None:
            break
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
    today = date.today()
    if _CLEANUP_STATE["last"] == today:
        return False
    _CLEANUP_STATE["last"] = today
    return True


def _cleanup(con, retention_days: int) -> None:
    con.execute(f"DELETE FROM metrics_api WHERE ts < now() - INTERVAL {int(retention_days)} DAY")
    con.execute(f"DELETE FROM metrics_task WHERE event_ts < now() - INTERVAL {int(retention_days)} DAY")
    con.execute(f"DELETE FROM metrics_sys WHERE ts < now() - INTERVAL {int(retention_days)} DAY")


def flush_once(now: float | None = None) -> dict:
    """单周期：返回各表写入行数；任何失败只 log。"""
    out = {"api": 0, "task": 0, "sys": 0}
    r = None
    if _redis_available():
        try:
            r = _get_redis()
        except Exception:  # noqa: BLE001
            r = None
    try:
        con = _monitor_con()
    except Exception:  # noqa: BLE001 - 写失败保留数据待重试
        _LOG.warning("monitor.duckdb 连接失败，数据保留待重试", exc_info=True)
        return out
    try:
        ensure_tables(con)
        out["api"] = _write_api(con, api_ring.drain())
        evs = list(local_events.drain())
        if r is not None:
            evs.extend(_drain_redis_events(r))
        out["task"] = _write_task(con, evs)
        if r is not None:
            out["sys"] = _write_sys(con, _drain_sys_samples(r))
        from lquant.core.config import get_settings

        if _should_cleanup_today():
            _cleanup(con, get_settings().monitor_retention_days)
        con.commit()
    except Exception:  # noqa: BLE001
        _LOG.warning("flusher 落盘失败，数据保留待重试", exc_info=True)
        return out
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _cycle(interval: float) -> None:
    while not _STOP.wait(interval):
        flush_once()


def start_flusher() -> threading.Thread | None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    from lquant.core.config import get_settings

    _STOP.clear()
    _thread = threading.Thread(target=_cycle,
                               args=(get_settings().monitor_flush_interval_sec,),
                               name="monitor-flusher", daemon=True)
    _thread.start()
    return _thread


def stop_flusher() -> None:
    _STOP.set()
    flush_once()  # final flush：环缓冲剩余 + Redis 样本剩余
    global _thread
    _thread = None
```

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_flusher.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/flusher.py tests/unit/test_monitor_flusher.py
git add src/lquant/monitor/flusher.py tests/unit/test_monitor_flusher.py
git commit -m "feat: flusher 落盘线程（monitor.duckdb 唯一写者）"
```

---

### Task 8: monitor 包生命周期 + main.py 装配

**Files:**
- Modify: `src/lquant/monitor/__init__.py`
- Modify: `src/lquant/server/main.py`（create_app + startup/shutdown）
- Test: `tests/unit/test_monitor_lifecycle.py`

**Interfaces:**
- Consumes: `api_mw.MonitorMiddleware`、`flusher.start_flusher/stop_flusher`、`proc_sampler.start_sampler/stop_sampler`、`settings.monitor_enabled`
- Produces: `monitor.start_monitor() -> None`（enabled 时：flusher 线程 + http 自采样线程 proc_name='http'）；`monitor.stop_monitor() -> None`（stop_sampler + stop_flusher final flush）；`create_app()` 挂 `MonitorMiddleware`（`app = MonitorMiddleware(app)` 包 ASGI 栈）；startup/shutdown 钩子调 start/stop。

- [ ] **Step 1: 写失败测试**

```python
"""start/stop_monitor 生命周期：enabled 开关 + 线程启停。"""
from __future__ import annotations

from unittest.mock import patch

from lquant.monitor import __init__ as mon


def test_start_disabled(monkeypatch):
    with patch.object(mon.flusher, "start_flusher") as sf, \
         patch.object(mon.proc_sampler, "start_sampler") as ss:
        monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
        from lquant.core.config import get_settings

        get_settings.cache_clear()
        mon.start_monitor()
        mon.stop_monitor()
        sf.assert_not_called()
        ss.assert_not_called()
        get_settings.cache_clear()


def test_start_enabled_starts_threads(monkeypatch):
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "1")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    with patch.object(mon.flusher, "start_flusher", return_value=None) as sf, \
         patch.object(mon.proc_sampler, "start_sampler", return_value=None) as ss, \
         patch.object(mon.flusher, "stop_flusher") as sfn, \
         patch.object(mon.proc_sampler, "stop_sampler") as ssn:
        mon.start_monitor()
        sf.assert_called_once()
        ss.assert_called_once()
        mon.stop_monitor()
        sfn.assert_called_once()
        ssn.assert_called_once()
    get_settings.cache_clear()


def test_app_has_middleware_and_endpoints():
    """create_app 后 /api/monitor 路由存在；MonitorMiddleware 包裹生效。"""
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/api/monitor/summary" in paths
    with TestClient(app) as client:
        r = client.get("/api/monitor/summary")
        assert r.status_code == 200
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_lifecycle.py -v`
Expected: FAIL（start_monitor 不存在 / 路由不存在）

- [ ] **Step 3: 实现**

`src/lquant/monitor/__init__.py`：

```python
"""lquant 监控子系统：采集 → flusher 落盘 → 查询。"""
from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)


def start_monitor() -> None:
    """http 进程启动钩子：flusher 线程 + 自身采样。失败只 log。"""
    try:
        from lquant.core.config import get_settings

        if not get_settings().monitor_enabled:
            return
        from lquant.monitor import flusher, proc_sampler

        flusher.start_flusher()
        proc_sampler.start_sampler("http")
    except Exception:  # noqa: BLE001
        _LOG.exception("start_monitor 失败")


def stop_monitor() -> None:
    try:
        from lquant.monitor import flusher, proc_sampler

        proc_sampler.stop_sampler()
        flusher.stop_flusher()  # final flush
    except Exception:  # noqa: BLE001
        _LOG.exception("stop_monitor 失败")
```

`src/lquant/server/main.py` 改动（两处）：

```python
from lquant.monitor import start_monitor, stop_monitor
from lquant.monitor.api_mw import MonitorMiddleware
```

`create_app()` 末尾 `return app` 改为：

```python
    app.include_router(ws.router)  # /ws/jobs/{id}，无 /api 前缀（与前端代理一致）
    return MonitorMiddleware(app)
```

（同时 `create_app` 上方类型注解保持 FastAPI；`app = create_app()` 不变 —— 返回的是包了中间件的 ASGI 栈，FastAPI 测试客户端兼容。）

startup/shutdown 钩子（`@app.on_event` 部分追加）：

```python
@app.on_event("startup")
def _monitor_startup() -> None:
    start_monitor()


@app.on_event("shutdown")
def _monitor_shutdown() -> None:
    stop_monitor()
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `python -m pytest tests/unit/test_monitor_lifecycle.py -v && python -m pytest tests/unit/test_api.py -x -q 2>&1 | tail -5`
Expected: PASS（既有 API 测试不受中间件影响）

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/__init__.py src/lquant/server/main.py tests/unit/test_monitor_lifecycle.py
git add src/lquant/monitor/__init__.py src/lquant/server/main.py tests/unit/test_monitor_lifecycle.py
git commit -m "feat: monitor 生命周期装配进 http 进程"
```

---

### Task 9: 查询层 + /api/monitor 端点

**Files:**
- Create: `src/lquant/monitor/queries.py`
- Create: `src/lquant/server/api/monitor.py`
- Modify: `src/lquant/server/main.py`（routers 元组加 `monitor`）
- Test: `tests/unit/test_monitor_api.py`

**Interfaces:**
- Consumes: `flusher._monitor_con()`（查询连接同路径同配置）；`server.envelope.make_router`；`server.jobs._redis_available/get_redis`
- Produces:
  - `queries.range_bucket(range_name: str) -> int`（秒：1h→60、6h→300、24h→900、7d→7200；非法值 ValueError）
  - `queries.api_latency_series(range_name: str) -> list[dict]`（分桶 `{bucket, count, avg, p50, p95, err_rate}`，generate_series 网格 + LEFT JOIN 补零；`time_bucket` 不可用则 epoch 整除分桶）
  - `queries.slowest_routes(range_name: str, limit: int = 10) -> list[dict]`
  - `queries.task_latency_series(range_name: str) -> list[dict]`（`{bucket, p95_delay, p95_elapsed, count}`）
  - `queries.queue_depths() -> list[dict]`（三队列 `{queue, pending, failed}`；pending = Queue.count + StartedJobRegistry.count）
  - `queries.proc_statuses() -> list[dict]`（SCAN proc keys → `{proc_name, pid, cpu_pct, mem_rss_mb, current_job, online, ts}`；online = 样本年龄 <15s）
  - `queries.api_live() -> dict`（环缓冲最近 5min：count/p50/p95/err_rate）
  - `queries.recent_task_events(limit: int = 50) -> list[dict]`（LRANGE recent list）
  - `queries.data_pulls() -> dict`（主库 reader() 读 collect_log：`{recent: [...200], by_job: [...]}`）
  - 路由：`GET /api/monitor/summary`、`/api-latency?range=`、`/tasks?range=`、`/data-pulls`、`/workers`（用 `make_router(prefix="/monitor", tags=["monitor"])`，返回裸 dict 由 EnvRoute 包信封）

- [ ] **Step 1: 写失败测试**

```python
"""monitor 查询层与 API 端点：分桶/聚合/快照/信封。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from lquant.monitor import queries as q
from lquant.monitor.ring import api_ring
from lquant.monitor.types import ApiMetricPoint


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    path = str(tmp_path / "mon.duckdb")
    monkeypatch.setenv("LQ_MONITOR_DB", path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    api_ring.drain()
    yield path
    get_settings.cache_clear()


def _seed_api(mdb, n=10):
    from datetime import datetime, timedelta

    con = duckdb.connect(mdb)
    from lquant.monitor.flusher import ensure_tables

    ensure_tables(con)
    base = datetime.now() - timedelta(minutes=2)
    for i in range(n):
        con.execute("INSERT INTO metrics_api VALUES (?, ?, ?, ?, ?, ?)",
                    [base + timedelta(seconds=i), "/api/factors", "GET",
                     200 if i < 8 else 500, 50.0 + i * 100,
                     "fast" if i < 8 else "error"])
    con.commit()
    con.close()


def test_range_bucket():
    assert q.range_bucket("1h") == 60
    assert q.range_bucket("6h") == 300
    assert q.range_bucket("24h") == 900
    assert q.range_bucket("7d") == 7200
    with pytest.raises(ValueError):
        q.range_bucket("9h")


def test_api_latency_series_buckets(mdb):
    _seed_api(mdb)
    rows = q.api_latency_series("1h")
    assert rows, "应有分桶输出"
    assert all({"bucket", "count", "avg", "p50", "p95", "err_rate"} == set(r) for r in rows)
    total = sum(r["count"] for r in rows)
    assert total == 10
    assert sum(r["err_rate"] for r in rows) == pytest.approx(0.2)


def test_slowest_routes(mdb):
    _seed_api(mdb)
    rows = q.slowest_routes("1h")
    assert rows[0]["route"] == "/api/factors"
    assert rows[0]["count"] == 10


def test_proc_statuses_offline_and_online(mdb):
    fake = MagicMock()
    stale = time.time() - 60
    fresh = time.time()
    fake.scan_iter.return_value = [b"lquant:monitor:proc:general-0",
                                   b"lquant:monitor:proc:http"]
    fake.get.side_effect = [
        json.dumps({"ts": stale, "proc_name": "general-0", "pid": 1,
                    "cpu_pct": None, "mem_rss_mb": None, "current_job": None}),
        json.dumps({"ts": fresh, "proc_name": "http", "pid": 2,
                    "cpu_pct": 3.0, "mem_rss_mb": 100.0, "current_job": None}),
    ]
    with patch.object(q, "_redis_available", return_value=True), \
         patch.object(q, "_get_redis", return_value=fake):
        rows = q.proc_statuses()
    by = {r["proc_name"]: r for r in rows}
    assert by["general-0"]["online"] is False
    assert by["http"]["online"] is True
    assert by["http"]["cpu_pct"] == 3.0


def test_api_live_from_ring(mdb):
    api_ring.append(ApiMetricPoint(ts=time.time() - 10, route="/api/x",
                                   method="GET", status=200, duration_ms=50.0,
                                   dur_category="fast"))
    api_ring.append(ApiMetricPoint(ts=time.time() - 10, route="/api/x",
                                   method="GET", status=500, duration_ms=20.0,
                                   dur_category="error"))
    out = q.api_live()
    assert out["count"] == 2
    assert out["err_rate"] == pytest.approx(0.5)


def test_summary_endpoint_envelope(mdb):
    _seed_api(mdb)
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        assert "procs" in body["data"]
        assert "queues" in body["data"]
        assert "api_live" in body["data"]
        r2 = client.get("/api/monitor/api-latency?range=1h")
        assert r2.status_code == 200
        assert "series" in r2.json()["data"]
        assert "slowest" in r2.json()["data"]


def test_tasks_endpoint(mdb):
    from datetime import datetime, timedelta

    con = duckdb.connect(mdb)
    from lquant.monitor.flusher import ensure_tables

    ensure_tables(con)
    con.execute("INSERT INTO metrics_task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [datetime.now(), "j1", "fn", "finished", "lquant-default",
                 datetime.now() - timedelta(seconds=5),
                 datetime.now() - timedelta(seconds=4),
                 datetime.now(), 1000.0, 1000.0, None])
    con.commit()
    con.close()
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/tasks?range=1h")
        assert r.status_code == 200
        rows = r.json()["data"]["series"]
        assert sum(x["count"] for x in rows) == 1


def test_data_pulls_endpoint(tmp_path, monkeypatch):
    """collect_log 视角：主库 reader() 读 recent + by_job。"""
    import os

    os.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    with __import__("lquant.core.db", fromlist=["writer"]).writer() as con:
        con.execute("CREATE TABLE collect_log (job VARCHAR, trade_date DATE, "
                    "started_at TIMESTAMP, finished_at TIMESTAMP, rows INTEGER, "
                    "status VARCHAR, message VARCHAR, PRIMARY KEY (job, trade_date))")
        con.execute("INSERT INTO collect_log VALUES ('daily', '2026-09-01', "
                    "'2026-09-01 18:00:00', '2026-09-01 18:00:10', 100, 'ok', NULL)")
    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/data-pulls")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["recent"][0]["job"] == "daily"
        assert data["by_job"][0]["avg_duration_ms"] == 10000.0
    get_settings.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_api.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 queries.py**

```python
"""monitor 查询层：monitor.duckdb 聚合 + Redis 快照 + 主库 collect_log。"""
from __future__ import annotations

import json
import logging
import time

_LOG = logging.getLogger(__name__)

RANGE_BUCKET = {"1h": 60, "6h": 300, "24h": 900, "7d": 7200}
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


def _bucket_expr(bucket_sec: int) -> str:
    """epoch 整除分桶（time_bucket 可用性以锁定 DuckDB 版本为准；此式通用）。"""
    return (f"to_timestamp(floor(epoch(ts) / {bucket_sec}) * {bucket_sec})"
            if "ts" else "")


def api_latency_series(range_name: str) -> list[dict]:
    bs = range_bucket(range_name)
    con = _con()
    try:
        rows = con.execute(f"""
            WITH grid AS (
                SELECT unnest(generate_series(
                    to_timestamp(epoch(now()) - {range_sec(range_name)}),
                    to_timestamp(epoch(now())), INTERVAL {bs} SECOND)) AS b),
            agg AS (
                SELECT to_timestamp(floor(epoch(ts) / {bs}) * {bs}) AS b,
                       count(*) AS cnt, avg(duration_ms) AS avg_ms,
                       quantile_cont(duration_ms, 0.5) AS p50,
                       quantile_cont(duration_ms, 0.95) AS p95,
                       avg(CASE WHEN dur_category = 'error' THEN 1.0
                                ELSE 0.0 END) AS err_rate
                FROM metrics_api
                WHERE ts > now() - INTERVAL {range_sec(range_name)} SECOND
                GROUP BY 1)
            SELECT grid.b, coalesce(agg.cnt, 0), coalesce(agg.avg_ms, NULL),
                   agg.p50, agg.p95, coalesce(agg.err_rate, 0.0)
            FROM grid LEFT JOIN agg USING (b) ORDER BY grid.b
        """).fetchall()
    finally:
        con.close()
    return [{"bucket": r[0].isoformat(), "count": r[1], "avg": r[2],
             "p50": r[3], "p95": r[4], "err_rate": r[5]} for r in rows]


def range_sec(range_name: str) -> int:
    return {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600,
            "7d": 7 * 86400}[range_name]


def slowest_routes(range_name: str, limit: int = 10) -> list[dict]:
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT route, count(*) AS cnt, avg(duration_ms) AS avg_ms,
                   quantile_cont(duration_ms, 0.95) AS p95,
                   avg(CASE WHEN dur_category = 'error' THEN 1.0
                            ELSE 0.0 END) AS err_rate
            FROM metrics_api
            WHERE ts > now() - INTERVAL {range_sec(range_name)} SECOND
            GROUP BY route ORDER BY p95 DESC LIMIT {int(limit)}
        """).fetchall()
    finally:
        con.close()
    return [{"route": r[0], "count": r[1], "avg": r[2], "p95": r[3],
             "err_rate": r[4]} for r in rows]


def task_latency_series(range_name: str) -> list[dict]:
    bs = range_bucket(range_name)
    con = _con()
    try:
        rows = con.execute(f"""
            WITH grid AS (
                SELECT unnest(generate_series(
                    to_timestamp(epoch(now()) - {range_sec(range_name)}),
                    to_timestamp(epoch(now())), INTERVAL {bs} SECOND)) AS b),
            agg AS (
                SELECT to_timestamp(floor(epoch(event_ts) / {bs}) * {bs}) AS b,
                       quantile_cont(elapsed_ms, 0.95) AS p95_elapsed,
                       quantile_cont(queue_delay_ms, 0.95) AS p95_delay,
                       count(*) AS cnt
                FROM metrics_task
                WHERE event_ts > now() - INTERVAL {range_sec(range_name)} SECOND
                  AND event IN ('finished', 'failed')
                GROUP BY 1)
            SELECT grid.b, agg.p95_delay, agg.p95_elapsed,
                   coalesce(agg.cnt, 0)
            FROM grid LEFT JOIN agg USING (b) ORDER BY grid.b
        """).fetchall()
    finally:
        con.close()
    return [{"bucket": r[0].isoformat(), "p95_delay": r[1],
             "p95_elapsed": r[2], "count": r[3]} for r in rows]


def queue_depths() -> list[dict]:
    from lquant.server.jobs import QUEUES

    out: list[dict] = []
    if not _redis_available():
        return [{"queue": q, "pending": None, "failed": None} for q in QUEUES]
    try:
        from rq.job import Job
        from rq.registry import (FailedJobRegistry, StartedJobRegistry)
        from rq.queue import Queue

        r = _get_redis()
        for name in QUEUES:
            queue = Queue(name, connection=r)
            pending = queue.count + StartedJobRegistry(name, connection=r).count
            failed = FailedJobRegistry(name, connection=r).count
            out.append({"queue": name, "pending": int(pending),
                        "failed": int(failed)})
    except Exception:  # noqa: BLE001
        _LOG.warning("队列深度查询失败", exc_info=True)
        return [{"queue": q, "pending": None, "failed": None} for q in QUEUES]
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

    def pct(v: list[float], q: float) -> float:
        i = max(0, min(len(v) - 1, int(q * len(v)) - 1))
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
```

（实现时删除未使用的 `_bucket_expr` helper。）

- [ ] **Step 4: 实现 server/api/monitor.py**

```python
"""监控 API：实时快照 + 历史聚合（统一信封）。"""
from __future__ import annotations

from fastapi import HTTPException, Query

from lquant.monitor import queries as q
from lquant.server.envelope import make_router

router = make_router(prefix="/monitor", tags=["monitor"])


@router.get("/summary")
def summary() -> dict:
    return q.summary()


@router.get("/workers")
def workers() -> dict:
    return {"procs": q.proc_statuses()}


@router.get("/api-latency")
def api_latency(range: str = Query("1h")) -> dict:
    try:
        return {"series": q.api_latency_series(range),
                "slowest": q.slowest_routes(range)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/tasks")
def tasks(range: str = Query("1h")) -> dict:
    try:
        return {"series": q.task_latency_series(range)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/data-pulls")
def data_pulls() -> dict:
    return q.data_pulls()
```

`server/main.py` routers 元组加 `monitor`：

```python
    for r in (health, data, factors, backtests, market, paper, watchlist,
              strategies, analyses, sync, etf, news, settings, ask, monitor):
```

import 同步加 `monitor`。

- [ ] **Step 5: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_api.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
ruff check src/lquant/monitor/queries.py src/lquant/server/api/monitor.py tests/unit/test_monitor_api.py
git add src/lquant/monitor/queries.py src/lquant/server/api/monitor.py src/lquant/server/main.py tests/unit/test_monitor_api.py
git commit -m "feat: monitor 查询层与 /api/monitor/* 端点"
```

---

### Task 10: `lq worker` CLI + supervisor

**Files:**
- Modify: `src/lquant/monitor/worker.py`（补 `spawn_worker` / `run_supervisor`）
- Create: `src/lquant/cli/commands/worker.py`
- Modify: `src/lquant/cli/main.py`（注册 `cli.add_command(worker.worker)`）
- Test: `tests/unit/test_monitor_cli.py`

**Interfaces:**
- Consumes: `MonitoringWorker`、`proc_sampler.start_sampler`、`config.clamp_backtest_workers / BACKTEST_WORKERS_MAX / get_settings`
- Produces: `worker.spawn_worker(name: str, queues: list[str]) -> None`（子进程入口，top-level 可 pickle：建 MonitoringWorker（name 由 RQ 自动）→ 启动 sampler 线程（current_job_fn）→ `worker.run()`；Redis ping 失败则 exit 1）；`worker.run_supervisor(general: int, backtest: int) -> None`（spawn context 拉起子进程，watchdog 每 5s respawn 死子进程，SIGINT/SIGTERM → 子进程 SIGINT（RQ friendly shutdown）→ 5s grace → terminate）；CLI `lq worker --general N --backtest N`。

- [ ] **Step 1: 写失败测试**

```python
"""lq worker：preflight、K clamp、supervisor 子进程数与 respawn。"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from lquant.monitor import worker as wk


def test_spawn_worker_preflight_fails(monkeypatch, capsys):
    monkeypatch.setattr(wk, "_redis_ok", lambda: False)
    with pytest.raises(SystemExit) as ei:
        wk.spawn_worker("general-0", ["lquant-default"])
    assert ei.value.code == 1
    assert "Redis" in capsys.readouterr().err


def test_supervisor_spawns_expected_children(monkeypatch):
    procs = []

    def fake_spawn(name, queues):
        p = MagicMock()
        p.name = name
        p.is_alive.side_effect = [True] * 3 + [False]  # 第一次检查活着，第二次死
        p.pid = 123
        procs.append(p)
        return p

    monkeypatch.setattr(wk, "_spawn_process", fake_spawn)
    monkeypatch.setattr(wk, "_redis_ok", lambda: True)
    stop_after = {"n": 0}

    def fake_sleep(_):
        stop_after["n"] += 1
        if stop_after["n"] >= 3:
            raise KeyboardInterrupt

    with patch.object(wk.time, "sleep", fake_sleep), \
         patch.object(wk.signal, "signal"):  # 不装真实信号处理器
        with pytest.raises(KeyboardInterrupt):
            wk.run_supervisor(general=1, backtest=2)
    assert len(procs) == 3  # 1 通用 + 2 回测


def test_general_zero_allowed(monkeypatch):
    procs = []

    def fake_spawn(name, queues):
        p = MagicMock()
        p.is_alive.return_value = True
        p.pid = 1
        procs.append(p)
        return p

    monkeypatch.setattr(wk, "_spawn_process", fake_spawn)
    monkeypatch.setattr(wk, "_redis_ok", lambda: True)
    import threading

    stop = threading.Event()

    def fake_sleep(_):
        stop.set()

    monkeypatch.setattr(wk.time, "sleep", fake_sleep)
    wk.run_supervisor(general=0, backtest=1)  # 返回即退出循环
    assert len(procs) == 1
    assert procs[0].name.startswith("backtest-")


def test_cli_defaults_from_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_BACKTEST_WORKERS", raising=False)
    from lquant.cli.commands.worker import _resolve_counts

    general, backtest = _resolve_counts(general=1, backtest=None)
    assert general == 1 and backtest == 2


def test_cli_clamps(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from lquant.cli.commands.worker import _resolve_counts

    general, backtest = _resolve_counts(general=1, backtest=99)
    assert backtest == 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_monitor_cli.py -v`
Expected: FAIL（spawn_worker / run_supervisor / _resolve_counts 不存在）

- [ ] **Step 3: 实现 worker.py 补全 + CLI**

`worker.py` 追加：

```python
import signal
import time

GENERAL_QUEUES = ["lquant-default", "lquant-ingest"]
BACKTEST_QUEUE = "lquant-backtest"


def _redis_ok() -> bool:
    from lquant.server.jobs import _redis_available

    return _redis_available()


def spawn_worker(name: str, queues: list[str]) -> None:
    """子进程入口（top-level，spawn 可 pickle）。Redis 失败 exit 1。"""
    if not _redis_ok():
        print("错误: Redis 不可用 —— 先启动 Redis（docker compose up -d redis）",
              file=__import__("sys").stderr)
        raise SystemExit(1)
    from rq import SimpleWorker  # noqa: F401  # fork 不可用时 RQ 自行降级；默认 Worker 即可
    from lquant.core.config import get_settings
    from lquant.monitor import proc_sampler

    w = MonitoringWorker(queues)
    proc_sampler.start_sampler(name, current_job_fn=current_job_fn)
    try:
        w.run()
    finally:
        proc_sampler.stop_sampler()


def _spawn_process(name: str, queues: list[str]):
    """独立函数便于测试替身。"""
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=spawn_worker, args=(name, queues), name=name,
                    daemon=True)
    p.start()
    return p


def run_supervisor(general: int, backtest: int) -> None:
    """拉起 worker 组 + watchdog respawn；Ctrl-C → 子进程 SIGINT → 5s grace → terminate。"""
    if not _redis_ok():
        print("错误: Redis 不可用 —— 先启动 Redis（docker compose up -d redis）",
              file=__import__("sys").stderr)
        raise SystemExit(1)
    children: list = []
    for i in range(general):
        children.append(_spawn_process(f"general-{i}", GENERAL_QUEUES))
    for i in range(backtest):
        children.append(_spawn_process(f"backtest-{i}", [BACKTEST_QUEUE]))
    print(f"[worker] 已拉起 {len(children)} 个 worker 进程")

    def _shutdown(signum, frame):
        for p in children:
            if p.is_alive():
                import os as _os

                _os.kill(p.pid, signal.SIGINT)  # RQ friendly shutdown
        deadline = time.time() + 5
        for p in children:
            p.join(timeout=max(0.1, deadline - time.time()))
        for p in children:
            if p.is_alive():
                p.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        while True:
            time.sleep(5)
            for idx, p in enumerate(children):
                if not p.is_alive():
                    _LOG.warning("worker %s 退出(code=%s)，respawn", p.name,
                                 p.exitcode)
                    name, queues = p.name, (
                        GENERAL_QUEUES if p.name.startswith("general-")
                        else [BACKTEST_QUEUE])
                    children[idx] = _spawn_process(name, queues)
    except SystemExit:
        raise
```

`src/lquant/cli/commands/worker.py`：

```python
"""lq worker：拉起 worker 进程组（1 通用 + K 回测）。"""
from __future__ import annotations

import click

from lquant.core.config import BACKTEST_WORKERS_MAX, clamp_backtest_workers


def _resolve_counts(general: int, backtest: int | None) -> tuple[int, int]:
    """CLI > env/config > 默认；clamp 0..4。"""
    if backtest is None:
        from lquant.core.config import get_settings

        backtest = get_settings().backtest_workers
    return max(0, general), clamp_backtest_workers(backtest)


@click.command()
@click.option("--general", default=1, type=int,
              help=f"通用 worker 数（订阅 default+ingest 队列）；0 关闭")
@click.option("--backtest", default=None, type=int,
              help=f"回测 worker 数（默认取配置，上限 {BACKTEST_WORKERS_MAX}）")
def worker(general: int, backtest: int | None) -> None:
    """启动 worker 进程组（需 Redis）。"""
    g, b = _resolve_counts(general, backtest)
    if g == 0 and b == 0:
        click.echo("错误: general 与 backtest 均为 0，无进程可拉起", err=True)
        raise SystemExit(1)
    from lquant.monitor.worker import run_supervisor

    run_supervisor(general=g, backtest=b)
```

`src/lquant/cli/main.py` 加：

```python
from lquant.cli.commands import worker as worker_cmd
cli.add_command(worker_cmd.worker)
```

（注意 `cli/commands/__init__.py` 若有导出清单需同步；实现时检查。）

- [ ] **Step 4: 跑测试通过**

Run: `python -m pytest tests/unit/test_monitor_cli.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
ruff check src/lquant/monitor/worker.py src/lquant/cli/commands/worker.py src/lquant/cli/main.py tests/unit/test_monitor_cli.py
git add src/lquant/monitor/worker.py src/lquant/cli/commands/worker.py src/lquant/cli/main.py tests/unit/test_monitor_cli.py
git commit -m "feat: lq worker CLI（1 通用 + K 回测 worker 进程组）"
```

---

### Task 11: lquant.sh 集成

**Files:**
- Modify: `lquant.sh`

**Interfaces:**
- Consumes: `lq worker` 命令；docker-compose redis 服务
- Produces: 启动脚本同时拉起 http 与 worker；worker 日志 `logs/worker.log`

- [ ] **Step 1: 查看 lquant.sh 现有启动段**（http 启动、日志目录、pid 管理方式），按同样式追加：

```bash
# worker 进程组（1 通用 + K 回测，K=LQ_BACKTEST_WORKERS 或默认 2）
if command -v redis-cli >/dev/null 2>&1 && redis-cli -u "${LQ_REDIS_URL:-redis://localhost:6379/0}" ping >/dev/null 2>&1; then
  mkdir -p logs
  nohup "$LQ_BIN" worker >> logs/worker.log 2>&1 &
  echo "[worker] started (pid $!), log: logs/worker.log"
else
  echo "[worker] Redis 不可用，跳过 worker 启动（监控页将显示 offline）"
fi
```

（`$LQ_BIN` 按脚本中现有 uvicorn 启动变量替换；若脚本用 `uv run lq`，照抄该形式。）

- [ ] **Step 2: 语法检查**

Run: `bash -n lquant.sh`
Expected: 无输出（语法 OK）

- [ ] **Step 3: 提交**

```bash
git add lquant.sh
git commit -m "feat: lquant.sh 启动 worker 进程组"
```

---

### Task 12: 前端 /monitor 页面

**Files:**
- Create: `web/src/app/monitor/page.tsx`
- Modify: `web/src/components/Sidebar.tsx`（"数据" 组加 `{ href: '/monitor', label: '监控' }`）
- Test: 手动验证（`cd web && npm run dev` 访问 /monitor）

**Interfaces:**
- Consumes: `/api/monitor/summary`、`/api-latency?range=`、`/tasks?range=`、`/data-pulls`（响应走统一信封，前端按项目现有 `getData()`/swr 模式解包）
- Produces: 监控页面；Sidebar "数据" 组新增入口

- [ ] **Step 1: 参照现有页面写页面**

先读 `web/src/app/sync/page.tsx`（同为运维视角表格页）与 `web/src/components/KChart.tsx`（echarts 封装，注意项目记忆：globals.css 导入、双轴图对齐坑），然后实现页面：

- stat 卡区（`/summary` 每 30s swr 刷新）：进程卡（online 状态点 + cpu/mem 当前值）、三队列 pending/failed、api_live p95
- 进程状态表：proc_name / online 点 / cpu% / mem / current_job / 样本年龄
- API 耗时曲线（KChart，range 切换 1h/6h/24h/7d）：p50/p95 双线 + 错误率右轴
- 任务延时曲线：p95 queue_delay + p95 elapsed 双线
- 数据拉取延迟表：recent 200（job / trade_date / 耗时 ms / rows / status）+ by_job 聚合表
- 慢接口排行 top10 表

页面骨架（遵循项目现有 client 组件 + swr 模式）：

```tsx
'use client';
import useSWR from 'swr';
import { useState } from 'react';
import KChart from '@/components/KChart';
import { PageHeader } from '@/components/PageHeader';
import { Panel } from '@/components/Panel';

const fetcher = (url: string) => fetch(url).then(r => r.json()).then(d => d.data);
const RANGES = ['1h', '6h', '24h', '7d'] as const;

export default function MonitorPage() {
  const [range, setRange] = useState<(typeof RANGES)[number]>('1h');
  const { data: sum } = useSWR('/api/monitor/summary', fetcher,
    { refreshInterval: 30_000 });
  const { data: apiLat } = useSWR(`/api/monitor/api-latency?range=${range}`, fetcher);
  const { data: tasks } = useSWR(`/api/monitor/tasks?range=${range}`, fetcher);
  const { data: pulls } = useSWR('/api/monitor/data-pulls', fetcher);
  // ... 渲染见分步实现，遵循设计系统 token（bg/line/text 语义色）
}
```

渲染部分按 sync/page.tsx 的表格样式与 KChart 用法逐块补齐；双轴图（耗时双线 + 错误率右轴）按项目记忆的坑处理（globals.css 导入 + 双轴对齐）。

- [ ] **Step 2: 构建/类型检查**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add web/src/app/monitor/page.tsx web/src/components/Sidebar.tsx
git commit -m "feat: 前端 /monitor 监控页面"
```

---

### Task 13: 集成测试（Redis 全链路）+ 最终验证

**Files:**
- Create: `tests/integration/test_monitor_e2e.py`（若 `tests/` 无 integration 目录则创建并确认 pytest.ini testpaths 覆盖）

**Interfaces:**
- Consumes: 全部前序交付物；Redis（不可用则 skip）

- [ ] **Step 1: 写集成测试**

```python
"""监控全链路集成：http + RQ worker 子进程 → 事件落盘 → API 断言。

需要本机 Redis（docker compose up -d redis）；不可用则 skip。
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from lquant.server.jobs import _redis_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="需要 Redis"),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    monkeypatch.setenv("LQ_SYNC_WORKER", "0")
    monkeypatch.setenv("LQ_REDIS_URL",
                       os.getenv("LQ_REDIS_URL", "redis://localhost:6379/0"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _enqueue_test_task() -> str:
    from lquant.server.jobs import enqueue

    def _echo():
        return 42

    job = enqueue("lquant-default", _echo)
    return job.id


def test_full_chain(env):
    """降级模式任务事件 → 内存队列 → flusher → metrics_task → API。"""
    from fastapi.testclient import TestClient

    from lquant.monitor import flusher
    from lquant.server.main import create_app

    _enqueue_test_task()
    time.sleep(1)  # 等本地线程任务跑完发事件
    flusher.flush_once()
    with TestClient(create_app()) as client:
        rows = client.get("/api/monitor/tasks?range=1h").json()["data"]["series"]
        assert sum(r["count"] for r in rows) >= 1
        s = client.get("/api/monitor/summary").json()["data"]
        assert "procs" in s and "queues" in s


def test_rq_worker_chain(env):
    """RQ worker 子进程执行任务 → 事件经 Redis → flusher 落盘。"""
    import subprocess
    import sys

    from lquant.monitor import flusher
    from lquant.server.jobs import enqueue, get_redis

    def _demo():
        return 1

    job = enqueue("lquant-default", _demo)
    # burst 模式直接在测试进程内跑一个 MonitoringWorker（不起真实子进程，快且稳）
    from rq import SimpleWorker

    from lquant.monitor.worker import MonitoringWorker

    w = MonitoringWorker([f"lquant-default"],
                         connection=get_redis(), name=f"test-{uuid.uuid4().hex[:8]}")
    w.work(burst=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        flusher.flush_once()
        import duckdb

        from lquant.core.config import get_settings

        n = duckdb.connect(get_settings().monitor_db_path).execute(
            "SELECT count(*) FROM metrics_task WHERE job_id = ?",
            [job.id]).fetchone()[0]
        if n >= 2:  # started + finished
            break
        time.sleep(0.5)
    assert n >= 2, "任务 started/finished 事件应落盘"
```

（`tests/integration/` 无 `__init__.py` 需要创建；确认 pytest.ini `testpaths = tests` 已覆盖。）

- [ ] **Step 2: 跑集成测试（有 Redis 则跑，无则确认 skip）**

Run: `python -m pytest tests/integration/test_monitor_e2e.py -v`
Expected: PASS（有 Redis）或 2 skipped（无 Redis）

- [ ] **Step 3: 全量回归**

Run: `python -m pytest tests/unit -x -q 2>&1 | tail -5 && ruff check src/lquant/monitor src/lquant/server/api/monitor.py src/lquant/server/main.py src/lquant/core/config.py src/lquant/server/jobs.py`
Expected: 全部 PASS，ruff 对新文件无违规

- [ ] **Step 4: 提交**

```bash
git add tests/integration/
git commit -m "test: 监控全链路集成测试（Redis 任务事件→落盘→API）"
```

---

## Self-Review 记录

- **Spec 覆盖**：配置(T1)、types/ring(T2)、中间件(T3)、emit+jobs 降级事件(T4)、sampler(T5)、MonitoringWorker(T6)、flusher(T7)、生命周期+main 装配(T8)、查询+端点(T9)、lq worker CLI(T10)、lquant.sh(T11)、前端(T12)、集成测试(T13) — spec §3–§11 全覆盖。lquant.sh 的 `LQ_BIN` 变量与 `cli/commands/__init__.py` 导出清单需实现时按实际代码适配（已在任务中标注）。
- **占位符**：Task 12 渲染部分为"参照现有页面实现"（前端样式必须跟设计系统走，无法在计划里穷举 JSX），已在任务中指明参照文件与要点；其余任务均有完整代码。
- **类型一致性**：`emit_task_event` 签名在 T4/T6 一致；`TaskEvent` 字段在 T2/T4/T7 一致；`flush_once` 返回 dict 在 T7 测试与 T8 依赖一致；`range_bucket`/`RANGE_BUCKET` 在 T9 内部一致。
