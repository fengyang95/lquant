"""`lq agent` CLI 覆盖补齐：注册表读 yaml（LQ_ROOT 指向临时目录），freeze 写回校验。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """临时 LQ_ROOT：config/agents + docs/agent-skill 隔离，freeze 不碰真仓库。"""
    root = tmp_path / "repo"
    (root / "config" / "agents").mkdir(parents=True)
    (root / "docs" / "agent-skill").mkdir(parents=True)
    (root / "config" / "agents" / "gp-internal.yaml").write_text(yaml.safe_dump({
        "name": "gp-internal", "kind": "builtin", "driver": "platform",
        "permissions": {"quota_eval": 50, "can_submit": True},
    }, allow_unicode=True))
    (root / "config" / "agents" / "frozen.yaml").write_text(yaml.safe_dump({
        "name": "frozen", "kind": "skill", "driver": "agent", "enabled": False,
    }, allow_unicode=True))
    monkeypatch.setenv("LQ_ROOT", str(root))
    return root


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["agent", *args])


def test_list_agents(agent_root):
    r = _invoke("list")
    assert r.exit_code == 0, r.output
    assert "gp-internal" in r.output
    assert "frozen" in r.output and "disabled" in r.output


def test_list_agents_empty(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    (empty / "config" / "agents").mkdir(parents=True)
    monkeypatch.setenv("LQ_ROOT", str(empty))
    r = _invoke("list")
    assert r.exit_code == 0, r.output
    assert "没有注册 Agent" in r.output


def test_show_agent(agent_root):
    r = _invoke("show", "gp-internal")
    assert r.exit_code == 0, r.output
    assert "kind:      builtin" in r.output
    assert "can_submit: True" in r.output


def test_show_agent_missing(agent_root):
    r = _invoke("show", "nope")
    assert r.exit_code != 0
    assert "Agent 未注册" in r.output


def test_agent_test_probe_pass(agent_root):
    r = _invoke("test", "gp-internal")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["g0"] is True
    assert body["agent"] == "gp-internal"


def test_agent_test_missing(agent_root):
    r = _invoke("test", "nope")
    assert r.exit_code != 0


def test_agent_test_probe_fail(agent_root, monkeypatch):
    import lquant.factors.mining.gates as gates_mod

    r0 = SimpleNamespace(passed=False, reason_code="E_STATIC")
    monkeypatch.setattr(gates_mod, "g0_static", lambda expr, allowed_fields=None: r0)
    r = _invoke("test", "gp-internal")
    assert r.exit_code != 0
    assert "探针校验失败" in r.output


def test_freeze_toggle(agent_root):
    r = _invoke("freeze", "gp-internal")
    assert r.exit_code == 0, r.output
    assert "enabled=False" in r.output
    raw = yaml.safe_load((agent_root / "config/agents/gp-internal.yaml").read_text())
    assert raw["enabled"] is False
    assert "skill_sha256=-" in r.output  # docs/agent-skill/SKILL.md 不存在

    (agent_root / "docs/agent-skill/SKILL.md").write_text("手册")
    r2 = _invoke("freeze", "gp-internal", "--enable")
    assert r2.exit_code == 0, r2.output
    raw2 = yaml.safe_load((agent_root / "config/agents/gp-internal.yaml").read_text())
    assert raw2["enabled"] is True
    assert "skill_sha256=" in r2.output and "-" not in r2.output.split("skill_sha256=")[1]


def test_freeze_missing(agent_root):
    r = _invoke("freeze", "nope")
    assert r.exit_code != 0
    assert "Agent 未注册" in r.output
