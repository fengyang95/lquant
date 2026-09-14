"""线程安全采集缓冲：API 环缓冲 + 错误环缓冲 + 本地任务事件队列（包级单例）。"""
from __future__ import annotations

import threading
from collections import deque
from typing import TypeVar

from lquant.monitor.types import ApiErrorPoint, ApiMetricPoint, TaskEvent

T = TypeVar("T")


class ApiRing[T]:
    """固定容量环缓冲；snapshot 返回不可变 tuple，drain 原子取走并清空。"""

    def __init__(self, maxlen: int = 5000) -> None:
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._items: deque[T] = deque(maxlen=maxlen)

    def append(self, point: T) -> None:
        with self._lock:
            self._items.append(point)

    def snapshot(self) -> tuple[T, ...]:
        with self._lock:
            return tuple(self._items)

    def drain(self) -> tuple[T, ...]:
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


api_ring = ApiRing[ApiMetricPoint]()
error_ring = ApiRing[ApiErrorPoint]()
local_events = TaskEventQueue()
