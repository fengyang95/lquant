"""workspace.ensure_workspace 的单元测试。"""

import json
from pathlib import Path

import pytest

from lquant.agent.workspace import ensure_workspace


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """模拟仓库根：含 config/skills/。"""
    skills_src = tmp_path / "config" / "skills" / "demo-skill"
    skills_src.mkdir(parents=True)
    (skills_src / "SKILL.md").write_text("# demo", encoding="utf-8")
    return tmp_path


def test_creates_workspace_and_files(root: Path, tmp_path: Path) -> None:
    ws = ensure_workspace("data/agent_workspace/ws1", root)
    assert ws == root / "data/agent_workspace/ws1"
    assert ws.is_dir()
    assert (ws / "CLAUDE.md").is_file()
    assert (ws / ".claude" / "mcp.json").is_file()
    assert (ws / ".claude" / "skills" / "demo-skill" / "SKILL.md").is_file()


def test_claude_md_contains_role_and_priority(root: Path) -> None:
    ws = ensure_workspace("ws", root)
    content = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert "lquant" in content
    assert "MCP" in content
    assert "只读" in content
    assert str(root) in content


def test_mcp_json_structure(root: Path) -> None:
    ws = ensure_workspace("ws", root)
    data = json.loads((ws / ".claude" / "mcp.json").read_text(encoding="utf-8"))
    server = data["mcpServers"]["lquant"]
    assert server["type"] == "stdio"
    assert server["command"] == "python"
    assert server["args"] == ["-m", "lquant.agent.mcp_server"]
    assert server["env"]["LQ_ROOT"] == str(root)
    assert server["env"]["PYTHONPATH"] == str(root / "src")


def test_skills_synced_each_call(root: Path) -> None:
    ws = ensure_workspace("ws", root)
    skills_dir = ws / ".claude" / "skills"
    # 源目录新增文件后再同步，应被拷入
    (root / "config" / "skills" / "demo-skill" / "extra.md").write_text(
        "x", encoding="utf-8"
    )
    ensure_workspace("ws", root)
    assert (skills_dir / "demo-skill" / "extra.md").is_file()
    # 源目录删除文件后再同步，目标不应残留
    (root / "config" / "skills" / "demo-skill" / "extra.md").unlink()
    ensure_workspace("ws", root)
    assert not (skills_dir / "demo-skill" / "extra.md").exists()


def test_idempotent(root: Path) -> None:
    first = ensure_workspace("ws", root)
    second = ensure_workspace("ws", root)
    assert first == second


def test_missing_skills_source_skipped(root: Path) -> None:
    import shutil

    shutil.rmtree(root / "config" / "skills")
    ws = ensure_workspace("ws", root)
    assert not (ws / ".claude" / "skills").exists()
    assert (ws / ".claude" / "mcp.json").is_file()
