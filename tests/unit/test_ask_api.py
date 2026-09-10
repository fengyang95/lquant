"""_api/ask 会话 CRUD + 发消息 202。流式走 Task 4 的集成测试。"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from lquant.server.main import app


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
