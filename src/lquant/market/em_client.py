"""东财请求限流代理。

必须一开始就走这里，不要先裸奔再补 —— 封 IP 是头号风险。
"""
from __future__ import annotations

import time
from functools import lru_cache

from lquant.data.ratelimit import TokenBucket


@lru_cache(maxsize=4)
def _bucket(qps: float) -> TokenBucket:
    return TokenBucket(qps)


def em_get(url: str, qps: float = 3.0, **kw):
    import requests

    _bucket(qps).acquire()
    kw.setdefault("timeout", 15)
    kw.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    last: Exception | None = None
    for i in range(3):
        try:
            return requests.get(url, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"请求失败: {url} -> {last}")
