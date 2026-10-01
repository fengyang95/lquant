"""/api/agent 能力清单 + skill 读写，以及 /ask 建会话时锁定能力配置。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from lquant.core import config as cfg_mod
from lquant.server.main import app

_MIN_APP_YAML = """\
agent:
  provider: ${LQ_AGENT_PROVIDER:mock}
  workspace_dir: ws
  default_skills: ${LQ_AGENT_DEFAULT_SKILLS:all}
  default_mcp_tools: ${LQ_AGENT_DEFAULT_MCP_TOOLS:all}
"""

_SKILL_MD = "---\nname: {n}\ndescription: 描述 {n}\ntags: [t]\n---\n\n# {n}\n"


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    """隔离的仓库根：config/app.yaml + config/skills/ 都在 tmp 下。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app.yaml").write_text(_MIN_APP_YAML, encoding="utf-8")
    d = tmp_path / "config" / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(n="alpha"), encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    cfg_mod.get_settings.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    cfg_mod.get_settings.cache_clear()


# ---- 能力清单 -------------------------------------------------------------


async def test_capabilities_lists_providers_skills_and_tools(client):
    r = await client.get("/api/agent/capabilities")
    assert r.status_code == 200
    data = r.json()["data"]
    assert {p["id"] for p in data["providers"]} == {"claude_code", "codex", "mock"}
    assert all("label" in p and "available" in p for p in data["providers"])
    assert [s["name"] for s in data["skills"]] == ["alpha"]
    assert data["skills"][0]["description"] == "描述 alpha"
    # MCP 工具清单来自 mcp_server 的注册表，不是第二份手写列表
    from lquant.agent.mcp_server import TOOL_HANDLERS

    assert {t["name"] for t in data["mcp_tools"]} == set(TOOL_HANDLERS)


async def test_capabilities_reports_global_defaults(client):
    """前端据此预填新建会话弹层；all 映射成 None = 全开。"""
    data = (await client.get("/api/agent/capabilities")).json()["data"]
    assert data["defaults"]["provider"] == "mock"
    assert data["defaults"]["skills"] is None
    assert data["defaults"]["mcp_tools"] is None


# ---- 建会话时锁定能力 ------------------------------------------------------


async def test_create_session_locks_capability_config(client):
    r = await client.post("/api/ask/sessions", json={
        "context": {"symbol": "600519"},
        "provider": "codex",
        "skills": ["alpha"],
        "mcp_tools": ["get_quotes"],
    })
    assert r.status_code == 200
    ses = r.json()["data"]
    assert ses["agent_config"] == {
        "provider": "codex", "skills": ["alpha"], "mcp_tools": ["get_quotes"]}
    # 列表里也要带上（前端渲染已锁定的能力 chips）
    listed = (await client.get("/api/ask/sessions")).json()["data"]
    assert next(s for s in listed if s["id"] == ses["id"])["agent_config"] == ses["agent_config"]


async def test_create_session_without_config_keeps_empty(client):
    """不带能力字段 = 未指定（走全局默认），不是「一个都不启用」。"""
    ses = (await client.post("/api/ask/sessions", json={})).json()["data"]
    assert ses["agent_config"] == {}


@pytest.mark.parametrize("body,msg", [
    ({"provider": "gpt"}, "未知 provider"),
    ({"mcp_tools": ["get_nope"]}, "未知 MCP 工具"),
    ({"skills": ["../etc"]}, "非法 skill 名"),
    ({"skills": "alpha"}, "必须是字符串列表"),
    ({"skills": [""]}, "非空字符串"),
])
async def test_create_session_rejects_invalid_config(client, body, msg):
    r = await client.post("/api/ask/sessions", json=body)
    assert r.status_code == 400
    assert msg in r.json()["message"]


async def test_create_session_accepts_explicit_empty_capability_set(client):
    """空列表是合法配置：一个 skill / 工具都不给（与「不传」语义不同）。"""
    ses = (await client.post("/api/ask/sessions", json={
        "skills": [], "mcp_tools": []})).json()["data"]
    assert ses["agent_config"] == {"skills": [], "mcp_tools": []}


# ---- skill 读写 -----------------------------------------------------------


async def test_skill_read_write_delete_roundtrip(client):
    r = await client.get("/api/agent/skills/alpha")
    assert r.status_code == 200
    assert "描述 alpha" in r.json()["data"]["content"]

    content = _SKILL_MD.format(n="beta").replace("alpha", "beta")
    r = await client.put("/api/agent/skills/beta", json={"content": content})
    assert r.status_code == 200
    assert [s["name"] for s in (await client.get(
        "/api/agent/capabilities")).json()["data"]["skills"]] == ["alpha", "beta"]

    assert (await client.delete("/api/agent/skills/beta")).status_code == 200
    assert [s["name"] for s in (await client.get(
        "/api/agent/capabilities")).json()["data"]["skills"]] == ["alpha"]


async def test_put_skill_overwrites_existing(client):
    r = await client.put("/api/agent/skills/alpha",
                         json={"content": "---\nname: alpha\ndescription: 改过了\n---\n"})
    assert r.status_code == 200
    assert (await client.get("/api/agent/skills/alpha")).json()["data"]["content"].endswith(
        "改过了\n---\n")


@pytest.mark.parametrize("content,msg", [
    ("没有 frontmatter", "frontmatter"),
    ("---\nname: x\n---\n", "description"),
    ("---\ndescription: y\n---\n", "name"),
])
async def test_put_skill_rejects_bad_frontmatter(client, content, msg):
    r = await client.put("/api/agent/skills/alpha", json={"content": content})
    assert r.status_code == 400
    assert msg in r.json()["message"]
    # 拒绝时不能把原文件改坏
    assert "描述 alpha" in (await client.get("/api/agent/skills/alpha")).json()["data"]["content"]


@pytest.mark.parametrize("name", ["A-B", "a b", "a.b", "a_b", "-x"])
async def test_invalid_skill_name_rejected(client, name):
    """写入按名字形状挡（400）；读/删只认存在性，这种名字根本不存在 → 404。
    （穿越型名字见 test_agent_capabilities 的单元用例 —— 含 `/` 或 `..` 的路径
    在路由层就被归一化掉了，测不到那一层。）"""
    assert (await client.put(f"/api/agent/skills/{name}",
                             json={"content": _SKILL_MD.format(n="x")})).status_code == 400
    assert (await client.get(f"/api/agent/skills/{name}")).status_code == 404
    assert (await client.delete(f"/api/agent/skills/{name}")).status_code == 404


async def test_read_missing_skill_404(client):
    assert (await client.get("/api/agent/skills/nope")).status_code == 404


async def test_delete_missing_skill_404(client):
    assert (await client.delete("/api/agent/skills/nope")).status_code == 404
