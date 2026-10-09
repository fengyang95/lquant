"""``get_agent_service`` 的 provider 选择（接线层，此前没有覆盖）。

隔离要点：``get_settings`` 是 lru_cache，清缓存后会**重新**读 ``LQ_ROOT`` 下的
``config/app.yaml``。所以只设 ``LQ_ROOT`` 而不放 app.yaml 是不够的 ——
app.yaml 不存在时 ``raw={}``，agent 段直接退回**代码默认值**，
``LQ_AGENT_PROVIDER`` 这类插值根本没机会生效（本仓「改了没生效」的常见形态）。
这里在 tmp 根下写一份最小 app.yaml，插值链路才是真的通的。
"""
from __future__ import annotations

import pytest

from lquant.agent import service as svc_mod
from lquant.agent.errors import AgentError
from lquant.core import config as cfg_mod

_MIN_APP_YAML = """\
agent:
  provider: ${LQ_AGENT_PROVIDER:claude_code}
  workspace_dir: ${LQ_AGENT_WORKSPACE:ws}
  timeout_seconds: 30
  skip_permissions: ${LQ_AGENT_SKIP_PERMISSIONS:false}
"""


@pytest.fixture(autouse=True)
async def _isolate(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app.yaml").write_text(_MIN_APP_YAML, encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    cfg_mod.get_settings.cache_clear()
    svc_mod._cache.clear()
    yield
    await svc_mod.shutdown_agent_service()
    cfg_mod.get_settings.cache_clear()
    svc_mod._cache.clear()


async def _select(monkeypatch, provider: str):
    monkeypatch.setenv("LQ_AGENT_PROVIDER", provider)
    cfg_mod.get_settings.cache_clear()
    svc_mod._cache.clear()
    return await svc_mod.get_agent_service()


async def test_settings_really_pick_up_the_env_provider(monkeypatch):
    """先证明隔离有效：否则下面的断言全是在验默认值。"""
    monkeypatch.setenv("LQ_AGENT_PROVIDER", "codex")
    cfg_mod.get_settings.cache_clear()
    assert cfg_mod.get_settings().agent.provider == "codex"


async def test_codex_provider_selected(monkeypatch):
    from lquant.agent.codex import CodexAgentService

    svc = await _select(monkeypatch, "codex")
    assert isinstance(svc, CodexAgentService)
    assert svc.label == "codex"


async def test_claude_provider_selected(monkeypatch):
    from lquant.agent.claude_code import ClaudeCodeAgentService

    svc = await _select(monkeypatch, "claude_code")
    assert isinstance(svc, ClaudeCodeAgentService)
    assert svc.label == "claude_code"


async def test_mock_provider_selected(monkeypatch):
    from lquant.agent.mock import MockAgentService

    assert isinstance(await _select(monkeypatch, "mock"), MockAgentService)


async def test_unknown_provider_rejected(monkeypatch):
    with pytest.raises(AgentError) as exc:
        await _select(monkeypatch, "gpt")
    assert "未知 agent provider" in str(exc.value)


async def test_codex_workspace_lands_under_configured_root(monkeypatch, tmp_path):
    """会话工作区必须落在配置的 root 下的 ``<workspace_dir>/<sid>``，且带 AGENTS.md。"""
    svc = await _select(monkeypatch, "codex")
    assert svc._workspace_base == tmp_path / "ws"
    ws = svc._workspace_for("sess-1", {})
    assert ws == tmp_path / "ws" / "sess-1"
    assert (ws / "AGENTS.md").is_file()


# ---- 会话级 provider / 能力路由 -------------------------------------------


async def test_store_rebinds_when_root_changes(tmp_path, monkeypatch):
    """换 LQ_ROOT 必须换库。

    集成测试的隔离手段是「清缓存 + 换 LQ_ROOT + chdir」；store 若按模块单例
    一直留着，第二个用例就还在写上一个 root 的 ask.db —— 表现为跨用例串数据
    或写进已删除的 tmp 目录。
    """
    first = svc_mod._get_store()
    monkeypatch.setenv("LQ_ROOT", str(tmp_path / "other"))
    cfg_mod.get_settings.cache_clear()
    second = svc_mod._get_store()
    assert second is not first
    assert second._path == str(tmp_path / "other" / "data" / "ask.db")


async def test_multiple_providers_coexist_and_share_one_store():
    """会话级选择意味着多个 provider 实例同时活着 —— 但会话事实源只能有一个。

    各建各的 SessionStore 会让「codex 会话」的消息落在另一个库里，
    /ask 页面按 sid 读消息就读不到了。
    """
    a = await svc_mod.get_agent_service("claude_code")
    b = await svc_mod.get_agent_service("codex")
    assert a is not b
    assert a.store is b.store
    # 注册表缓存：同 provider 反复取到同一个实例
    assert await svc_mod.get_agent_service("codex") is b


async def test_service_for_session_routes_by_locked_provider():
    from lquant.agent.claude_code import ClaudeCodeAgentService
    from lquant.agent.codex import CodexAgentService

    store = svc_mod._get_store()
    claude_ses = await store.create(None, {"provider": "claude_code"})
    codex_ses = await store.create(None, {"provider": "codex"})
    legacy = await store.create(None)  # 老会话 / A2A 建的：无 agent_config

    assert isinstance(await svc_mod.get_service_for_session(claude_ses.id),
                      ClaudeCodeAgentService)
    assert isinstance(await svc_mod.get_service_for_session(codex_ses.id),
                      CodexAgentService)
    # 未指定 → 全局默认（测试会话里 conftest 固定成 mock，生产默认是 claude_code）
    default_svc = await svc_mod.get_agent_service()
    assert await svc_mod.get_service_for_session(legacy.id) is default_svc


def _mk_skill(root, name: str) -> None:
    d = root / "config" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n",
                                encoding="utf-8")


