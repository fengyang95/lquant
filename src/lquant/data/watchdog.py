"""子进程超时看门狗。

BaoStock 批量连续请求会静默挂起 —— 实测 20 只串行跑 12 分钟无返回，
且 socket.setdefaulttimeout(15) 完全救不回来（它用自己的阻塞 socket）。
唯一可靠办法：放进子进程，超时就杀。
"""
from __future__ import annotations

import multiprocessing as mp
from typing import Any, Callable

from lquant.core.config import get_settings


def _run(fn: Callable[..., Any], q: mp.Queue, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    try:
        q.put(("ok", fn(*args, **kwargs)))
    except BaseException as e:  # noqa: BLE001
        q.put(("err", f"{type(e).__name__}: {e}"))


def run_with_watchdog(
    fn: Callable[..., Any],
    *args: Any,
    timeout: int | None = None,
    **kwargs: Any,
) -> Any:
    """在子进程执行 fn，超过 timeout 秒则杀掉并抛 TimeoutError。"""
    timeout = timeout or get_settings().ingest_watchdog_sec
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_run, args=(fn, q, args, kwargs), daemon=True)
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.kill()
        p.join(5)
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} 超过 {timeout}s 未返回（源站静默挂起）")
    if q.empty():
        raise RuntimeError(f"{getattr(fn, '__name__', fn)} 子进程异常退出，无返回")
    status, payload = q.get()
    if status == "err":
        raise RuntimeError(payload)
    return payload
