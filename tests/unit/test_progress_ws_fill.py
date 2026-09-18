"""server/progress.py + server/ws.py 覆盖补齐：Redis 分支、WS 兜底链与断连收尾。

WS 用例直接以协程方式调 handler（FakeWS 记录 send_json），避开 TestClient
在「断连收尾」路径上的不确定性。
"""
from __future__ import annotations

import asyncio
import types

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from lquant.server import progress

# ---------------------------------------------------------------- progress

class FakeRedis:
    def __init__(self, fail=False):
        self.kv: dict = {}
        self.hashes: dict = {}
        self._fail = fail

    def _boom(self):
        if self._fail:
            raise RuntimeError("redis down")

    def set(self, key, value, ex=None):
        self._boom()
        self.kv[key] = value

    def get(self, key):
        self._boom()
        return self.kv.get(key)

    def hset(self, key, field, value):
        self._boom()
        self.hashes.setdefault(key, {})[field] = value

    def expire(self, key, ttl):
        self._boom()

    def hget(self, key, field):
        self._boom()
        return self.hashes.get(key, {}).get(field)


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(progress, "_redis_available", lambda: True)
    r = FakeRedis()
    monkeypatch.setattr(progress, "_get_redis", lambda: r)
    progress._PROGRESS.clear()
    progress._NAMES.clear()
    yield r
    progress._PROGRESS.clear()
    progress._NAMES.clear()


def test_set_get_progress_via_redis(fake_redis) -> None:
    progress.set_progress("j1", done=1, total=3, phase="p")
    assert progress.get_progress("j1")["done"] == 1
    assert "lquant:progress:j1" in fake_redis.kv


def test_progress_redis_write_failure_swallowed(fake_redis) -> None:
    fake_redis._fail = True
    progress.set_progress("j1", done=1, total=3, phase="p")  # 不抛
    assert progress.get_progress("j1")["done"] == 1  # 进程内注册表兜底


def test_get_progress_redis_error_falls_back_to_local(fake_redis) -> None:
    progress.set_progress("j1", done=2, total=4, phase="p")
    fake_redis._fail = True
    assert progress.get_progress("j1")["done"] == 2


def test_get_progress_redis_returns_none_falls_back(fake_redis) -> None:
    progress.set_progress("j1", done=2, total=4, phase="p")
    fake_redis.kv.clear()  # Redis 无该 key → 回落进程内
    assert progress.get_progress("j1")["done"] == 2


def test_get_progress_redis_non_dict_ignored(fake_redis, monkeypatch) -> None:
    import json as _json

    fake_redis.kv["lquant:progress:j9"] = _json.dumps([1, 2])
    assert progress.get_progress("j9") is None


def test_set_job_name_redis_and_bytes(fake_redis) -> None:
    progress.set_job_name("j1", "因子评价")
    assert fake_redis.hashes["lquant:job:names"]["j1"] == "因子评价"
    # 进程内命中
    assert progress.get_job_name("j1") == "因子评价"
    # 进程内未命中 → Redis 侧 str
    fake_redis.hashes["lquant:job:names"]["j2"] = "日线同步"
    assert progress.get_job_name("j2") == "日线同步"
    # Redis 侧 bytes → decode
    fake_redis.hashes["lquant:job:names"]["j3"] = b"backfill"
    assert progress.get_job_name("j3") == "backfill"
    # Redis 侧空值 → None
    assert progress.get_job_name("j4") is None


def test_set_job_name_redis_failure_swallowed(fake_redis) -> None:
    fake_redis._fail = True
    progress.set_job_name("j1", "因子评价")  # 不抛
    assert progress.get_job_name("j1") == "因子评价"


def test_get_job_name_redis_failure_returns_none(fake_redis) -> None:
    fake_redis._fail = True
    assert progress.get_job_name("nope") is None


def test_job_name_eviction_when_over_capacity(fake_redis) -> None:
    for i in range(progress._PROGRESS_MAX + 5):
        progress.set_job_name(f"j{i}", "n")
    assert len(progress._NAMES) <= progress._PROGRESS_MAX


def test_progress_partial_update_keeps_old_fields(fake_redis) -> None:
    """None 字段保留旧值；首次写入补零。"""
    progress.set_progress("j1", done=1, total=10, phase="a", message="m1")
    progress.set_progress("j1", done=2)  # 其余字段缺省 → 保留旧值
    p = progress.get_progress("j1")
    assert p["done"] == 2 and p["total"] == 10
    assert p["phase"] == "a" and p["message"] == "m1"


def test_get_redis_delegates_to_jobs(monkeypatch) -> None:
    """_get_redis 转发 jobs.get_redis（worker 进程写入场景）。"""
    sentinel = object()
    monkeypatch.setattr("lquant.server.jobs.get_redis", lambda: sentinel)
    assert progress._get_redis() is sentinel


