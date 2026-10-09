"""SessionStore：SQLite 持久化行为。"""
import aiosqlite
import pytest

from lquant.agent.sessions import SessionStore, render_briefing


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


# ---- 另开会话：标题、删旧答案、上下文简报 ---------------------------------


async def test_create_accepts_title(store):
    """fork 时给新会话一个带来源的标题；空白标题回退默认，不落一个空标题。"""
    ses = await store.create(None, {"provider": "codex"}, title="  大盘 → codex  ")
    assert ses.title == "大盘 → codex"
    assert (await store.get(ses.id)).title == "大盘 → codex"
    assert (await store.create(None, title="   ")).title == "新会话"


async def test_delete_assistant_after_keeps_earlier_turns(store):
    """重新生成：只删目标 user 消息**之后**的 assistant，前面的历史不许动。

    ``(created_at, rowid)`` 的排序口径必须与 ``messages()`` 一致 —— 两处不一致
    会导致「界面上看到的顺序」与「删的是哪几条」对不上，而这条路径删错了
    是**静默**的（用户只发现答案不见了）。
    """
    ses = await store.create(None)
    u1 = await store.add_message(ses.id, "user", "第一问")
    a1 = await store.add_message(ses.id, "assistant", "第一答")
    u2 = await store.add_message(ses.id, "user", "第二问")
    await store.add_message(ses.id, "assistant", "第二答")
    await store.add_message(ses.id, "assistant", "第二答重跑")

    assert await store.delete_assistant_after(ses.id, u2.id) == 2
    left = [m.id for m in await store.messages(ses.id)]
    assert left == [u1.id, a1.id, u2.id]
    # 第二问后面那两条 assistant 都没了；再删一次是空操作（幂等）
    assert await store.delete_assistant_after(ses.id, u2.id) == 0

    assert await store.delete_assistant_after(ses.id, "missing") == 0


async def test_delete_assistant_after_ignores_system_rows(store):
    """system 行不是「回答」，删旧答案时不该连它一起抹掉。"""
    ses = await store.create(None)
    u = await store.add_message(ses.id, "user", "问")
    await store.add_message(ses.id, "system", "运行时信息")
    await store.add_message(ses.id, "assistant", "答")
    assert await store.delete_assistant_after(ses.id, u.id) == 1
    assert [m.role for m in await store.messages(ses.id)] == ["user", "system"]


def _msgs(*pairs):
    from lquant.agent.schemas import Message

    return [Message(id=f"m{i}", session_id="s", role=r, content=c)
            for i, (r, c) in enumerate(pairs)]


def test_render_briefing_orders_oldest_first():
    text = render_briefing(_msgs(("user", "第一问"), ("assistant", "第一答"),
                                 ("user", "第二问")))
    assert text == "用户：第一问\n\n助手：第一答\n\n用户：第二问"


def test_render_briefing_keeps_recent_and_drops_oldest():
    """超预算时**从最早的开始丢**：用户接着问的总是最后那几轮。"""
    text = render_briefing(
        _msgs(("user", "很久以前" * 20), ("user", "最近这一问")), max_chars=40)
    assert "最近这一问" in text
    assert "很久以前" not in text


def test_render_briefing_truncates_when_latest_alone_exceeds_budget():
    """最近一条本身就超预算：截尾保留，不能返回空串（那等于没带上下文）。"""
    text = render_briefing(_msgs(("assistant", "前言" + "尾部关键结论")), max_chars=30)
    assert text
    assert text.endswith("尾部关键结论")
    assert len(text) <= 30


def test_render_briefing_skips_system_and_empty():
    """system 是本机运行时噪声（不该转述给别的后端），空 assistant 是占位。"""
    text = render_briefing(_msgs(("system", "MCP 白名单：xxx"),
                                 ("user", "问"), ("assistant", "")))
    assert text == "用户：问"


async def test_add_message_with_preset_id_and_get_message(store):
    """mid 允许预生成：取消路径要能在 INSERT 的 await 之前就登记目标行。"""
    ses = await store.create(None)
    m = await store.add_message(ses.id, "assistant", "", mid="fixed-id-1")
    assert m.id == "fixed-id-1"
    got = await store.get_message(ses.id, "fixed-id-1")
    assert got is not None and got.content == ""
    # 跨会话拿不到（sid 参与匹配）
    other = await store.create(None)
    assert await store.get_message(other.id, "fixed-id-1") is None
    assert await store.get_message(ses.id, "nope") is None


async def test_mark_interrupted_is_idempotent(tmp_path):
    """取消留痕：只要本轮 id 还登记着就补一句，补完即 pop（幂等）。

    「回答已经跑完」这一档由 provider 在收尾时 pop 掉 id 来区分，**不是**
    靠内容非空 —— 流到一半被取消的回答内容也非空，但同样需要这个标记。
    """
    from lquant.agent.mock import MockAgentService

    svc = MockAgentService(SessionStore(str(tmp_path / "ask.db")))
    ses = await svc.create_session(None)
    # 没有登记 id → 空操作
    await svc._mark_interrupted(ses.id)
    # 登记了 id 且内容为空 → 补「（已中断）」
    m = await svc.store.add_message(ses.id, "assistant", "", mid="mid-empty")
    svc._ans_id[ses.id] = "mid-empty"
    await svc._mark_interrupted(ses.id)
    got = await svc.store.get_message(ses.id, m.id)
    assert got.content == "（已中断）"
    assert ses.id not in svc._ans_id
    # 幂等：再调一次不重复补
    await svc._mark_interrupted(ses.id)
    assert (await svc.store.get_message(ses.id, m.id)).content == "（已中断）"
    # 截断到一半的回答也要补
    m2 = await svc.store.add_message(ses.id, "assistant", "（Mock 回答", mid="mid-half")
    svc._ans_id[ses.id] = "mid-half"
    await svc._mark_interrupted(ses.id)
    assert (await svc.store.get_message(ses.id, m2.id)).content == "（Mock 回答（已中断）"


async def test_mark_interrupted_swallows_store_errors(tmp_path):
    """留痕失败不能改写取消语义（不能让取消变成 500）。"""
    from lquant.agent.mock import MockAgentService

    svc = MockAgentService(SessionStore(str(tmp_path / "ask.db")))
    ses = await svc.create_session(None)

    async def boom(*_a, **_k):
        raise RuntimeError("db 挂了")

    svc._ans_id[ses.id] = "whatever"
    svc.store.get_message = boom          # type: ignore[method-assign]
    await svc._mark_interrupted(ses.id)   # 不抛
    assert ses.id not in svc._ans_id
