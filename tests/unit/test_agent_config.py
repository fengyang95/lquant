"""AgentConfig 新字段测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lquant.core.config import AgentConfig


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    # 默认就是内置 Claude Code（「问 AI」= 问 Claude Code）
    assert cfg.provider == "claude_code"
    assert cfg.claude_path == "claude"
    # 第二个无头 CLI provider：OpenAI Codex（同一套骨架，见 agent/codex.py）
    assert cfg.codex_path == "codex"
    assert cfg.workspace_dir == "data/agent_workspace"
    assert cfg.timeout_seconds == 300


def test_agent_config_custom_values() -> None:
    cfg = AgentConfig(
        provider="codex",
        claude_path="/usr/local/bin/claude",
        codex_path="/opt/codex",
        workspace_dir="/tmp/ws",
        timeout_seconds=60,
    )
    assert cfg.provider == "codex"
    assert cfg.claude_path == "/usr/local/bin/claude"
    assert cfg.codex_path == "/opt/codex"
    assert cfg.workspace_dir == "/tmp/ws"
    assert cfg.timeout_seconds == 60


@pytest.mark.parametrize("bad", ["abc", 1.5, None, [30]])
def test_agent_config_timeout_must_be_int(bad: object) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(timeout_seconds=bad)  # type: ignore[arg-type]


# ---- 新会话的全局默认能力（skill / MCP 工具） --------------------------------


def test_capability_defaults_are_unrestricted() -> None:
    """不配 = 不裁剪，行为与改造前一致（老会话与 A2A 会话都落到这条）。"""
    cfg = AgentConfig()
    assert cfg.default_skills is None
    assert cfg.default_mcp_tools is None


@pytest.mark.parametrize("field", ["default_skills", "default_mcp_tools"])
def test_capability_defaults_accept_yaml_list(field: str) -> None:
    cfg = AgentConfig(**{field: ["get_quotes", "get_daily"]})
    assert getattr(cfg, field) == ["get_quotes", "get_daily"]


@pytest.mark.parametrize("field", ["default_skills", "default_mcp_tools"])
def test_capability_defaults_accept_comma_string(field: str) -> None:
    """环境变量插值只会产出字符串，所以要能吃逗号串。"""
    cfg = AgentConfig(**{field: "a-stock-data, factor-mining"})
    assert getattr(cfg, field) == ["a-stock-data", "factor-mining"]


@pytest.mark.parametrize("field", ["default_skills", "default_mcp_tools"])
@pytest.mark.parametrize("raw", ["all", "", "  ", None])
def test_capability_defaults_all_or_blank_means_unrestricted(field: str, raw: object) -> None:
    assert getattr(AgentConfig(**{field: raw}), field) is None


@pytest.mark.parametrize("field", ["default_skills", "default_mcp_tools"])
def test_capability_defaults_empty_list_means_none_enabled(field: str) -> None:
    """空列表 = 一个都不启用，与「不配 = 全开」是两回事，不能合并。"""
    assert getattr(AgentConfig(**{field: []}), field) == []


@pytest.mark.parametrize("field", ["default_skills", "default_mcp_tools"])
def test_capability_defaults_none_sentinel_means_none_enabled(field: str) -> None:
    """显式 `none` 表达「一个都不启用」。

    环境变量只能产出字符串，而空串在这里是「全开」；没有 `none` 这个哨兵，
    「想配成不启用」就没有安全的写法了。
    """
    assert getattr(AgentConfig(**{field: "none"}), field) == []


def test_get_settings_reads_agent_section(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读 app.yaml 的 agent 段（缓存清空即可，不要 evict 模块）。

    回归：这里原来用 `sys.modules.pop("lquant.core.config")` 强制重导入，
    且 finally 里也 pop —— **模块被永久踢出 sys.modules**，下一次 import
    会造出第二个 config 模块对象和第二个 `get_settings`（各自一份 lru_cache）。
    凡是模块级 `from lquant.core.config import get_settings` 的下游（如
    data/store/parquet.py 的 `_root()`）便永远停留在旧对象上、清不掉缓存，
    后续用例读到的 parquet_dir 是一个过期值 —— 表现为
    `parquet._root()` 与 CLI 里现场 import 的 settings 指向两个不同的湖。
    要的是「不吃缓存」，`cache_clear()` 就够了。
    """
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
    import lquant.core.config as mod

    mod.get_settings.cache_clear()
    try:
        s = mod.get_settings()
        assert s.agent.provider == "claude_code"
        assert s.agent.claude_path == "/opt/claude"
        assert s.agent.workspace_dir == "ws"
        assert s.agent.timeout_seconds == 42
    finally:
        mod.get_settings.cache_clear()


def test_config_module_identity_is_stable() -> None:
    """`lquant.core.config` 只能有一个模块实例、一份 `get_settings`。

    evict 模块（`sys.modules.pop`）会造出第二个模块对象与第二份
    lru_cache：模块级 `from lquant.core.config import get_settings` 的下游
    （parquet._root 等）绑定旧对象，`cache_clear()` 清的是新对象，缓存
    永远清不掉 —— 于是「settings 指向哪个湖」在不同调用点悄悄分叉。
    这个不变量一旦破掉，本用例会失败。
    """
    import lquant.core.config as cfg
    from lquant.data.store.parquet import get_settings as store_get_settings

    assert cfg.get_settings is store_get_settings