# ---------------------------------------------------------------- ws

class FakeWS:
    def __init__(self, fail_on_send: int | None = None):
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False
        self._sends = 0
        self._fail_on_send = fail_on_send

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self._sends += 1
        if self._fail_on_send is not None and self._sends >= self._fail_on_send:
            raise WebSocketDisconnect(code=1000)
        self.sent.append(payload)

    async def close(self):
        self.closed = True
        self._sends = -10**9  # close 后再 send 也算失败

    @property
    def client_state(self):
        return WebSocketState.CONNECTED if not self.closed else WebSocketState.DISCONNECTED


class FakeJob:
    def __init__(self, status="started", result=None, error=None):
        self._status = status
        self._result = result
        self._error = error

    def get_status(self):
        return self._status

    @property
    def result(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    @property
    def error(self):
        if isinstance(self._error, Exception):
            raise self._error
        return self._error


def _run(coro):
    return asyncio.run(coro)


def test_ws_job_finished_with_progress(monkeypatch) -> None:
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job", lambda jid: FakeJob("finished", result={"rows": 3}))
    monkeypatch.setattr(ws_mod, "get_progress",
                        lambda jid: {"done": 3, "total": 3, "phase": "x"})
    f = FakeWS()
    _run(ws_mod.job_progress(f, "j1"))
    assert f.sent[-1]["status"] == "finished"
    assert f.sent[-1]["result"] == {"rows": 3}
    assert f.sent[-1]["progress"] == {"done": 3, "total": 3, "phase": "x"}
    assert f.closed


def test_ws_job_failed_error_payload(monkeypatch) -> None:
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job", lambda jid: FakeJob("failed", error="ValueError: 炸"))
    f = FakeWS()
    _run(ws_mod.job_progress(f, "j1"))
    assert f.sent[-1]["status"] == "failed" and f.sent[-1]["error"] == "ValueError: 炸"


def test_ws_job_result_property_raises(monkeypatch) -> None:
    """result / error 属性抛异常 → _safe_result/_safe_error 吞掉返 None。"""
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job",
                        lambda jid: FakeJob("finished", result=RuntimeError("boom")))
    f = FakeWS()
    _run(ws_mod.job_progress(f, "j1"))
    assert f.sent[-1]["result"] is None

    monkeypatch.setattr(ws_mod, "get_job",
                        lambda jid: FakeJob("failed", error=RuntimeError("boom")))
    f2 = FakeWS()
    _run(ws_mod.job_progress(f2, "j1"))
    assert f2.sent[-1]["error"] is None


def test_ws_job_data_task_fallback(monkeypatch) -> None:
    """data_task 兜底链：running → 继续，failed → 终态。"""
    from lquant.server import ws as ws_mod

    tasks = [
        {"status": "running", "done_symbols": 3, "total_symbols": 10, "phase": "拉取"},
        {"status": "failed", "done_symbols": 5, "total_symbols": 10, "phase": "x"},
    ]
    monkeypatch.setattr(ws_mod, "get_job", lambda jid: None)
    monkeypatch.setattr(ws_mod, "get_task", lambda tid: tasks.pop(0))
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    f = FakeWS()
    _run(ws_mod.job_progress(f, "t1"))
    assert f.sent[0]["status"] == "running" and f.sent[0]["done"] is False
    assert f.sent[0]["progress"] == {"done": 3, "total": 10, "phase": "拉取"}
    assert f.sent[1]["status"] == "failed" and f.sent[1]["done"] is True
    assert f.closed


def test_ws_job_job_record_fallback(monkeypatch) -> None:
    """job 队列与 data_task 都查不到 → job_record 终态帧。"""
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job", lambda jid: None)
    monkeypatch.setattr(ws_mod, "get_task", lambda tid: None)
    monkeypatch.setattr(ws_mod, "get_job_record",
                        lambda jid: {"status": "failed", "error": "ValueError: x"})
    f = FakeWS()
    _run(ws_mod.job_progress(f, "j1"))
    assert f.sent[-1]["status"] == "failed" and f.sent[-1]["done"] is True


def test_ws_job_job_record_non_terminal_keeps_looking(monkeypatch) -> None:
    """job_record 非终态（started 遗留）→ 发 not_found 终结连接。"""
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job", lambda jid: None)
    monkeypatch.setattr(ws_mod, "get_task", lambda tid: None)
    monkeypatch.setattr(ws_mod, "get_job_record", lambda jid: {"status": "started", "error": None})
    f = FakeWS()
    _run(ws_mod.job_progress(f, "j1"))
    assert f.sent[-1]["status"] == "not_found"


