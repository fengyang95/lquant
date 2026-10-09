"""workspace.ensure_workspace 的单元测试。"""

import json
import sys
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


def test_writes_agents_md_for_codex_with_same_content(root: Path) -> None:
    """claude 读 CLAUDE.md、codex 读 AGENTS.md —— 两个都要有，且内容同源。

    只写一个的话，另一个 provider 起来就是个「没有口径的裸 agent」。
    """
    ws = ensure_workspace("ws", root)
    claude_md = ws / "CLAUDE.md"
    agents_md = ws / "AGENTS.md"
    assert claude_md.is_file() and agents_md.is_file()
    assert agents_md.read_text(encoding="utf-8") == claude_md.read_text(encoding="utf-8")
    assert "MCP" in agents_md.read_text(encoding="utf-8")


def test_mcp_server_spec_is_shared_between_providers(root: Path) -> None:
    """codex 的 -c 覆盖项由 mcp_server_spec 生成，不能是第二份手写配置。"""
    from lquant.agent.workspace import mcp_server_spec

    spec = mcp_server_spec(root, python="/opt/python")
    assert spec["command"] == "/opt/python"
    assert spec["args"] == ["-m", "lquant.agent.mcp_server"]
    assert spec["env"] == {"LQ_ROOT": str(root), "PYTHONPATH": str(root / "src")}
    # 不传 python 时退回当前解释器（与 mcp.json 一致）
    assert mcp_server_spec(root)["command"] == sys.executable


def test_claude_md_contains_role_and_priority(root: Path) -> None:
    ws = ensure_workspace("ws", root)
    content = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert "lquant" in content
    assert "MCP" in content
    assert "只读" in content
    assert str(root) in content


def test_claude_md_has_single_priority_order_and_api_base(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指引只能有一处：MCP → skill/HTTP → CLI → 临时脚本，且写明 API base。

    历史坑：CLAUDE.md 只说 MCP/CLI，skill 只说 HTTP 且 base 硬编码
    localhost:8000，--append-system-prompt 又是第三套说法 —— agent 只能自己猜。
    """
    monkeypatch.setenv("LQ_API_HOST", "127.0.0.1")
    monkeypatch.setenv("LQ_API_PORT", "8123")
    ws = ensure_workspace("ws", root)
    content = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    for token in ("MCP 工具", ".claude/skills", "lquant CLI", "临时 python 脚本"):
        assert token in content, token
    assert "http://127.0.0.1:8123" in content
    # 明确不许调触发写入的端点
    assert "只读端点" in content


def test_skill_api_base_placeholder_is_substituted(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = root / "config" / "skills" / "demo-skill" / "SKILL.md"
    skill.write_text("base: ${LQ_API_BASE}/api\n", encoding="utf-8")
    monkeypatch.setenv("LQ_API_HOST", "127.0.0.1")
    monkeypatch.setenv("LQ_API_PORT", "8123")

    ws = ensure_workspace("ws", root)
    copied = (ws / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text(
        encoding="utf-8")
    assert copied == "base: http://127.0.0.1:8123/api\n"
    # 仓库里的源文件不被改动（占位符留在模板里）
    assert "${LQ_API_BASE}" in skill.read_text(encoding="utf-8")


def test_real_repo_skills_sync(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实仓库的两套 skill：外部 a-stock-data **原样**拷入（无占位符、不被改写），
    本地 lquant-market 的 ${LQ_API_BASE} 被替换成本机地址。

    直接调 _sync_skills（workspace 与 root 分开传），免得 ensure_workspace 把
    工作区写进真实仓库的 data/ 下。
    """
    from lquant.agent.workspace import _sync_skills

    monkeypatch.setenv("LQ_API_HOST", "127.0.0.1")
    monkeypatch.setenv("LQ_API_PORT", "8123")
    repo = Path(__file__).resolve().parents[2]
    ws = tmp_path / "ws"
    ws.mkdir()

    _sync_skills(ws, repo)

    skills = ws / ".claude" / "skills"
    external = (skills / "a-stock-data" / "SKILL.md").read_text(encoding="utf-8")
    source = (repo / "config" / "skills" / "a-stock-data" / "SKILL.md").read_text(
        encoding="utf-8")
    assert external == source, "外部 vendored skill 必须原样拷入，不能被改写"

    local = (skills / "lquant-market" / "SKILL.md").read_text(encoding="utf-8")
    assert "${LQ_API_BASE}" not in local
    assert "http://127.0.0.1:8123" in local


def test_mcp_json_structure(root: Path) -> None:
    ws = ensure_workspace("ws", root)
    data = json.loads((ws / ".claude" / "mcp.json").read_text(encoding="utf-8"))
    server = data["mcpServers"]["lquant"]
    assert server["type"] == "stdio"
    assert server["command"] == sys.executable
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


# ---- 按会话启用集裁剪能力 ---------------------------------------------------


def _add_skill(root: Path, name: str) -> None:
    d = root / "config" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}", encoding="utf-8")


