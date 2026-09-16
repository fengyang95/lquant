"""任务进度事件总线（core/task_events）+ /data/tasks/{id}/events SSE 端点。

发布方模拟后台线程：直接在测试线程 publish()（bus 内部用
loop.call_soon_threadsafe 投递，本身就是跨线程语义）。
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("task_events")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as a_con:
        for stmt in DDL_STATEMENTS:
            a_con.execute(stmt)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_publish_reaches_subscriber():
    from lquant.core import task_events

    async def main() -> dict:
        q, unsub = task_events.subscribe("t1")
        task_events.publish("t1", {"done": 1})
        ev = await asyncio.wait_for(q.get(), timeout=2)
        unsub()
        return ev

    assert asyncio.run(main()) == {"done": 1}


def test_new_subscriber_gets_latest_snapshot():
    """订阅前发布的最新一帧，订阅后立刻拿到（免空白等待）。"""
    from lquant.core import task_events

    async def main() -> dict:
        task_events.publish("t2", {"done": 5})
        q, unsub = task_events.subscribe("t2")
        ev = await asyncio.wait_for(q.get(), timeout=2)
        unsub()
        return ev

    assert asyncio.run(main()) == {"done": 5}


def test_unsubscribe_stops_delivery():
    from lquant.core import task_events

    async def main() -> None:
        q, unsub = task_events.subscribe("t3")
        unsub()
        task_events.publish("t3", {"done": 9})
        assert q.empty()

    asyncio.run(main())


async def test_sse_endpoint_streams_progress(api_env):
    """连上收 snapshot 帧 → progress 帧 → terminal 后流结束。

    注意：用 httpx ASGITransport 而非 TestClient —— 后者的 portal 在
    流式响应挂起等待时不可靠（挂死），ASGITransport 直接在当前事件循环
    跑 app，publish 的 call_soon_threadsafe 才能唤醒 q.get()。
    """
    import httpx

    from lquant.core import task_events
    from lquant.data.ingest.tasks import create_task
    from lquant.server.main import create_app

    task_id = create_task(kind="daily_update", params={"days": 1})["task_id"]
    frames: list[bytes] = []

    async def publish_events() -> None:
        await asyncio.sleep(0.2)
        task_events.publish(task_id, {"task_id": task_id, "done": 3})
        task_events.publish(task_id, {"task_id": task_id, "terminal": True, "done": 10})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        pub = asyncio.create_task(publish_events())
        async with client.stream("GET", f"/api/data/tasks/{task_id}/events") as r:
            assert r.status_code == 200, f"{r.status_code}: {await r.aread()}"
            assert r.headers["content-type"].startswith("text/event-stream")
            async for chunk in r.aiter_bytes():
                frames.append(chunk)
                if b"event: done" in b"".join(frames):
                    break  # 终态帧到达即可收线，不再等心跳
        await asyncio.wait_for(pub, timeout=5)

    raw = b"".join(frames)
    assert b"event: snapshot" in raw
    assert b"event: progress" in raw
    assert b"event: done" in raw


def test_sse_unknown_task_404(client):
    r = client.get("/api/data/tasks/nonexistent/events")
    assert r.status_code == 404
