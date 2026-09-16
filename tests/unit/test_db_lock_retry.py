"""DuckDB 跨进程写锁冲突的容错：连接重试 + API 层 503 语义化映射。

背景：单机多进程（API / sync worker / ad-hoc 脚本）共享一个 lquant.duckdb，
DuckDB 只允许单写进程 —— 别的进程持锁期间，API 任何建连直接抛 IOException，
表现为全站 500（含 /factors/evaluate）。两层修复：
1) core/db._connect 对锁冲突做有界重试（吸收秒级重叠）；
2) 重试耗尽后 API 层把锁冲突映射为 503（可重试语义）而非裸 500。
"""
from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from lquant.core import db


def test_connect_retries_on_lock_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁冲突 IOException 触发重试，第二次成功。"""
    calls = {"n": 0}
    real_connect = duckdb.connect  # monkeypatch 的是共享 duckdb 模块，先存原函数

    def flaky_connect(path: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise duckdb.IOException(
                'Could not set lock on file "x.duckdb": Conflicting lock is held')
        return real_connect(path)

    monkeypatch.setattr(db.duckdb, "connect", flaky_connect)
    monkeypatch.setattr(db, "_CONNECT_RETRY_TOTAL", 0.2)
    con = db._connect()
    con.close()
    assert calls["n"] == 2


def test_connect_gives_up_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """持续锁冲突：超过重试预算后原样抛出（调用方/异常处理层兜底）。"""
    calls = {"n": 0}

    def locked_connect(path: str):
        calls["n"] += 1
        raise duckdb.IOException("Could not set lock on file: Conflicting lock is held")

    monkeypatch.setattr(db.duckdb, "connect", locked_connect)
    monkeypatch.setattr(db, "_CONNECT_RETRY_TOTAL", 0.2)
    with pytest.raises(duckdb.IOException):
        db._connect()
    assert calls["n"] > 1  # 确实重试过


def test_connect_does_not_retry_other_io_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """非锁冲突的 IOException（如文件损坏）不重试，直接抛。"""
    calls = {"n": 0}

    def broken_connect(path: str):
        calls["n"] += 1
        raise duckdb.IOException("database file is corrupted")

    monkeypatch.setattr(db.duckdb, "connect", broken_connect)
    with pytest.raises(duckdb.IOException):
        db._connect()
    assert calls["n"] == 1


def _client(**kw) -> TestClient:
    from lquant.server.main import create_app

    return TestClient(create_app(), **kw)


def test_api_maps_lock_conflict_to_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁冲突在 API 层映射为 503 + 可读消息，而非 500。"""
    def locked_connect(path: str):
        raise duckdb.IOException(
            'IO Error: Could not set lock on file "lquant.duckdb": Conflicting lock')

    monkeypatch.setattr(db.duckdb, "connect", locked_connect)
    monkeypatch.setattr(db, "_CONNECT_RETRY_TOTAL", 0.05)
    c = _client()
    r = c.get("/api/factors")  # 触库端点（factor_def 查询）
    assert r.status_code == 503
    assert "忙" in r.json()["detail"] or "lock" in r.json()["detail"]


def test_api_other_io_errors_still_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """非锁冲突的 IOException 不走 503 映射，保持 500 语义。"""
    def corrupted_connect(path: str):
        raise duckdb.IOException("database file is corrupted")

    monkeypatch.setattr(db.duckdb, "connect", corrupted_connect)
    monkeypatch.setattr(db, "_CONNECT_RETRY_TOTAL", 0.05)
    c = _client(raise_server_exceptions=False)  # 让服务端异常链跑完，观察最终状态码
    r = c.get("/api/factors")
    assert r.status_code == 500
