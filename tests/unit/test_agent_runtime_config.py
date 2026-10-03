"""运行时 agent 配置：设置页写入的值必须**真的生效**（不是只显示「已修改」）。

这一层是「前端能控制这些」的地基：``agent.provider`` 曾经只写进 ``app_setting``，
而 agent 侧读的是 ``get_settings().agent``（lru_cache，只认 config/app.yaml）——
界面显示已修改、实际一点没变。用例覆盖两件事：

1. :func:`lquant.agent.runtime.effective_agent_config` 的合并与降级；
2. 真实消费方（``default_agent_config`` / CLI 服务的命令行与超时）跟着变。

隔离方式沿用 T3 报告结论：``LQ_ROOT`` + chdir + ``cache_clear``。
"""
from __future__ import annotations

import pytest

from lquant.agent.claude_code import ClaudeCodeAgentService
from lquant.agent.codex import CodexAgentService
from lquant.agent.runtime import effective_agent_config
from lquant.agent.service import default_agent_config
from lquant.core.config import get_settings
from lquant.core.settings_store import SETTING_DEFS, SettingsStore, coerce_setting, defaults

_APP_YAML = """\
agent:
  provider: claude_code
  default_skills: alpha
  default_mcp_tools: all
  timeout_seconds: 111
  skip_permissions: true
  partial_messages: true
  max_concurrent_runs: 6
"""


