"""会话内控制：会话级运行参数、重新生成、换后端另开会话、运行中列表。

这些都是「不想为一件小事重开会话」的诉求：超时不够 / 它答歪了 / 想换个后端
再问问。用例走真实 HTTP 路径（``/api/ask/...``），因为前端点的是这些入口，
而这一层最容易出的错是「接口收下了但没人读」—— 所以每个写入都配一条
「真的生效了」的断言。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from lquant.agent.service import get_service_for_session
from lquant.server.main import app


@pytest.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _new_session(client, **body) -> str:
    r = await client.post("/api/ask/sessions", json=body or None)
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


async def _wait_idle(sid: str, timeout: float = 15.0) -> None:
    """等这一轮跑完。

    不能写死 sleep：Mock 是逐字流式的，耗时随正文长度变化。也不能只看
    ``is_busy`` 为假就返回 —— ``asyncio.create_task`` 只是排期，HTTP 返回 202
    时后台任务**还没拿到槽位**，那一瞬间 ``is_busy`` 恰好是假，直接返回就会
    在下一轮请求上撞出 409。所以先给它一点时间进入「忙」，再等它回到「闲」。
    """
    svc = await get_service_for_session(sid)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    grace = loop.time() + 1.0  # 一秒内都没忙过 = 这轮早就结束了
    saw_busy = False
    while True:
        if svc.is_busy(sid):
            saw_busy = True
        elif saw_busy or loop.time() > grace:
            return
        if loop.time() > deadline:
            raise AssertionError("等待 agent 空闲超时")
        await asyncio.sleep(0.05)


async def _messages(client, sid) -> list[dict]:
    r = await client.get(f"/api/ask/sessions/{sid}/messages")
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ------------------------------------------------- 会话级运行参数（PATCH config）


async def test_patch_session_run_params(client):
    sid = await _new_session(client)
    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"timeout_seconds": 900, "skip_permissions": False})
    assert r.status_code == 200, r.text
    cfg = r.json()["data"]["agent_config"]
    assert cfg["timeout_seconds"] == 900
    assert cfg["skip_permissions"] is False
    # 落库且能被运行时读到（不是只回显在响应里）
    svc = await get_service_for_session(sid)
    assert (await svc.store.get_agent_config(sid))["timeout_seconds"] == 900
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_patch_session_run_param_none_means_follow_default(client):
    """``None`` = 回到全局默认档，必须真的把键覆盖成 None（不是「没传」）。"""
    sid = await _new_session(client)
    assert (await client.patch(f"/api/ask/sessions/{sid}/config",
                               json={"timeout_seconds": 900})).status_code == 200
    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"timeout_seconds": None})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["agent_config"]["timeout_seconds"] is None
    await client.delete(f"/api/ask/sessions/{sid}")


@pytest.mark.parametrize("bad", [5, 99999, "abc"])
async def test_patch_session_rejects_bad_timeout(client, bad):
    sid = await _new_session(client)
    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"timeout_seconds": bad})
    assert r.status_code == 400, r.text
    assert "超时" in r.text or "时间" in r.text
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_patch_session_rejects_non_bool_permissions(client):
    sid = await _new_session(client)
    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"skip_permissions": "true"})
    assert r.status_code == 400, r.text
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_patch_session_rejects_provider_with_hint(client):
    """换后端不是改配置，是另开会话 —— 错误信息要指路，否则用户只看到 400。"""
    sid = await _new_session(client)
    r = await client.patch(f"/api/ask/sessions/{sid}/config",
                           json={"provider": "claude_code"})
    assert r.status_code == 400
    assert "另开会话" in r.text
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_session_run_params_change_the_real_command(client):
    """会话级开关必须进到命令行里 —— 早期版本这两个字段写了没人读。"""
    sid = await _new_session(client, provider="claude_code",
                             skills=None, mcp_tools=None)
    await client.patch(f"/api/ask/sessions/{sid}/config",
                       json={"skip_permissions": False, "timeout_seconds": 120})
    svc = await get_service_for_session(sid)
    cfg = await svc.store.get_agent_config(sid)
    ws = svc._workspace_for(sid, cfg)
    cmd = svc._build_cmd("hi", None, ws, cfg)
    assert "--dangerously-skip-permissions" not in cmd
    assert svc._timeout_for(cfg) == 120.0
    await client.delete(f"/api/ask/sessions/{sid}")


# --------------------------------------------------------- 重新生成


async def test_regenerate_replaces_previous_answer(client):
    sid = await _new_session(client)
    await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "大盘怎么样"})
    await _wait_idle(sid)
    before = await _messages(client, sid)
    first_answers = [m["id"] for m in before if m["role"] == "assistant"]
    assert first_answers, "第一轮应当有回答"

    r = await client.post(f"/api/ask/sessions/{sid}/regenerate")
    assert r.status_code == 202, r.text
    assert r.json()["data"]["replaced_messages"] == len(first_answers)
    await _wait_idle(sid)

    after = await _messages(client, sid)
    users = [m for m in after if m["role"] == "user"]
    answers = [m for m in after if m["role"] == "assistant"]
    assert len(users) == 1, "重新生成不该再落一条 user 消息"
    assert len(answers) == 1, "旧答案必须被替换而不是摞一条"
    assert answers[0]["id"] not in first_answers
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_regenerate_without_question_400(client):
    sid = await _new_session(client)
    r = await client.post(f"/api/ask/sessions/{sid}/regenerate")
    assert r.status_code == 400
    assert "提问" in r.text
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_regenerate_rejects_provider_switch(client):
    sid = await _new_session(client)
    r = await client.post(f"/api/ask/sessions/{sid}/regenerate",
                          json={"provider": "claude_code"})
    assert r.status_code == 400
    assert "另开会话" in r.text
    await client.delete(f"/api/ask/sessions/{sid}")


# --------------------------------------------------------- 换后端另开会话


async def test_fork_copies_context_to_other_provider(client):
    sid = await _new_session(client, context={"symbol": "600519"},
                             skills=["a-stock-data"], mcp_tools=None)
    await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
    await _wait_idle(sid)

    r = await client.post(f"/api/ask/sessions/{sid}/fork",
                          json={"provider": "claude_code"})
    assert r.status_code == 200, r.text
    new = r.json()["data"]
    assert new["id"] != sid
    assert new["agent_config"]["provider"] == "claude_code"
    # 能力集跟着搬（用户对新会话的预期就是「跟刚才一样，只是换个后端」）
    assert new["agent_config"]["skills"] == ["a-stock-data"]
    assert new["context"]["symbol"] == "600519"
    assert new["context"]["forked_from"] == sid
    assert "600519 怎么样" in new["context"]["briefing"]
    assert new["briefing_chars"] > 0
    # 原会话一动不动：能力集没被搬走，也没被写上别的 provider
    src = (await client.get("/api/ask/sessions")).json()["data"]
    src_cfg = next(s for s in src if s["id"] == sid)["agent_config"]
    assert src_cfg["skills"] == ["a-stock-data"]
    assert src_cfg.get("provider") is None
    assert "briefing" not in (next(s for s in src if s["id"] == sid)["context"])
    await client.delete(f"/api/ask/sessions/{new['id']}")
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_fork_rejects_same_or_unknown_provider(client):
    sid = await _new_session(client)
    r = await client.post(f"/api/ask/sessions/{sid}/fork", json={"provider": "mock"})
    assert r.status_code == 400
    assert "相同" in r.text
    r = await client.post(f"/api/ask/sessions/{sid}/fork", json={"provider": "gpt5"})
    assert r.status_code == 400
    assert "未知 provider" in r.text
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_fork_unknown_session_404(client):
    r = await client.post("/api/ask/sessions/nope/fork", json={"provider": "claude_code"})
    assert r.status_code == 404


# --------------------------------------------------------- 运行中列表 / 终止


async def test_runs_lists_running_and_accepts_kill(client):
    sid = await _new_session(client)
    await client.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
    # 立刻查：这一轮还在跑（不等它结束，否则测的是「列表为空」）。
    # 轮询是因为 create_task 只是排期 —— 202 返回时它可能还没拿到槽位。
    mine: list[dict] = []
    data: dict = {}
    for _ in range(40):
        data = (await client.get("/api/ask/runs")).json()["data"]
        mine = [x for x in data["runs"] if x["session_id"] == sid]
        if mine:
            break
        await asyncio.sleep(0.05)
    assert data["max_concurrent_runs"] >= 1
    assert mine, "刚起的回答应当出现在运行中列表里"
    assert mine[0]["provider"] == "mock"

    r = await client.post(f"/api/ask/runs/{sid}/kill")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["killed"] is True
    await _wait_idle(sid)
    rows = (await client.get("/api/ask/runs")).json()["data"]["runs"]
    assert all(x["session_id"] != sid for x in rows)
    await client.delete(f"/api/ask/sessions/{sid}")


async def test_runs_kill_unknown_session_404(client):
    r = await client.post("/api/ask/runs/nope/kill")
    assert r.status_code == 404
