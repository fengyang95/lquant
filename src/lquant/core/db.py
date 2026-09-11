"""DuckDB 连接管理。

硬约束：DuckDB 只允许单个写进程。
所有写操作收敛到 writer()，读用 readonly()，两者不混用。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

from lquant.core.config import get_settings

# 同进程多线程同时首次 duckdb.connect() 同一文件会撞 instance cache
# （Unique file handle conflict），建连阶段串行化
_connect_lock = threading.Lock()


def _connect() -> duckdb.DuckDBPyConnection:
    with _connect_lock:
        return duckdb.connect(_path())


def _path() -> str:
    p = get_settings().duckdb_path
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def writer():
    """写连接 —— 同一时刻只允许一个进程持有。"""
    con = _connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


@contextmanager
def reader():
    """只读语义的连接 —— 可并发。

    注意：不能加 read_only=True。DuckDB 的实例缓存按路径+配置区分，
    read_only 与读写会被当成两个独立实例，互相看不到对方已提交的写
    （症状：写完立刻读不到 / 同进程读写不一致）。统一用同一配置，
    让所有连接共享同一实例，由 DuckDB 内部锁保证并发安全。
    """
    con = _connect()
    try:
        yield con
    finally:
        con.close()
