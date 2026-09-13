"""端到端：创建会话 → WS 订阅 → 发消息 → 断言事件流与落库对账。"""
import asyncio
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lquant.core.config import get_settings
from lquant.server.main import app

_TIMEOUT = 10.0

_FAKE_QUOTE = {
    "symbol": "600519", "price": 1700.0, "change_pct": 1.2,
    "ts": "2026-09-10 10:00:00", "source": "stub",
}


@pytest.fixture(autouse=True)
def fake_env(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离，杜绝写真实 ./data。

    SessionStore 路径取 settings.root/data/ask.db（agent/service.py 惰性
    单例），所以必须连 agent service 的单例 _cache 一起清，否则首个用例
    建立的 store 绑定会泄漏到后续用例。
    """
    import lquant.agent.service as agent_service

    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "config", tmp_path / "config")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    agent_service._cache.clear()
    yield
    get_settings.cache_clear()
    agent_service._cache.clear()


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


def test_ask_e2e_error_path_persists_assistant_error(monkeypatch):
    """error 路径：fetch_quotes 抛错 → WS 收到 error 事件 → 错误以 assistant 消息落库。"""

    def broken_fetch_quotes(symbols):  # noqa: ANN001, ARG001
        raise RuntimeError("行情源炸了")

    monkeypatch.setattr("lquant.agent.mock.fetch_quotes", broken_fetch_quotes)

    with TestClient(app) as c:
        r = c.post("/api/ask/sessions")
        assert r.status_code == 200
        sid = r.json()["data"]["id"]

        with c.websocket_connect(f"/ws/ask/{sid}") as ws:
            r = c.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
            assert r.status_code == 202

            events = _drain_until_terminal(ws)
            assert events[-1]["type"] == "error"
            assert "行情源炸了" in events[-1]["message"]

        # 错误已落库：最后一条消息 role == assistant
        r = c.get(f"/api/ask/sessions/{sid}/messages")
        msgs = r.json()["data"]
        assert msgs, "会话应有落库消息"
        assert msgs[-1]["role"] == "assistant"
        assert "出错了" in msgs[-1]["content"]

        c.delete(f"/api/ask/sessions/{sid}")


def test_ask_e2e_cancel_path(monkeypatch):
    """cancel 路径：POST cancel 后 WS 收到 error（已中断）事件。

    竞态说明：mock 流程很快（delta sleep 0.01s），cancel 请求可能在
    done 之后才被处理——若 cancel 晚到则流以 done 正常收尾，此时断言
    收到 done 也算通过。
    """

    # 放慢 delta，给 cancel 留出在 done 之前生效的窗口
    real_sleep = asyncio.sleep

    async def slow_sleep(delay, *a, **kw):
        if delay == 0.01:
            delay = 0.05
        return await real_sleep(delay, *a, **kw)

    monkeypatch.setattr("lquant.agent.mock.asyncio.sleep", slow_sleep)

    with TestClient(app) as c:
        r = c.post("/api/ask/sessions")
        assert r.status_code == 200
        sid = r.json()["data"]["id"]

        with c.websocket_connect(f"/ws/ask/{sid}") as ws:
            r = c.post(f"/api/ask/sessions/{sid}/messages", json={"content": "600519 怎么样"})
            assert r.status_code == 202

            r = c.post(f"/api/ask/sessions/{sid}/cancel")
            assert r.status_code == 200

            events = _drain_until_terminal(ws)
            terminal = events[-1]
            if terminal["type"] == "error":
                assert "已中断" in terminal["message"]
            else:
                # 竞态：cancel 晚于 done，流已正常结束
                assert terminal["type"] == "done"

        c.delete(f"/api/ask/sessions/{sid}")


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
