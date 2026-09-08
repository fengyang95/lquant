"""令牌桶限流。东财封 IP 是头号风险，请求必须一开始就走限流。"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, qps: float, burst: int | None = None) -> None:
        self.qps = max(qps, 0.01)
        self.capacity = burst or max(int(qps), 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1) -> None:
        if self.qps >= 100:      # 视为不限流
            return
        with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.qps
                )
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                time.sleep((n - self.tokens) / self.qps)
