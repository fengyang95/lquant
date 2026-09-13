"""Agent 注册表（config/agents/*.yaml）解析。

重点回归：注册表路径必须相对**仓库根**解析，不能依赖进程 CWD ——
否则 CLI / server 换个工作目录启动就报"Agent 未注册"，而文件其实一直在仓库里。
"""
from __future__ import annotations

from pathlib import Path

from lquant.core.config import find_root
from lquant.factors.agents import find_agent, load_agents

REPO_AGENTS = find_root() / "config" / "agents"


def test_repo_registry_declares_expected_agents():
    names = {a.name for a in load_agents()}
    assert {"claude-code", "gp-internal", "remote-miner"} <= names


def test_load_agents_is_cwd_independent(tmp_path, monkeypatch):
    """换到任意目录（甚至不存在 config/agents 的临时目录）仍能找到注册表。"""
    before = {a.name for a in load_agents()}
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "config" / "agents").exists()
    assert {a.name for a in load_agents()} == before


def test_find_agent_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = find_agent("gp-internal")
    assert a is not None
    assert a.kind == "builtin"
    assert a.driver == "platform"


def test_find_agent_unknown_returns_none():
    assert find_agent("no-such-agent") is None


def test_load_agents_explicit_absolute_dir(tmp_path):
    """显式绝对路径原样使用（测试/多租户场景），不叠加仓库根。"""
    (tmp_path / "x.yaml").write_text(
        "name: x\nkind: skill\ndriver: agent\npermissions:\n  quota_eval: 7\n",
        encoding="utf-8")
    profs = load_agents(str(tmp_path))
    assert [p.name for p in profs] == ["x"]
    assert profs[0].quota_eval == 7


def test_load_agents_missing_dir_returns_empty(tmp_path):
    assert load_agents(str(tmp_path / "nope")) == []


def test_load_agents_fail_fast_on_bad_kind(tmp_path):
    import pytest

    (tmp_path / "bad.yaml").write_text(
        "name: bad\nkind: nonsense\ndriver: agent\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知 kind"):
        load_agents(str(tmp_path))


def test_freeze_writes_skill_hash_from_repo_root():
    """freeze 的 SKILL.md hash 必须按仓库根定位，与 CWD 无关。"""
    import hashlib

    skill = REPO_AGENTS.parent.parent / "docs" / "agent-skill" / "SKILL.md"
    assert skill.exists()
    assert hashlib.sha256(Path(skill).read_bytes()).hexdigest()[:16]

    # 冻结时写进 claude-code.yaml 的 hash 必须与当前 SKILL.md 一致（未漂移）
    import yaml

    raw = yaml.safe_load((REPO_AGENTS / "claude-code.yaml").read_text(encoding="utf-8"))
    assert raw.get("skill_sha256") == hashlib.sha256(skill.read_bytes()).hexdigest()[:16]
