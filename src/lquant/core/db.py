"""DuckDB 连接管理。

硬约束：DuckDB 只允许单个写进程。
所有写操作收敛到 writer()，读用 reader()，两者不混用。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb

from lquant.core.config import get_settings

# 跨进程文件锁冲突的短重试：外部脚本（回填 CLI、校验脚本）可能长期持有
# lquant.duckdb 的单写锁，此时服务端 duckdb.connect() 直接 IOException，
# 所有依赖库表的接口瞬时 500。带退避的短重试能吸收几秒级的锁重叠；
# 持锁方长期不走时仍会快速失败（指数退避封顶 0.4s）。
# 建连锁：同进程多线程同时首次 duckdb.connect() 同一文件会撞 instance cache
# （Unique file handle conflict），建连阶段串行化。reader() 的建连同样需要它，
# 所以这一层独立于下面的写锁。
_connect_lock = threading.Lock()

# 跨进程写锁冲突的重试预算（秒）。单机多进程共享一个 duckdb 文件，别的进程
# 持锁期间建连直接抛 IOException —— 表现为全站 500（含 /factors/evaluate）。
# 短重叠（秒级）用重试吸收；长时间持锁重试耗尽后原样抛出，由 API 层映射为 503。
_CONNECT_RETRY_TOTAL = 10.0

# 写串行锁：跨进程的单写者由 DuckDB 的 OS 文件锁兜底（第二个写进程会直接报错），
# 但**同进程不同线程**并不受它保护 —— DuckDB 的并发控制是乐观的，两个连接各自
# 对同一行读改写再提交时，后提交的那个会抛
# `TransactionContext Error: Conflict on update!`。
# 本仓库的写路径天然多线程：数据任务在后台线程按批 _progress_update，API 请求
# 线程同时可能 claim_retry 同一行；因子挖掘任务写因子表的同时 API 也在写别的表。
# 所以「同一时刻只有一个写者」不能只靠约定。用 RLock 以容忍同线程内的嵌套
# （writer() 块里嵌 reader() 是明令禁止的，故只需防同线程嵌套 writer）。
#
# 不变量：写事务必须是**同步闭合**的 —— 不得在 async 函数里跨 await 持有
# writer()。RLock 按线程重入，同一事件循环线程上的另一个协程会把锁再拿一次，
# 互斥就失效了。当前 src/ 下 writer() 全部在同步函数内（有测试扫描守着）。
_WRITE_LOCK = threading.RLock()


def _connect() -> duckdb.DuckDBPyConnection:
    deadline = time.monotonic() + _CONNECT_RETRY_TOTAL
    delay = 0.05
    while True:
        try:
            with _connect_lock:
                return duckdb.connect(_path())
        except duckdb.IOException as e:
            # 只重试锁冲突；文件损坏等其它 IO 错误立即抛出
            if "lock" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)  # 锁外睡眠，不阻塞其它线程建连
            delay = min(delay * 2, 1.0)


def _path() -> str:
    p = get_settings().duckdb_path
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def writer():
    """写连接 —— 进程内串行；跨进程由 DuckDB 的 OS 文件锁保证单写。

    锁必须罩住整个事务（含 connect 与 commit）：只锁建连挡不住乐观并发冲突 ——
    「读到旧值 → 算 → 写回」中间那段窗口才是丢更新的地方。
    """
    with _WRITE_LOCK:
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