def test_enabled_skills_filters_copy(root: Path) -> None:
    """只拷启用的 skill：未启用的目录不进工作区，agent 就看不到它。"""
    _add_skill(root, "factor-mining")
    ws = ensure_workspace("ws", root, enabled_skills={"factor-mining"})
    skills = ws / ".claude" / "skills"
    assert (skills / "factor-mining" / "SKILL.md").is_file()
    assert not (skills / "demo-skill").exists()


def test_enabled_skills_none_copies_all(root: Path) -> None:
    """None = 不限制（老调用方与 A2A 会话走这条，行为与改造前一致）。"""
    _add_skill(root, "factor-mining")
    ws = ensure_workspace("ws", root, enabled_skills=None)
    skills = ws / ".claude" / "skills"
    assert (skills / "demo-skill").is_dir()
    assert (skills / "factor-mining").is_dir()


def test_enabled_skills_empty_copies_none_but_keeps_dir(root: Path) -> None:
    """空集 = 一个都不启用；目录保留（CLAUDE.md 指向它，空目录比没有更少歧义）。"""
    ws = ensure_workspace("ws", root, enabled_skills=set())
    skills = ws / ".claude" / "skills"
    assert skills.is_dir()
    assert list(skills.iterdir()) == []


def test_enabled_skills_cannot_escape_source_dir(root: Path) -> None:
    """启用集来自请求体/数据库，不能靠 `../` 把任意目录拷进工作区。"""
    outside = root / "secret"
    outside.mkdir()
    (outside / "SKILL.md").write_text("机密", encoding="utf-8")
    ws = ensure_workspace("ws", root, enabled_skills={"../secret", "demo-skill"})
    skills = ws / ".claude" / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["demo-skill"]


def test_mcp_server_spec_carries_enabled_tools(root: Path) -> None:
    from lquant.agent.workspace import VERDICT_TOOL, mcp_server_spec

    spec = mcp_server_spec(root, enabled_tools={"get_quotes", "get_daily"})
    # submit_verdict 永远在名单里：它是一条输出通道，不是数据访问权限
    assert spec["env"]["LQ_MCP_ENABLED_TOOLS"] == f"get_daily,get_quotes,{VERDICT_TOOL}"
    # 空集要写成**存在但非全开**：缺失 = 全开，名单 = 只开名单里那几个，
    # 两者语义不同，不能合并成一个空值（结论通道除外，理由同上）
    assert mcp_server_spec(root, enabled_tools=set())["env"]["LQ_MCP_ENABLED_TOOLS"] \
        == VERDICT_TOOL
    # 不传 = 不注入该变量（老调用方行为不变）
    assert "LQ_MCP_ENABLED_TOOLS" not in mcp_server_spec(root)["env"]


def test_mcp_spec_injects_session_binding(root: Path) -> None:
    """MCP 子进程要能落结论：会话 id 与 ask.db 路径都靠环境变量传。"""
    from lquant.agent.workspace import mcp_server_spec

    spec = mcp_server_spec(root, session_id="sid-7")
    assert spec["env"]["LQ_AGENT_SESSION_ID"] == "sid-7"
    assert spec["env"]["LQ_ASK_DB"] == str(root / "data" / "ask.db")
    # 不传会话时保持旧形态（不回退到"猜一个"）
    assert "LQ_AGENT_SESSION_ID" not in mcp_server_spec(root)["env"]


def test_whitelist_always_allows_submit_verdict(root: Path) -> None:
    """白名单裁的是数据访问；结论通道被裁掉只会让 agent 退回自由文本。"""
    from lquant.agent.workspace import mcp_server_spec

    spec = mcp_server_spec(root, enabled_tools={"get_daily"})
    assert spec["env"]["LQ_MCP_ENABLED_TOOLS"] == "get_daily,submit_verdict"
    # 空集也是合法配置（一个数据工具都不给），但结论通道仍在
    empty = mcp_server_spec(root, enabled_tools=set())
    assert empty["env"]["LQ_MCP_ENABLED_TOOLS"] == "submit_verdict"
    # None = 不裁剪，不注入该变量
    assert "LQ_MCP_ENABLED_TOOLS" not in mcp_server_spec(root)["env"]


def test_workspace_mcp_json_carries_session_binding(root: Path) -> None:
    ws = ensure_workspace("ws-bind", root, session_id="sid-7")
    cfg = json.loads((ws / ".claude" / "mcp.json").read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["lquant"]["env"]
    assert env["LQ_AGENT_SESSION_ID"] == "sid-7"