def test_ws_job_send_disconnect(monkeypatch) -> None:
    """发送时客户端断开 → WebSocketDisconnect 分支 + finally 收尾。"""
    from lquant.server import ws as ws_mod

    monkeypatch.setattr(ws_mod, "get_job", lambda jid: FakeJob("started"))
    monkeypatch.setattr(ws_mod, "get_progress", lambda jid: None)
    f = FakeWS(fail_on_send=2)
    _run(ws_mod.job_progress(f, "j1"))
    assert f.closed and len(f.sent) == 1


async def _fast_sleep(_s):  # 替换 asyncio.sleep：立即返回，加速轮询循环
    return None


def test_ws_ask_stream_forwards_events(monkeypatch) -> None:
    from lquant.server import ws as ws_mod

    events = [types.SimpleNamespace(model_dump=lambda: {"type": "token", "v": 1}),
              types.SimpleNamespace(model_dump=lambda: {"type": "done"})]

    class FakeQueue:
        def __init__(self):
            self._i = 0

        async def get(self):
            try:
                ev = events[self._i]
                self._i += 1
                return ev
            except IndexError:
                raise WebSocketDisconnect(code=1000) from None

    class FakeBus:
        async def subscribe(self, sid):
            return FakeQueue()

        async def unsubscribe(self, sid, q):
            bus_calls.append("unsub")

    bus_calls: list = []
    monkeypatch.setattr(ws_mod, "get_event_bus", lambda: FakeBus())
    f = FakeWS()
    _run(ws_mod.ask_stream(f, "s1"))
    assert f.sent == [{"type": "token", "v": 1}, {"type": "done"}]
    assert bus_calls == ["unsub"]
    assert f.closed


def test_ws_ask_stream_runtime_error_swallowed(monkeypatch) -> None:
    from lquant.server import ws as ws_mod

    class FakeQueue:
        async def get(self):
            raise RuntimeError("connection closed")

    class FakeBus:
        async def subscribe(self, sid):
            return FakeQueue()

        async def unsubscribe(self, sid, q):
            bus_calls.append("unsub")

    bus_calls: list = []
    monkeypatch.setattr(ws_mod, "get_event_bus", lambda: FakeBus())
    f = FakeWS()
    _run(ws_mod.ask_stream(f, "s1"))
    assert bus_calls == ["unsub"] and f.closed


def test_ws_market_ticks_success_then_disconnect(monkeypatch) -> None:
    from lquant.market import ticks as ticks_mod
    from lquant.server import ws as ws_mod

    rows = [{"symbol": "600519", "price": 1.0, "ts": 123}]
    calls = {"n": 0}

    def fake_fetch(symbols):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(ticks_mod, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    f = FakeWS(fail_on_send=2)
    _run(ws_mod.market_ticks(f, symbols="600519"))
    assert f.sent[0]["available"] is True
    assert f.sent[0]["ts"] == 123
    assert f.closed


def test_ws_market_ticks_empty_symbols(monkeypatch) -> None:
    from lquant.server import ws as ws_mod

    f = FakeWS()
    _run(ws_mod.market_ticks(f, symbols=""))
    assert f.sent == [{"available": False, "error": "symbols 为空", "quotes": []}]
    assert f.closed


def test_ws_market_ticks_three_failures_break(monkeypatch) -> None:
    """连续 3 次 TicksError → 掐断连接（恢复风暴兜底）。"""
    from lquant.market import ticks as ticks_mod
    from lquant.server import ws as ws_mod

    calls = {"n": 0}

    def fake_fetch(symbols):
        calls["n"] += 1
        raise ticks_mod.TicksError("no source")

    monkeypatch.setattr(ticks_mod, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    f = FakeWS()
    _run(ws_mod.market_ticks(f, symbols="600519"))
    assert calls["n"] == 3
    assert len(f.sent) == 3 and all(m["available"] is False for m in f.sent)
    assert f.closed


def test_ws_market_ticks_recovery_after_transient_failure(monkeypatch) -> None:
    """瞬时 1 次失败不掐断：随后成功继续推（恢复风暴语义）。"""
    from lquant.market import ticks as ticks_mod
    from lquant.server import ws as ws_mod

    calls = {"n": 0}

    def fake_fetch(symbols):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ticks_mod.TicksError("flaky")
        return [{"symbol": "600519", "ts": 1}]

    monkeypatch.setattr(ticks_mod, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    f = FakeWS(fail_on_send=4)
    _run(ws_mod.market_ticks(f, symbols="600519"))
    assert [m["available"] for m in f.sent] == [False, True, True]
    assert f.closed


def test_ws_market_ticks_disconnect_from_send(monkeypatch) -> None:
    from lquant.market import ticks as ticks_mod
    from lquant.server import ws as ws_mod

    def fake_fetch(symbols):
        return [{"symbol": "600519", "ts": 1}]

    monkeypatch.setattr(ticks_mod, "fetch_quotes", fake_fetch)
    f = FakeWS(fail_on_send=1)
    _run(ws_mod.market_ticks(f, symbols="600519"))
    assert f.sent == [] and f.closed
