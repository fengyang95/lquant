"""SessionStore：SQLite 持久化行为。"""
import pytest

from lquant.agent.sessions import SessionStore


@pytest.fixture()
async def store(tmp_path):
    s = SessionStore(str(tmp_path / "ask.db"))
    yield s
    await s.close()


async def test_create_and_list_session(store):
    ses = await store.create({"symbol": "600519"})
    assert ses.context == {"symbol": "600519"}
    assert ses.title == "新会话"
    all_s = await store.list()
    assert [x.id for x in all_s] == [ses.id]
    assert all_s[0].created_at  # ISO 时间串非空


async def test_get_missing_returns_none(store):
    assert await store.get("nope") is None


async def test_message_roundtrip(store):
    ses = await store.create(None)
    m1 = await store.add_message(ses.id, "user", "茅台怎么样")
    await store.add_message(ses.id, "assistant", "涨停。", tool_calls=[{"name": "get_quote"}])
    got = await store.messages(ses.id)
    assert [m.role for m in got] == ["user", "assistant"]
    assert got[1].tool_calls == [{"name": "get_quote"}]
    assert m1.created_at


async def test_delete_session_cascades_messages(store):
    ses = await store.create(None)
    await store.add_message(ses.id, "user", "hi")
    await store.delete(ses.id)
    assert await store.get(ses.id) is None
    assert await store.messages(ses.id) == []


async def test_streaming_update(store):
    ses = await store.create(None)
    m = await store.add_message(ses.id, "assistant", "", tool_calls=[])
    await store.append_assistant_delta(ses.id, m.id, "你好")
    await store.append_assistant_delta(ses.id, m.id, "，世界")
    got = await store.messages(ses.id)
    assert got[0].content == "你好，世界"
    await store.finish_assistant(ses.id, m.id)