@pytest.fixture()
def tmp_root(tmp_path, monkeypatch):
    """临时仓库根：config/app.yaml + 两个可用 skill（存在性校验要用）。"""
    (tmp_path / "config" / "skills" / "alpha").mkdir(parents=True)
    (tmp_path / "config" / "skills" / "beta").mkdir(parents=True)
    for name in ("alpha", "beta"):
        (tmp_path / "config" / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的说明\n---\n\n# {name}\n",
            encoding="utf-8")
    (tmp_path / "config" / "app.yaml").write_text(_APP_YAML, encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _put(key: str, value: str) -> None:
    SettingsStore().put(key, value)


def _claude(root) -> ClaudeCodeAgentService:
    from lquant.agent.sessions import SessionStore

    return ClaudeCodeAgentService(SessionStore(str(root / "ask.db")),
                                  workspace_dir="ws", root=root)


# --------------------------------------------------------------- 合并与降级


def test_no_override_falls_back_to_yaml(tmp_root):
    cfg = effective_agent_config()
    assert cfg["provider"] == "claude_code"
    assert cfg["default_skills"] == ["alpha"]
    assert cfg["default_mcp_tools"] is None  # all → 不裁剪
    assert cfg["timeout_seconds"] == 111
    assert cfg["skip_permissions"] is True
    assert cfg["partial_messages"] is True


def test_provider_override_takes_effect(tmp_root):
    _put("agent.provider", "mock")
    assert effective_agent_config()["provider"] == "mock"
    # 新建会话的默认值随之变化（前端「AI 设置」改完下一次新建即生效）
    assert default_agent_config()["provider"] == "mock"


def test_default_skills_override(tmp_root):
    _put("agent.default_skills", "none")
    assert default_agent_config()["skills"] == []
    _put("agent.default_skills", "beta,alpha")
    assert default_agent_config()["skills"] == ["beta", "alpha"]
    _put("agent.default_skills", "all")
    assert default_agent_config()["skills"] is None


def test_default_mcp_tools_override(tmp_root):
    _put("agent.default_mcp_tools", "none")
    assert default_agent_config()["mcp_tools"] == []


def test_dirty_override_falls_back_to_default_not_crash(tmp_root):
    """手工 SQL 写坏的值只该让这一项回退默认，不该让 agent 起不来。"""
    _, err = coerce_setting("agent.timeout_seconds", "abc")
    assert err, "写入侧应当先拦住非数字"
    from lquant.core.db import writer

    SettingsStore()._ensure_table()
    with writer() as con:
        con.execute(
            "INSERT INTO app_setting (setting_key, setting_value, source, updated_at) "
            "VALUES ('agent.timeout_seconds', 'oops', 'runtime', now()) "
            "ON CONFLICT (setting_key) DO UPDATE SET setting_value='oops'")
    assert effective_agent_config()["timeout_seconds"] == 111  # yaml 默认，不是崩


# --------------------------------------------------------------- 写入侧校验


def test_default_skills_must_exist(tmp_root):
    with pytest.raises(ValueError, match="skill 不存在"):
        _put("agent.default_skills", "nope")


def test_default_skills_shape_checked(tmp_root):
    with pytest.raises(ValueError, match="非法 skill 名"):
        _put("agent.default_skills", "Bad Name")


def test_default_skills_accepts_all_and_none(tmp_root):
    _put("agent.default_skills", "all")
    _put("agent.default_skills", "none")


def test_default_mcp_tools_checked_against_registry(tmp_root):
    with pytest.raises(ValueError, match="未知 MCP 工具"):
        _put("agent.default_mcp_tools", "no_such_tool")
    _put("agent.default_mcp_tools", "none")
    _put("agent.default_mcp_tools", "all")


def test_timeout_range_checked(tmp_root):
    with pytest.raises(ValueError, match="10~3600"):
        _put("agent.timeout_seconds", "5")
    with pytest.raises(ValueError, match="10~3600"):
        _put("agent.timeout_seconds", "99999")
    _put("agent.timeout_seconds", "60")
    assert effective_agent_config()["timeout_seconds"] == 60


def test_agent_keys_registered_and_derived_from_config(tmp_root):
    for key in ("agent.default_skills", "agent.default_mcp_tools",
                "agent.timeout_seconds", "agent.skip_permissions",
                "agent.partial_messages", "agent.max_concurrent_runs"):
        assert key in SETTING_DEFS
        assert defaults()[key][1] == "config"  # app.yaml 里显式声明了


def test_max_concurrent_runs_range_checked(tmp_root):
    from lquant.agent.concurrency import MAX_RUNS_MAX, MAX_RUNS_MIN
    from lquant.agent.service import max_concurrent_runs

    assert max_concurrent_runs() == 6
    with pytest.raises(ValueError, match="并发上限"):
        _put("agent.max_concurrent_runs", str(MAX_RUNS_MIN - 1))
    with pytest.raises(ValueError, match="并发上限"):
        _put("agent.max_concurrent_runs", str(MAX_RUNS_MAX + 1))
    _put("agent.max_concurrent_runs", "2")
    assert max_concurrent_runs() == 2


# ------------------------------------------------- 真实消费方（命令行 / 超时）


def test_timeout_priority_session_then_ctor_then_runtime(tmp_root):
    _put("agent.timeout_seconds", "60")
    svc = _claude(tmp_root)
    assert svc._timeout_for({}) == 60.0          # 运行时全局
    svc2 = ClaudeCodeAgentService(
        svc.store, workspace_dir="ws", root=tmp_root, timeout_seconds=7)
    assert svc2._timeout_for({}) == 7.0          # 构造参数（测试 / A2A 显式注入）
    assert svc2._timeout_for({"timeout_seconds": 3}) == 3.0  # 会话级最高


def test_partial_messages_toggle_takes_effect_without_rebuild(tmp_root):
    svc = _claude(tmp_root)  # 同一个实例，模拟「服务不重启」
    ws = svc._workspace_for("s1", {})
    assert "--include-partial-messages" in svc._build_cmd("hi", None, ws)
    _put("agent.partial_messages", "false")
    assert "--include-partial-messages" not in svc._build_cmd("hi", None, ws)


def test_skip_permissions_toggle_takes_effect_without_rebuild(tmp_root):
    from lquant.agent.sessions import SessionStore

    claude = _claude(tmp_root)
    ws = claude._workspace_for("s1", {})
    assert "--dangerously-skip-permissions" in claude._build_cmd("hi", None, ws)
    _put("agent.skip_permissions", "false")
    assert "--dangerously-skip-permissions" not in claude._build_cmd("hi", None, ws)

    codex = CodexAgentService(SessionStore(str(tmp_root / "ask.db")),
                              workspace_dir="ws", root=tmp_root)
    ws2 = codex._workspace_for("s1", {})
    cmd = codex._build_cmd("hi", None, ws2)
    assert "-s" in cmd and "workspace-write" in cmd  # 关掉 → 退回只写工作区
    _put("agent.skip_permissions", "true")
    cmd = codex._build_cmd("hi", None, ws2)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "workspace-write" not in cmd


def test_ctor_override_still_wins_over_runtime(tmp_root):
    """A2A 执行器/测试显式传的权限值不能被运行时配置顶掉。"""
    _put("agent.skip_permissions", "true")
    svc = ClaudeCodeAgentService(
        _claude(tmp_root).store, workspace_dir="ws", root=tmp_root,
        skip_permissions=False)
    ws = svc._workspace_for("s1", {})
    assert "--dangerously-skip-permissions" not in svc._build_cmd("hi", None, ws)


# ------------------------------------------------- HTTP 层（前端实际走的路径）


@pytest.fixture()
async def client(tmp_root):
    from httpx import ASGITransport, AsyncClient

    from lquant.server.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _put_api(client, key: str, value: str):
    return await client.put(f"/api/settings/{key}", json={"key": key, "value": value})


async def test_api_put_default_skills_takes_effect(client):
    r = await _put_api(client, "agent.default_skills", "beta")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["value"] == "beta"
    assert default_agent_config()["skills"] == ["beta"]


async def test_api_put_rejects_unknown_skill(client):
    r = await _put_api(client, "agent.default_skills", "nope")
    assert r.status_code == 422
    assert "skill 不存在" in r.text


async def test_api_put_rejects_bad_timeout(client):
    r = await _put_api(client, "agent.timeout_seconds", "5")
    assert r.status_code == 422
    assert "10~3600" in r.text


async def test_api_put_provider_switches_new_sessions(client):
    """设置页改 provider → 下一次取 service 就走新后端（曾经是死配置）。"""
    from lquant.agent.mock import MockAgentService
    from lquant.agent.service import get_agent_service

    r = await _put_api(client, "agent.provider", "mock")
    assert r.status_code == 200, r.text
    svc = await get_agent_service()
    assert isinstance(svc, MockAgentService)


async def test_api_reset_restores_yaml_default(client):
    assert (await _put_api(client, "agent.default_skills", "beta")).status_code == 200
    assert default_agent_config()["skills"] == ["beta"]
    r = await client.delete("/api/settings/agent.default_skills")
    assert r.status_code == 200
    assert default_agent_config()["skills"] == ["alpha"]  # 回到 app.yaml


def test_session_override_wins_over_ctor(tmp_root):
    """会话级比构造参数更硬：构造参数是「这个实例的默认档」（测试 / A2A 注入），
    会话配置是用户**当下**对这条会话的选择 —— 后者才代表当前意图。

    与 ``_timeout_for`` 的优先级口径一致（session > ctor > runtime）；两处
    不一致的话，「我在会话里关掉了权限怎么还带着全自主旗标」会变成常态。"""
    svc = ClaudeCodeAgentService(
        _claude(tmp_root).store, workspace_dir="ws", root=tmp_root,
        skip_permissions=False)
    ws = svc._workspace_for("s1", {})
    assert "--dangerously-skip-permissions" not in svc._build_cmd("hi", None, ws, {})
    cmd = svc._build_cmd("hi", None, ws, {"skip_permissions": True})
    assert "--dangerously-skip-permissions" in cmd


async def test_briefing_only_injected_before_first_cli_turn(tmp_root):
    """换后端另开会话：简报只在**首次**调用时拼进 prompt。

    第二轮起 CLI 自己带着上下文；再注入一次等于同一段历史重复两遍，模型会
    以为用户把话说了两次。所以判据是「本会话还没有 CLI 侧会话 id」。
    """
    svc = _claude(tmp_root)
    ses = await svc.store.create({"briefing": "用户：老问题\n\n助手：老答案"},
                                 {"provider": "claude_code"})
    first = await svc._with_briefing(ses.id, "接着问", None)
    assert "用户：老问题" in first and first.endswith("接着问")
    later = await svc._with_briefing(ses.id, "接着问", "cli-sid-123")
    assert later == "接着问"


async def test_briefing_absent_is_a_noop(tmp_root):
    svc = _claude(tmp_root)
    ses = await svc.store.create({}, {"provider": "claude_code"})
    assert await svc._with_briefing(ses.id, "问", None) == "问"
    await svc.store.close()


async def test_send_message_records_pid_and_workspace(tmp_root, monkeypatch):
    """运行中列表要能看到 pid / 工作区 —— 判断「卡住了要不要杀」时能拿 pid 去 ps。

    走 ``send_message`` 而不是裸 ``_run``：运行信息是 ``_claim`` 建槽位时开的账，
    ``_run`` 只往里补 pid。绕过 ``_claim`` 直接调 ``_run`` 测的是另一条路径
    （``set_run_info`` 会静默 no-op，因为压根没有槽位）。
    """
    from lquant.agent import cli_agent as mod

    class _FakeProc:
        pid = 4242
        returncode = None

        async def attach(self):
            return None

    svc = _claude(tmp_root)
    ses = await svc.store.create({}, {"provider": "claude_code"})
    seen: dict = {}

    async def fake_consume(sid, proc, on_event, cfg=None):
        # 在「正在跑」的这一刻取快照，而不是跑完之后
        seen.update(svc.running_runs()[0])

    monkeypatch.setattr(mod, "spawn_child", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(svc, "_consume", fake_consume)
    try:
        await svc.send_message(ses.id, "hi", lambda e: None)
        assert seen["session_id"] == ses.id
        assert seen["pid"] == 4242
        assert str(tmp_root / "ws") in str(seen["workspace"])
        assert seen["provider"] == "claude_code"
        # 结束后不能留下幽灵记录（否则并发上限会被慢慢吃光）
        assert svc.running_runs() == []
        assert svc._run_meta == {}
    finally:
        await svc.store.close()
