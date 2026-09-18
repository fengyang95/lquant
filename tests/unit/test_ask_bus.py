"""ask_bus 内存事件总线单元测试（asyncio_mode=auto）。"""

from __future__ import annotations

from lquant.agent.schemas import AgentEvent
from lquant.server.api.ask_bus import AskEventBus


async def test_publish_no_subscribers():
    bus = AskEventBus()
    # 无订阅者时 publish 不报错
    await bus.publish("s1", AgentEvent(type="done"))


async def test_subscribe_and_receive():
    bus = AskEventBus()
    q = await bus.subscribe("s1")
    ev = AgentEvent(type="assistant_delta", text="hi")
    await bus.publish("s1", ev)
    assert q.get_nowait() is ev


async def test_multiple_subscribers_fanout():
    bus = AskEventBus()
    q1 = await bus.subscribe("s1")
    q2 = await bus.subscribe("s1")
    q3 = await bus.subscribe("s2")
    ev = AgentEvent(type="tool_call", name="f")
    await bus.publish("s1", ev)
    assert q1.get_nowait() is ev
    assert q2.get_nowait() is ev
    assert q3.empty()  # 其他会话不受影响


async def test_unsubscribe_stops_delivery():
    bus = AskEventBus()
    q = await bus.subscribe("s1")
    await bus.unsubscribe("s1", q)
    await bus.publish("s1", AgentEvent(type="done"))
    assert q.empty()


async def test_unsubscribe_unknown_session_is_safe():
    bus = AskEventBus()
    q = await bus.subscribe("s1")
    # 未订阅的 sid / 未注册的 queue 都安全
    await bus.unsubscribe("nope", q)
    await bus.unsubscribe("s1", asyncio_queue())
    await bus.publish("s1", AgentEvent(type="done"))


def asyncio_queue():
    import asyncio

    return asyncio.Queue()


async def test_same_queue_not_duplicated():
    # set 语义：重复 subscribe 同一 queue 只投递一次
    bus = AskEventBus()
    q = await bus.subscribe("s1")
    await bus.subscribe("s1")  # 返回新 queue，但旧 queue 仍单份投递
    ev = AgentEvent(type="done")
    await bus.publish("s1", ev)
    assert q.qsize() == 1
