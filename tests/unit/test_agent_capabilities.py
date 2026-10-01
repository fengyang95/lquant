"""agent.capabilities：能力配置校验 + skill 文件读写（路径安全在单元层验）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from lquant.agent import capabilities as cap


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: 描述\ntags: [a, b]\n---\n\n# alpha\n",
        encoding="utf-8")
    return tmp_path


# ---- normalize_agent_config ----------------------------------------------


def test_normalize_keeps_only_present_fields():
    assert cap.normalize_agent_config({}) == {}
    assert cap.normalize_agent_config({"context": {"symbol": "600519"}}) == {}


def test_normalize_accepts_full_config():
    cfg = cap.normalize_agent_config(
        {"provider": "codex", "skills": ["alpha"], "mcp_tools": ["get_quotes"]})
    assert cfg == {"provider": "codex", "skills": ["alpha"], "mcp_tools": ["get_quotes"]}


def test_normalize_strips_whitespace():
    assert cap.normalize_agent_config({"skills": [" alpha "]}) == {"skills": ["alpha"]}


def test_normalize_distinguishes_none_from_empty_list():
    """``None`` = 全开；``[]`` = 一个都不启用。合并两者会让「全不选」变成「全给」。"""
    assert cap.normalize_agent_config({"skills": None}) == {"skills": None}
    assert cap.normalize_agent_config({"skills": []}) == {"skills": []}


def test_normalize_rejects_unknown_provider():
    with pytest.raises(cap.CapabilityError, match="未知 provider"):
        cap.normalize_agent_config({"provider": "gpt"})


def test_normalize_rejects_unknown_mcp_tool():
    with pytest.raises(cap.CapabilityError, match="未知 MCP 工具"):
        cap.normalize_agent_config({"mcp_tools": ["get_quotes", "get_nope"]})


def test_normalize_rejects_non_list_names():
    with pytest.raises(cap.CapabilityError, match="必须是字符串列表"):
        cap.normalize_agent_config({"skills": "alpha"})


# ---- skill 名与路径安全 ---------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "..", "a/b", "/abs", "A-B", "a b", "", "-x"])
def test_write_rejects_bad_skill_name(root: Path, bad: str):
    """**写入**（新建/改名）必须过形状校验。"""
    with pytest.raises(cap.CapabilityError, match="非法 skill 名"):
        cap.write_skill(root, bad, "---\nname: x\ndescription: y\n---\n")


@pytest.mark.parametrize("bad", ["../escape", "..", "a/b", "/abs", "A-B", "a b", "", "-x"])
def test_read_delete_of_bad_name_is_not_found(root: Path, bad: str):
    """读/删不做形状校验，只认「是不是 skills/ 的直接子目录」—— 不存在的就是 404。"""
    with pytest.raises(FileNotFoundError, match="skill 不存在"):
        cap.read_skill(root, bad)
    with pytest.raises(FileNotFoundError, match="skill 不存在"):
        cap.delete_skill(root, bad)


def test_bad_named_skill_is_still_readable_and_deletable(root: Path):
    """名字不合规的 skill 被列出来了（valid=False），就必须能读能删 ——
    「列出来却什么都干不了」比不列更糟（用户只能干瞪眼）。"""
    d = root / "config" / "skills" / "Bad_Name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: d\n---\n",
                                encoding="utf-8")
    assert "Bad_Name" in cap.read_skill(root, "Bad_Name")
    cap.delete_skill(root, "Bad_Name")
    assert not d.exists()


def test_traversal_name_cannot_reach_outside_skills_dir(root: Path):
    """`../secret` 这类名字解析后父目录不是 skills/，读/删都够不着。"""
    outside = root / "secret"
    outside.mkdir()
    (outside / "SKILL.md").write_text("机密", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        cap.delete_skill(root, "../secret")
    with pytest.raises(FileNotFoundError):
        cap.read_skill(root, "../secret")
    assert (outside / "SKILL.md").is_file()


# ---- skill 读写 -----------------------------------------------------------


def test_roundtrip(root: Path):
    cap.write_skill(root, "beta", "---\nname: beta\ndescription: d\n---\n")
    assert cap.read_skill(root, "beta").startswith("---\nname: beta")
    cap.delete_skill(root, "beta")
    with pytest.raises(FileNotFoundError):
        cap.read_skill(root, "beta")


def test_read_missing_raises(root: Path):
    with pytest.raises(FileNotFoundError, match="skill 不存在"):
        cap.read_skill(root, "nope")


def test_delete_missing_raises(root: Path):
    with pytest.raises(FileNotFoundError, match="skill 不存在"):
        cap.delete_skill(root, "nope")


@pytest.mark.parametrize("content,msg", [
    ("no frontmatter", "frontmatter"),
    ("---\nname: x\n---\n", "description"),
    ("---\ndescription: y\n---\n", "name"),
    ("---\n- just\n- a list\n---\n", "frontmatter"),  # 非映射的 frontmatter 按缺失处理
])
def test_write_rejects_bad_content(root: Path, content: str, msg: str):
    """缺 name/description 的 skill 对外不可见 —— 存下来等于让用户静默踩坑。"""
    with pytest.raises(cap.CapabilityError, match=msg):
        cap.write_skill(root, "beta", content)
    assert not (root / "config" / "skills" / "beta").exists()


def test_write_rejection_leaves_existing_file_intact(root: Path):
    before = cap.read_skill(root, "alpha")
    with pytest.raises(cap.CapabilityError):
        cap.write_skill(root, "alpha", "坏的")
    assert cap.read_skill(root, "alpha") == before


# ---- 清单 -----------------------------------------------------------------


def test_list_skills_marks_broken_ones(root: Path):
    """坏 skill 也要列出来（标 valid=False）—— 藏起来用户就永远修不好它。"""
    d = root / "config" / "skills" / "broken"
    d.mkdir()
    (d / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
    listed = {s["name"]: s for s in cap.list_skills(root)}
    assert listed["alpha"]["valid"] is True
    assert listed["alpha"]["tags"] == ["a", "b"]
    assert listed["broken"]["valid"] is False


def test_list_skills_rejects_names_the_validator_would_reject(root: Path):
    """目录名不合规的 skill 必须标 valid=False。

    否则前端「全选」会把它勾上，建会话时被 normalize_agent_config 以
    「非法 skill 名」拒掉 —— 用户在界面上看到的却是个正常条目，无从下手。
    清单与校验器必须同一口径。
    """
    d = root / "config" / "skills" / "Bad_Name"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: 看起来很正常\n---\n", encoding="utf-8")
    listed = {s["name"]: s for s in cap.list_skills(root)}
    assert listed["Bad_Name"]["description"] == "看起来很正常"
    assert listed["Bad_Name"]["valid"] is False
    with pytest.raises(cap.CapabilityError, match="非法 skill 名"):
        cap.normalize_agent_config({"skills": ["Bad_Name"]})


def test_list_skills_empty_when_dir_missing(tmp_path: Path):
    assert cap.list_skills(tmp_path) == []


def test_list_mcp_tools_matches_registry():
    from lquant.agent.mcp_server import TOOL_HANDLERS

    assert {t["name"] for t in cap.list_mcp_tools()} == set(TOOL_HANDLERS)
    assert all(t["description"] for t in cap.list_mcp_tools())
