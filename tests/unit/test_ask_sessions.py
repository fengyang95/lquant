"""SessionStore：SQLite 持久化行为。"""
import aiosqlite
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


async def test_append_delta_rejects_wrong_session(store):
    """session_id 不匹配时不修改任何行（防跨会话写穿）。"""
    ses = await store.create(None)
    other = await store.create(None)
    m = await store.add_message(ses.id, "assistant", "原内容")
    await store.append_assistant_delta(other.id, m.id, "恶意追加")
    got = await store.messages(ses.id)
    assert got[0].content == "原内容"


# ---- 会话级 agent 能力配置（provider / skills / mcp_tools） ----------------


async def test_new_session_agent_config_defaults_empty(store):
    """未指定能力配置的会话（含 A2A 建的）落空 dict，由调用方回退到全局默认。"""
    ses = await store.create(None)
    assert ses.agent_config == {}
    assert await store.get_agent_config(ses.id) == {}


async def test_agent_config_roundtrip(store):
    cfg = {"provider": "codex", "skills": ["factor-mining"], "mcp_tools": ["get_quotes"]}
    ses = await store.create({"symbol": "600519"}, agent_config=cfg)
    assert ses.agent_config == cfg
    assert await store.get_agent_config(ses.id) == cfg
    # get/list 也要带上，前端据此渲染已锁定的能力 chips
    assert (await store.get(ses.id)).agent_config == cfg
    assert (await store.list())[0].agent_config == cfg


async def test_set_agent_config(store):
    ses = await store.create(None)
    await store.set_agent_config(ses.id, {"provider": "mock"})
    assert await store.get_agent_config(ses.id) == {"provider": "mock"}


async def test_get_agent_config_missing_session_returns_empty(store):
    assert await store.get_agent_config("nope") == {}


async def test_migrate_adds_agent_config_to_legacy_db(tmp_path):
    """老库（有 claude_session_id、无 agent_config_json）打开后自动补列。

    这条专门盯住 `_migrate` 的**逐列独立** try/except：两列写在同一个 try 里时，
    第一列抛 duplicate 会直接跳 except，第二列永远补不上 —— 而这一档老库
    恰恰是第一列已存在、第二列缺失。
    """
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE ask_sessions ("
            " id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新会话',"
            " context_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,"
            " claude_session_id TEXT NOT NULL DEFAULT '')")
        await db.execute(
            "INSERT INTO ask_sessions (id, created_at) VALUES ('old1', '2026-01-01T00:00:00Z')")
        await db.commit()

    s = SessionStore(str(path))
    try:
        assert await s.get_agent_config("old1") == {}
        await s.set_agent_config("old1", {"provider": "codex"})
        assert await s.get_agent_config("old1") == {"provider": "codex"}
        assert (await s.get("old1")).agent_config == {"provider": "codex"}
    finally:
        await s.close()
