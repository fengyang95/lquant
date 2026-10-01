"""A2A 端到端：Agent Card 发现 + JSON-RPC（send/stream/get/cancel）+ 鉴权。

provider 由根 conftest 固定为 mock（单测不拉真 claude CLI）；这里验证的是
**协议层与接线**（路由前缀、SSE 分帧、错误码、会话共用），执行体细节在
tests/unit/test_a2a_executor.py。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lquant.core.config import get_settings
from lquant.server.main import app

_CARD_URL = "/.well-known/agent-card.json"
_A2A_URL = "/a2a"
_SEND = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
         "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
                                "parts": [{"text": "今天大盘怎么样"}]}}}


@pytest.fixture(autouse=True)
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """LQ_ROOT + chdir 隔离，杜绝写真实 ./data。"""
    import lquant.agent.service as agent_service
    from lquant.agent.a2a import executor as a2a_executor

    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "config", tmp_path / "config")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_A2A_TOKEN", raising=False)
    get_settings.cache_clear()
    a2a_executor.reset_a2a_executor()
    agent_service._cache.clear()
    yield
    a2a_executor.reset_a2a_executor()
    agent_service._cache.clear()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_quotes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "lquant.agent.mock.fetch_quotes",
        lambda symbols: [{"symbol": s, "price": 100.0, "change_pct": 1.0,
                          "ts": "2026-10-01 10:00:00"} for s in symbols])


def _frames(text: str) -> list[dict]:
    """解析 SSE 正文为帧列表（data: 行）。"""
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


# ---- 发现 ----------------------------------------------------------------

def test_agent_card_discoverable_without_api_prefix():
    with TestClient(app) as c:
        r = c.get(_CARD_URL)
    assert r.status_code == 200
    assert r.headers["A2A-Version"] == "1.0"
    assert "application/json" in r.headers["content-type"]
    card = r.json()
    for key in ("name", "description", "version", "supportedInterfaces", "capabilities",
                "defaultInputModes", "defaultOutputModes", "skills"):
        assert key in card, key
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a")
    assert card["capabilities"]["streaming"] is True
    assert {"a-stock-data", "factor-mining"} <= {s["id"] for s in card["skills"]}


# ---- JSON-RPC：message/send ---------------------------------------------

def test_message_send_returns_completed_task_and_shares_session():
    with TestClient(app) as c:
        r = c.post(_A2A_URL, json=_SEND)
        assert r.status_code == 200
        assert r.headers["A2A-Version"] == "1.0"
        body = r.json()
        assert body["jsonrpc"] == "2.0" and body["id"] == 1
        task = body["result"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["artifacts"][0]["parts"][0]["text"]

        # 共用事实源：A2A 建出来的会话在 /ask 侧能查到，消息也在
        ctx = task["contextId"]
        r2 = c.get(f"/api/ask/sessions/{ctx}/messages")
        assert r2.status_code == 200
        contents = [m["content"] for m in r2.json()["data"]]
        assert "今天大盘怎么样" in contents

        # history 只回文本 part
        assert [m["role"] for m in task["history"]][0] == "ROLE_USER"


def test_reusing_context_id_appends_to_same_session():
    with TestClient(app) as c:
        first = c.post(_A2A_URL, json=_SEND).json()["result"]
        again = {**_SEND, "id": 2, "params": {
            **_SEND["params"], "contextId": first["contextId"],
            "message": {"messageId": "m2", "role": "ROLE_USER",
                        "parts": [{"text": "那昨天呢"}]}}}
        second = c.post(_A2A_URL, json=again).json()["result"]

        assert second["contextId"] == first["contextId"]
        msgs = c.get(f"/api/ask/sessions/{first['contextId']}/messages").json()["data"]
        assert [m["content"] for m in msgs if m["role"] == "user"] == [
            "今天大盘怎么样", "那昨天呢"]


def test_message_send_failure_maps_to_failed_state(monkeypatch: pytest.MonkeyPatch):
    from lquant.agent.service import get_agent_service

    with TestClient(app) as c:
        svc = c.portal.call(get_agent_service)

        async def _boom(*_a, **_k):
            raise RuntimeError("执行体炸了")

        monkeypatch.setattr(svc, "send_message", _boom)
        task = c.post(_A2A_URL, json=_SEND).json()["result"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"


# ---- JSON-RPC：message/stream -------------------------------------------

def test_message_stream_sse_frame_order():
    with TestClient(app) as c:
        r = c.post(_A2A_URL, json={**_SEND, "id": 7, "method": "message/stream"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _frames(r.text)
    assert frames, r.text
    assert all(f["jsonrpc"] == "2.0" and f["id"] == 7 for f in frames)
    kinds = [next(iter(f["result"])) for f in frames]
    assert kinds[0] == "task"                     # 流必须以 Task 开头
    assert "artifactUpdate" in kinds
    assert kinds[-1] == "statusUpdate"
    assert frames[0]["result"]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    last = frames[-1]["result"]["statusUpdate"]
    assert last["status"]["state"] == "TASK_STATE_COMPLETED" and last["final"] is True


def test_stream_start_error_is_jsonrpc_error_not_sse():
    """contextId 不存在：必须在发 SSE 响应头之前就报错。"""
    with TestClient(app) as c:
        r = c.post(_A2A_URL, json={**_SEND, "method": "message/stream",
                                   "params": {**_SEND["params"], "contextId": "nope"}})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["code"] == -32602


# ---- JSON-RPC：tasks/get / tasks/cancel ---------------------------------

def test_tasks_get_and_history_length():
    with TestClient(app) as c:
        task = c.post(_A2A_URL, json=_SEND).json()["result"]
        got = c.post(_A2A_URL, json={"jsonrpc": "2.0", "id": 3, "method": "tasks/get",
                                     "params": {"id": task["id"]}}).json()["result"]
        assert got["status"]["state"] == "TASK_STATE_COMPLETED"
        assert got["artifacts"][0]["parts"][0]["text"] == task["artifacts"][0]["parts"][0]["text"]

        bare = c.post(_A2A_URL, json={"jsonrpc": "2.0", "id": 4, "method": "GetTask",
                                      "params": {"id": task["id"], "historyLength": 0}}
                      ).json()["result"]
    assert "history" not in bare


def test_tasks_cancel_terminal_is_not_cancelable():
    with TestClient(app) as c:
        task = c.post(_A2A_URL, json=_SEND).json()["result"]
        r = c.post(_A2A_URL, json={"jsonrpc": "2.0", "id": 5, "method": "tasks/cancel",
                                   "params": {"id": task["id"]}}).json()
    assert r["error"]["code"] == -32002
    assert r["error"]["data"]["reason"] == "TaskNotCancelableError"


def test_tasks_get_unknown_maps_to_task_not_found():
    with TestClient(app) as c:
        r = c.post(_A2A_URL, json={"jsonrpc": "2.0", "id": 6, "method": "tasks/get",
                                   "params": {"id": "nope"}}).json()
    assert r["error"]["code"] == -32001


# ---- 协议错误与鉴权 -------------------------------------------------------

def test_unknown_method_and_bad_json():
    with TestClient(app) as c:
        r = c.post(_A2A_URL, json={"jsonrpc": "2.0", "id": 1, "method": "no/such"})
        assert r.json()["error"]["code"] == -32601
        r = c.post(_A2A_URL, content=b"not json",
                   headers={"Content-Type": "application/json"})
        assert r.json()["error"]["code"] == -32700


def test_a2a_token_enforced(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LQ_A2A_TOKEN", "s3cret")
    with TestClient(app) as c:
        # 卡片仍然公开，但声明了鉴权方案
        card = c.get(_CARD_URL).json()
        assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
        assert card["securityRequirements"] == [{"bearer": []}]

        r = c.post(_A2A_URL, json=_SEND)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"

        r = c.post(_A2A_URL, json=_SEND, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        r = c.post(_A2A_URL, json=_SEND, headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        assert r.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
