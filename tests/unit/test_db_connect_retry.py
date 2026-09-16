"""`_connect` 对跨进程 DuckDB 文件锁冲突的短重试。

回归点：外部脚本（如 validate_accuracy.py）长期持有 lquant.duckdb 文件锁时，
服务端每次 `duckdb.connect()` 直接 IOException —— /data 页所有接口瞬时 500、
sync worker 连环报错。给 _connect 加带退避的短重试后，短暂锁重叠被吸收。
"""
from __future__ import annotations

import duckdb
import pytest

from lquant.core import db

_CONNED = object()


def _flake(n_fail: int):
    """前 n_fail 次抛锁冲突，之后成功。"""
    state = {"n": 0}

    def fake_connect(path: str):
        if state["n"] < n_fail:
            state["n"] += 1
            raise duckdb.IOException("Could not set lock on file ...")
        return _CONNED

    return fake_connect, state


@pytest.fixture(autouse=True)
def _no_env_delay(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)


def test_connect_retries_then_succeeds(monkeypatch):
    fake, state = _flake(2)
    monkeypatch.setattr(db.duckdb, "connect", fake)
    assert db._connect() is _CONNED
    assert state["n"] == 2


def test_connect_gives_up_after_max(monkeypatch):
    fake, state = _flake(10**6)
    monkeypatch.setattr(db.duckdb, "connect", fake)
    with pytest.raises(duckdb.IOException):
        db._connect()
    assert state["n"] == db._CONNECT_ATTEMPTS


def test_connect_no_retry_on_other_errors(monkeypatch):
    """非锁冲突的 IOException（如磁盘满）不应重试。"""
    calls = {"n": 0}

    def fake_connect(path: str):
        calls["n"] += 1
        raise duckdb.IOException("Disk full")

    monkeypatch.setattr(db.duckdb, "connect", fake_connect)
    with pytest.raises(duckdb.IOException):
        db._connect()
    assert calls["n"] == 1
