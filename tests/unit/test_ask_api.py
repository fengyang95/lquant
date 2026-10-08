"""_api/ask 会话 CRUD + 发消息 202。流式走 Task 4 的集成测试。"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from lquant.agent.service import get_agent_service
from lquant.server.api import ask as ask_api
from lquant.server.main import app

# 本模块与「问 AI」其它用例共享同一个 aiosqlite 会话库（<root>/data/ask.db）。
# xdist 多 worker 并发写会撞 sqlite「database is locked」，故整组钉在同一
# worker 按序执行（与 test_task_center 同一套做法）。
pytestmark = pytest.mark.xdist_group("ask_store")


@pytest.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_session_lifecycle(client):
    r = await client.post("/api/ask/sessions", json={"context": {"symbol": "600519"}})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    ses = body["data"]
    assert ses["context"]["symbol"] == "600519"

    r = await client.get("/api/ask/sessions")
    assert r.status_code == 200
    assert any(s["id"] == ses["id"] for s in r.json()["data"])

    r = await client.delete(f"/api/ask/sessions/{ses['id']}")
    assert r.status_code == 200


async def test_send_message_returns_202(client):
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]
    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "大盘怎么样"})
    assert r.status_code == 202
    assert r.json()["data"]["user_message"]["role"] == "user"
    # 稍等 mock 跑完再清理
    await asyncio.sleep(1.0)
    r = await client.get(f"/api/ask/sessions/{sid}/messages")
    roles = [m["role"] for m in r.json()["data"]]
    assert "assistant" in roles
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_empty_message_400(client):
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]
    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "  "})
    assert r.status_code == 400
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_cancel_returns_200(client):
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]
    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
    assert r.status_code == 202
    r = await client.post(f"/api/ask/sessions/{sid}/cancel")
    assert r.status_code == 200
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_dragon_tiger_endpoint(client):
    r = await client.get("/api/market/dragon-tiger")
    # 空湖也必须 200 + 列表（与现有 market 端点风格一致：空数据不报错）
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_unknown_session_404(client):
    r = await client.get("/api/ask/sessions/nope/messages")
    assert r.status_code == 404


async def test_user_message_persisted_before_202(client, monkeypatch):
    """user 消息在返回 202 之前就已同步落库 —— 不依赖任何轮询。

    旧实现是「先起任务再轮询 get_messages 等落库」，所以这里把 get_messages
    打断到永远返回空：新实现应当照常 202（不落库的实现在这里会 500）。
    """
    svc = await get_agent_service()
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]

    async def _no_messages(_sid):
        return []

    monkeypatch.setattr(svc, "get_messages", _no_messages)

    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "大盘怎么样"})
    assert r.status_code == 202, r.text
    user = r.json()["data"]["user_message"]
    assert user["role"] == "user" and user["content"] == "大盘怎么样"

    # 确实已落库：直接查 store（绕过被 monkeypatch 的 get_messages）。
    # 后台 agent 与本次断言并发，assistant 消息可能已落 —— 只要求 user 消息在最前。
    msgs = await svc.store.messages(sid)
    assert msgs, "user 消息应已落库"
    assert msgs[0].role == "user" and msgs[0].content == "大盘怎么样"

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_agent_failure_keeps_user_message_and_202(client, monkeypatch):
    """agent 跑挂不该回滚 user 消息、也不该让请求失败（后台异常走事件通道）。"""
    svc = await get_agent_service()
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]

    async def _boom(*_a, **_k):
        raise RuntimeError("agent 炸了")

    monkeypatch.setattr(svc, "send_message", _boom)

    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "大盘怎么样"})
    assert r.status_code == 202, r.text

    await asyncio.sleep(0.2)          # 让后台任务跑完并收敛
    assert not ask_api._tasks, "后台任务应正常收敛，不留孤儿"
    assert sid not in svc._tasks

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_concurrent_message_same_session_409(client, monkeypatch):
    """同会话已有回答在跑 → 409，而不是两条并发互相踩运行时引用。

    回归 D1：运行时引用（任务/子进程）按会话单槽存，没有这道门就会出现
    「/cancel 打到错的任务」「先结束的 pop 掉别人的引用」。
    """
    svc = await get_agent_service()
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]

    async def _hang(sid_, _content, _on_event, user_msg=None):
        svc._claim(sid_)
        try:
            await asyncio.sleep(30)
        finally:
            svc._release(sid_)

    monkeypatch.setattr(svc, "send_message", _hang)

    r1 = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "第一问"})
    assert r1.status_code == 202, r1.text
    for _ in range(100):
        if svc.is_busy(sid):
            break
        await asyncio.sleep(0.01)
    assert svc.is_busy(sid)

    r2 = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "第二问"})
    assert r2.status_code == 409, r2.text
    assert "已有正在执行" in r2.json()["message"]

    # 第二问不该落库（被门挡住，不是「先写后拒」）
    msgs = await svc.store.messages(sid)
    assert [m.content for m in msgs if m.role == "user"] == ["第一问"]

    await client.post(f"/api/ask/sessions/{sid}/cancel")
    await asyncio.sleep(0.2)
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_message_slot_released_after_completion(client):
    """跑完必须释放槽位，否则这个会话之后再也发不出消息。"""
    r = await client.post("/api/ask/sessions", json=None)
    sid = r.json()["data"]["id"]

    r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "第一问"})
    assert r.status_code == 202

    # 槽位是后台任务跑完才释放的，耗时随机器负载浮动：实测恰好在 ~1.5s 释放，
    # 而原来正好 sleep(1.5) 后立刻发第二条 —— 慢一点点就 409（CI 上随机红）。
    # 改成有上限的轮询：断言的是「最终会释放」，不再隐含「机器必须够快」。
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while True:
        r = await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "第二问"})
        if r.status_code != 409:
            break
        assert loop.time() < deadline, f"槽位 20s 内没有释放：{r.text}"
        await asyncio.sleep(0.05)
    assert r.status_code == 202, r.text

    # DELETE 内部会先 cancel 后台任务，所以不用再等第二问跑完。
    await client.delete(f"/api/ask/sessions/{sid}")


# ---- PATCH /sessions/{sid}/config（会话内改 skills / mcp_tools） ------------


async def _listed(client, sid):
    r = await client.get("/api/ask/sessions")
    assert r.status_code == 200
    return next(s for s in r.json()["data"] if s["id"] == sid)


async def test_update_config_skills_persists(client):
    r = await client.post("/api/ask/sessions", json={"provider": "mock"})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config", json={"skills": ["alpha"]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["agent_config"]["skills"] == ["alpha"]
    # 列表接口也读到新值（不是只改了返回体）
    assert (await _listed(client, sid))["agent_config"]["skills"] == ["alpha"]

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_null_skills_means_all(client):
    """JSON null = 全开，必须原样落库成 None，不能变成 []（全不启用）。"""
    r = await client.post("/api/ask/sessions",
                          json={"provider": "mock", "skills": ["alpha"]})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config", json={"skills": None})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["agent_config"]["skills"] is None
    assert (await _listed(client, sid))["agent_config"]["skills"] is None

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_patch_merges_only_given_keys(client):
    """浅合并：改 skills 不动已锁定的 provider 与 mcp_tools。"""
    r = await client.post("/api/ask/sessions",
                          json={"provider": "mock", "mcp_tools": ["get_quotes"]})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config", json={"skills": ["alpha"]})
    assert r.status_code == 200, r.text
    cfg = r.json()["data"]["agent_config"]
    assert cfg == {"provider": "mock", "mcp_tools": ["get_quotes"], "skills": ["alpha"]}

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_rejects_provider_change(client):
    r = await client.post("/api/ask/sessions", json={"provider": "mock"})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config", json={"provider": "codex"})
    assert r.status_code == 400, r.text
    assert "provider" in r.json()["message"]
    # 会话的 provider 没被改
    assert (await _listed(client, sid))["agent_config"]["provider"] == "mock"

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_rejects_unknown_tool(client):
    r = await client.post("/api/ask/sessions", json={"provider": "mock"})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"mcp_tools": ["不存在的工具"]})
    assert r.status_code == 400, r.text
    assert "未知 MCP 工具" in r.json()["message"]

    await client.delete(f"/api/ask/sessions/{sid}")


@pytest.mark.parametrize("body", [{"whatever": 1}, {}])
async def test_update_config_rejects_bad_body(client, body):
    r = await client.post("/api/ask/sessions", json={"provider": "mock"})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config", json=body)
    assert r.status_code == 400, r.text

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_rejects_missing_body(client):
    """body 缺省也应当是 400（「什么都没改」不是合法请求，静默 200 会骗人）。"""
    r = await client.post("/api/ask/sessions", json={"provider": "mock"})
    sid = r.json()["data"]["id"]

    r = await client.patch(f"/api/ask/sessions/{sid}/config")
    assert r.status_code == 400, r.text

    await client.delete(f"/api/ask/sessions/{sid}")


async def test_update_config_unknown_session_404(client):
    r = await client.patch("/api/ask/sessions/nope/config", json={"skills": ["alpha"]})
    assert r.status_code == 404, r.text
    assert "会话不存在" in r.json()["message"]
