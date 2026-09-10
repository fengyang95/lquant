"""端到端：创建会话 → WS 订阅 → 发消息 → 断言事件流与落库对账。"""
import time

import pytest
from starlette.testclient import TestClient

from lquant.server.main import app

_TIMEOUT = 10.0

_FAKE_QUOTE = {
    "symbol": "600519", "price": 1700.0, "change_pct": 1.2,
    "ts": "2026-09-10 10:00:00", "source": "stub",
}


@pytest.fixture(autouse=True)
def _stub_quotes(monkeypatch):
    """行情源不可达时不稳定——stub 掉 mock agent 用到的 fetch_quotes，保证端到端确定。"""

    def fake_fetch_quotes(symbols):  # noqa: ANN001, ARG001
        return [{**_FAKE_QUOTE, "symbol": s} for s in symbols]

    monkeypatch.setattr("lquant.agent.mock.fetch_quotes", fake_fetch_quotes)


def _drain_until_terminal(ws) -> list[dict]:
    """逐条收事件直到 done/error，带 10s deadline。"""
    events: list[dict] = []
    deadline = time.time() + _TIMEOUT
    while time.time() < deadline:
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] in ("done", "error"):
            return events
    raise AssertionError(f"10s 内未收到终态事件: {events}")


def test_ask_e2e_stream_and_reconcile():
    with TestClient(app) as c:
        # 1. 建会话
        r = c.post("/api/ask/sessions")
        assert r.status_code == 200
        sid = r.json()["data"]["id"]

        # 2. WS 订阅（与后续 POST 同一 TestClient loop）
        with c.websocket_connect(f"/ws/ask/{sid}") as ws:
            # 3. 发消息（202 立即返回，后台跑 agent）
            r = c.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
            assert r.status_code == 202

            # 4. 收事件直到 done
            events = _drain_until_terminal(ws)
            types = [e["type"] for e in events]
            assert types[0] in ("tool_call", "assistant_delta")
            assert types[-1] == "done"
            assert "assistant_delta" in types

            # 5. 落库对账：done.message_id 对应 assistant 消息
            r = c.get(f"/api/ask/sessions/{sid}/messages")
            mids = {m["id"] for m in r.json()["data"]}
            assert events[-1]["message_id"] in mids

        # 6. 会话可复用：新 WS + 继续提问仍收到事件
        with c.websocket_connect(f"/ws/ask/{sid}") as ws2:
            r = c.post(f"/api/ask/sessions/{sid}/messages", json={"content": "000001 呢"})
            assert r.status_code == 202
            ev = ws2.receive_json()
            assert ev["type"] in ("tool_call", "assistant_delta", "done")

        # 7. 清理
        c.delete(f"/api/ask/sessions/{sid}")
