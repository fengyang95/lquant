"""令牌桶限流 + 源级单飞锁。

两者**正交**，缺一不可：

- :class:`TokenBucket`（限流）管**速率** —— 每秒最多几个请求；
- :func:`source_lock`（单飞）管**并发会话** —— 同一时刻只允许一个会话在打这个源。

为什么必须分开：BaoStock 的黑名单错误码 ``10001011`` 触发条件之一是
**并发连接**（还有日请求 >5 万、扫太快）。限流再稳也拦不住「两个 worker
同时 login」—— 那是一个速率完全合规、但源站直接封你的场景。

锁是**进程内 RLock + 跨进程 flock** 双层：单机多 worker 靠 flock 互斥，
同进程多线程靠 RLock。为支持嵌套，flock 只在**进程内首次进入**时获取
（``_depth`` 计数），否则同一进程的第二次获取会自己锁死自己。
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

#: 等到多久还没拿到锁就放弃（秒）。极端值比「静默并发」安全：宁可报错。
DEFAULT_LOCK_TIMEOUT_SEC = 600.0

#: 等锁超过这个时长打一条 warning —— 让「任务为什么卡住」可见
_WARN_AFTER_SEC = 30.0

_rlocks: dict[str, threading.RLock] = {}
_depth: dict[str, int] = {}
_fds: dict[str, int] = {}
_meta = threading.Lock()


def _lock_path(name: str) -> Path:
    from lquant.core.config import get_settings

    d = Path(get_settings().cache_dir) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.lock"


def _acquire_flock(name: str, timeout: float) -> None:
    """跨进程排他锁（非阻塞轮询，带超时）。无 fcntl 的平台自动降级为仅进程内。"""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return
    path = _lock_path(name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    start = time.monotonic()
    warned = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if time.monotonic() - start > 1.0:
                logger.info(f"源锁 {name}: 已获得（等待 {time.monotonic() - start:.1f}s）")
            _fds[name] = fd
            return
        except OSError:
            waited = time.monotonic() - start
            if waited >= timeout:
                os.close(fd)
                raise TimeoutError(
                    f"等待源锁 {name} 超过 {timeout:.0f}s：另一个进程正在拉同一数据源。"
                    "并发会话会触发源站封禁（BaoStock 10001011），因此这里选择失败"
                    "而不是硬闯") from None
            if waited > _WARN_AFTER_SEC and not warned:
                warned = True
                logger.warning(f"源锁 {name}: 已被占用并等待 >{_WARN_AFTER_SEC:.0f}s，"
                               "仍在排队（并发会话会被源站封禁）")
            time.sleep(0.2)


def _release_flock(name: str) -> None:
    fd = _fds.pop(name, None)
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except ImportError:  # pragma: no cover - Windows
        pass
    finally:
        os.close(fd)


@contextmanager
def source_lock(name: str, timeout: float = DEFAULT_LOCK_TIMEOUT_SEC) -> Iterator[None]:
    """同一数据源的并发会话互斥。嵌套安全（同进程只真正加锁一次）。"""
    rl = _rlocks.setdefault(name, threading.RLock())
    if not rl.acquire(timeout=timeout):
        raise TimeoutError(f"等待源锁 {name} 超过 {timeout:.0f}s（同进程内被占用）")
    try:
        with _meta:
            _depth[name] = _depth.get(name, 0) + 1
            first = _depth[name] == 1
        if first:
            _acquire_flock(name, timeout)
        try:
            yield
        finally:
            with _meta:
                _depth[name] -= 1
                last = _depth[name] <= 0
                if last:
                    _depth.pop(name, None)
            if last:
                _release_flock(name)
    finally:
        rl.release()


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
        if n > self.capacity:
            # tokens 被封顶在 capacity，n>capacity 永远等不到 → 此前会死循环
            raise ValueError(
                f"acquire({n}) 超过桶容量 {self.capacity}，请调大 burst 或分批获取")
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
