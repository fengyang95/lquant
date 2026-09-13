"""`writer()` 的进程内串行保证。

回归点：DuckDB 的并发控制是乐观的 —— 两个连接（同进程、不同线程）各自对同一行
读改写再提交时，后提交的那个会抛 `TransactionContext Error: Conflict on update!`。
而本仓库的写路径天然是多线程：数据任务在后台线程按批 `_progress_update`，API 请求
线程同时可能 `claim_retry` 同一行。`writer()` 上是「同一时刻只允许一个写者」纯约定，
于是 `tests/unit/test_task_center.py` 偶发失败（约 1/6 概率）。
给 writer() 加进程内写锁后，这类冲突与丢更新都应消失。
"""
from __future__ import annotations

import threading

import pytest

from lquant.core.config import get_settings
from lquant.core.db import reader, writer


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离（不 patch get_settings 模块属性）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    with writer() as con:
        con.execute("CREATE TABLE t (k INTEGER, v INTEGER)")
        con.execute("INSERT INTO t VALUES (1, 0)")
    yield
    get_settings.cache_clear()


def test_concurrent_read_modify_write_serializes(isolated_db):
    """N 线程对同一行做「读→+1→写」：不得抛并发冲突，也不得丢更新。

    写锁把整段读改写串行化，所以最终值必然精确等于 总轮次 —— 既锁住了
    DuckDB 的 `Conflict on update!`，也锁住了聚合语义（不丢更新）。
    """
    n_threads, rounds = 6, 25
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)      # 尽量让各线程真正重叠
            for _ in range(rounds):
                with writer() as con:
                    cur = con.execute("SELECT v FROM t WHERE k = 1").fetchone()
                    con.execute("UPDATE t SET v = ? WHERE k = 1", [cur[0] + 1])
        except BaseException as e:  # noqa: BLE001 - 收集后统一断言，避免线程内断言丢失
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发写报错（写锁未覆盖？）: {errors[:3]}"
    with reader() as con:
        got = con.execute("SELECT v FROM t WHERE k = 1").fetchone()[0]
    assert got == n_threads * rounds, f"丢更新：期望 {n_threads * rounds}，实得 {got}"


def test_no_write_transaction_held_across_await():
    """不变量：写事务必须同步闭合，不得在 async 函数里跨 await 持有 writer()。

    写锁是 RLock（按线程重入）。同一事件循环线程上若另一个协程在写事务未闭合时
    再进 writer()，会直接把锁重入拿走 —— 互斥失效，DuckDB 的乐观并发冲突又回来了，
    而且是偶发、极难复现的那种。这条不变量靠静态扫描守着。

    注意：只报「块内有 await」的情况。async 处理器里同步写完就闭合是安全的
    （不跨 await 就不会被其它协程插进来），不必一刀切禁止。
    """
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for p in sorted(src_root.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        if "writer(" not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                is_writer = any(
                    isinstance(i.context_expr, ast.Call)
                    and getattr(i.context_expr.func, "id", "") == "writer"
                    for i in node.items)
                if not is_writer:
                    continue
                if any(isinstance(s, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                       for s in ast.walk(node)):
                    offenders.append(f"{p.relative_to(src_root)}:{node.lineno} in {fn.name}()")

    assert not offenders, (
        "写事务跨 await 持有会破坏进程内写互斥，请改为同步函数内闭合：\n  "
        + "\n  ".join(offenders))
