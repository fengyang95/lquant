"""子进程超时看门狗。

BaoStock 批量连续请求会静默挂起 —— 实测 20 只串行跑 12 分钟无返回，
且 socket.setdefaulttimeout(15) 完全救不回来（它用自己的阻塞 socket）。
唯一可靠办法：放进子进程，超时就杀。

实现要点（曾踩坑）：必须**先读队列再等进程退出**。mp.Queue 的载荷超过
管道缓冲（macOS ~64KB）时，子进程的 feeder 线程要等父进程读走才能写完；
若父进程先 join()，子进程永远退不出来 —— join 超时被误判为「源站静默
挂起」并强杀，而其实 fn 早就正常返回了（全市场清单 7373 行必现）。
"""

from __future__ import annotations

import multiprocessing as mp
import queue as _queue
import time
from collections.abc import Callable
from typing import Any

from lquant.core.config import get_settings

# 进程已退出后，队列里残余数据的最后等待窗口（feeder 刷盘收尾）
_DRAIN_GRACE_SEC = 2.0


def _run(
    fn: Callable[..., Any], q: mp.Queue, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    try:
        q.put(("ok", fn(*args, **kwargs)))
    except BaseException as e:  # noqa: BLE001
        q.put(("err", f"{type(e).__name__}: {e}"))


def _source_of(fn: Callable[..., Any]) -> str:
    """从被调函数推断数据源名（``lquant.data.providers.baostock`` → ``baostock``）。

    Provider 的网络调用都是模块级函数（子进程可 pickle 的要求），所以模块名就是
    最可靠的源标识；显式传 ``source=`` 可以覆盖。
    """
    mod = getattr(fn, "__module__", "") or ""
    return mod.rsplit(".", 1)[-1] or "unknown"


def run_with_watchdog(
    fn: Callable[..., Any],
    *args: Any,
    timeout: int | None = None,
    source: str | None = None,
    **kwargs: Any,
) -> Any:
    """在子进程执行 fn，超过 timeout 秒则杀掉并抛 TimeoutError。

    整段执行被**源级单飞锁**包住：限流管速率，它管并发会话 —— BaoStock 的
    黑名单错误码 10001011 明确把「并发连接」列为触发条件，两个 worker 各自
    限流合规却同时 login 一样会被封。锁按源名（模块名）区分，嵌套安全。
    """
    from lquant.data.ratelimit import source_lock

    with source_lock(source or _source_of(fn)):
        return _run_with_watchdog(fn, *args, timeout=timeout, **kwargs)


def _run_with_watchdog(
    fn: Callable[..., Any],
    *args: Any,
    timeout: int | None = None,
    **kwargs: Any,
) -> Any:
    timeout = timeout or get_settings().ingest_watchdog_sec
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_run, args=(fn, q, args, kwargs), daemon=True)
    p.start()

    deadline = time.monotonic() + timeout
    status: str | None = None
    payload: Any = None
    crashed = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status, payload = q.get(timeout=min(0.5, remaining))
            break
        except _queue.Empty:
            if not p.is_alive():
                # 进程已退出：结果要么已入队（等 feeder 刷完），要么永远不会来。
                # 先短暂排空再判异常退出，避免 feeder 尚未刷完被误判。
                try:
                    status, payload = q.get(timeout=_DRAIN_GRACE_SEC)
                except _queue.Empty:
                    status = None
                    crashed = True
                break

    if crashed:
        raise RuntimeError(f"{getattr(fn, '__name__', fn)} 子进程异常退出，无返回")
    if status is None:
        p.kill()
        p.join(5)
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} 超过 {timeout}s 未返回（源站静默挂起）")
    p.join(5)
    if p.is_alive():  # 结果已到手，进程收尾卡住不再影响调用方
        p.kill()
        p.join(5)
    if status == "err":
        raise RuntimeError(payload)
    return payload