async def test_session_workspace_isolated_and_skill_filtered(tmp_path):
    """两个会话的工作区互不干扰，且各自只放启用的 skill。

    共享一个工作区时，并发跑的两个会话会互相覆盖能力集 —— 单飞是**按会话**的，
    两个会话本来就允许同时跑。
    """
    for n in ("alpha", "beta"):
        _mk_skill(tmp_path, n)

    svc = await svc_mod.get_agent_service("claude_code")
    store = svc_mod._get_store()
    a = await store.create(None, {"skills": ["alpha"]})
    b = await store.create(None, {"skills": ["beta"]})

    ws_a = svc._workspace_for(a.id, await store.get_agent_config(a.id))
    ws_b = svc._workspace_for(b.id, await store.get_agent_config(b.id))
    assert ws_a != ws_b
    assert [p.name for p in (ws_a / ".claude" / "skills").iterdir()] == ["alpha"]
    assert [p.name for p in (ws_b / ".claude" / "skills").iterdir()] == ["beta"]


async def test_session_mcp_tool_whitelist_reaches_workspace(tmp_path):
    """会话选定的 MCP 工具要落到工作区 mcp.json 的 env 上（两个 provider 共用这份）。"""
    import json

    svc = await svc_mod.get_agent_service("claude_code")
    store = svc_mod._get_store()
    ses = await store.create(None, {"mcp_tools": ["get_quotes"]})
    ws = svc._workspace_for(ses.id, await store.get_agent_config(ses.id))
    env = json.loads((ws / ".claude" / "mcp.json").read_text(
        encoding="utf-8"))["mcpServers"]["lquant"]["env"]
    # submit_verdict 永远在名单里（结论输出通道，不是数据权限）
    assert env["LQ_MCP_ENABLED_TOOLS"] == "get_quotes,submit_verdict"


async def test_unset_session_config_keeps_all_capabilities(tmp_path):
    """未指定的会话（老会话 / A2A）必须**全开**，不能因为新机制变成裸模型。"""
    for n in ("alpha", "beta"):
        _mk_skill(tmp_path, n)

    svc = await svc_mod.get_agent_service("claude_code")
    ws = svc._workspace_for("legacy-sid", {})
    assert sorted(p.name for p in (ws / ".claude" / "skills").iterdir()) == ["alpha", "beta"]
    import json

    env = json.loads((ws / ".claude" / "mcp.json").read_text(
        encoding="utf-8"))["mcpServers"]["lquant"]["env"]
    assert "LQ_MCP_ENABLED_TOOLS" not in env
