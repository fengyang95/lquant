"""AgentConfig 新字段测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lquant.core.config import AgentConfig


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.provider == "mock"
    assert cfg.claude_path == "claude"
    assert cfg.workspace_dir == "data/agent_workspace"
    assert cfg.timeout_seconds == 300


def test_agent_config_custom_values() -> None:
    cfg = AgentConfig(
        provider="claude_code",
        claude_path="/usr/local/bin/claude",
        workspace_dir="/tmp/ws",
        timeout_seconds=60,
    )
    assert cfg.provider == "claude_code"
    assert cfg.claude_path == "/usr/local/bin/claude"
    assert cfg.workspace_dir == "/tmp/ws"
    assert cfg.timeout_seconds == 60


@pytest.mark.parametrize("bad", ["abc", 1.5, None, [30]])
def test_agent_config_timeout_must_be_int(bad: object) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(timeout_seconds=bad)  # type: ignore[arg-type]


def test_get_settings_reads_agent_section(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app.yaml").write_text(
        "agent:\n"
        "  provider: claude_code\n"
        "  claude_path: /opt/claude\n"
        "  workspace_dir: ws\n"
        "  timeout_seconds: 42\n",
        encoding="utf-8",
    )
    sys.modules.pop("lquant.core.config", None)
    import lquant.core.config as mod

    try:
        s = mod.get_settings()
        assert s.agent.provider == "claude_code"
        assert s.agent.claude_path == "/opt/claude"
        assert s.agent.workspace_dir == "ws"
        assert s.agent.timeout_seconds == 42
    finally:
        sys.modules.pop("lquant.core.config", None)
