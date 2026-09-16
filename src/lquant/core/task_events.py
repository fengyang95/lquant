"""数据任务进度事件总线（进程内）。

发布方是后台线程里的同步代码（ingest 批间进度），订阅方是 SSE 端点所在的
事件循环。publish() 用 loop.call_soon_threadsafe 跨线程投递，不阻塞发布方；
SSE 端点（订阅方）断开时 unsubscribe 清理。
进程内实现：RQ 分支（redis 队列）任务跑在 worker 进程，事件到不了这里 ——
该场景前端回退到轮询（TasksPanel 已有 2s 轮询兜底）。
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

_lock = threading.Lock()
# task_id → [(loop, queue), ...]；loop 是订阅者所在的事件循环
_subs: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
# 每个 task 只保留最新一帧快照，新订阅者连上立刻拿到当前进度（免空白等待）
_last: dict[str, dict] = {}


def publish(task_id: str, event: dict) -> None:
    """后台线程安全：投递最新进度快照给该 task 的所有订阅者。"""
    with _lock:
        _last[task_id] = event
        subs = list(_subs.get(task_id, ()))
    for loop, q in subs:
        def _put(q=q, event=event) -> None:
            if not q.full():
                q.put_nowait(event)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_put)  # 订阅方事件循环已关闭则跳过


def subscribe(task_id: str) -> tuple[asyncio.Queue, callable]:  # type: ignore[valid-type]
    """事件循环线程内调用；返回 (queue, unsubscribe)。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    loop = asyncio.get_running_loop()
    with _lock:
        _subs.setdefault(task_id, []).append((loop, q))
        last = _last.get(task_id)
    if last is not None:
        q.put_nowait(last)
    return q, lambda: _unsubscribe(task_id, q)


def _unsubscribe(task_id: str, q: asyncio.Queue) -> None:
    with _lock:
        subs = _subs.get(task_id, [])
        _subs[task_id] = [s for s in subs if s[1] is not q]
        if not _subs[task_id]:
            _subs.pop(task_id, None)
            _last.pop(task_id, None)  # 无订阅者后清掉快照，防长驻泄漏
