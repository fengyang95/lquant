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
    """工作区必须落在配置的 root 下，且带 codex 需要的 AGENTS.md。"""
    svc = await _select(monkeypatch, "codex")
    assert svc._workspace == tmp_path / "ws"
    assert (tmp_path / "ws" / "AGENTS.md").is_file()
