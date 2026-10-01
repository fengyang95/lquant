"""Agent Card：frontmatter 派生 skills、坏 skill 跳过不崩、bearer 声明。"""
from __future__ import annotations

from pathlib import Path

import pytest

from lquant.agent.a2a.card import (
    build_agent_card,
    default_description,
    load_skills,
    parse_frontmatter,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_skill(base: Path, name: str, body: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_parse_frontmatter_cases():
    assert parse_frontmatter("---\nname: a\n---\n# body") == {"name": "a"}
    assert parse_frontmatter("# no frontmatter") is None
    assert parse_frontmatter("") is None
    assert parse_frontmatter("---\nname: a\n") is None          # 没闭合
    assert parse_frontmatter("---\nname: [unclosed\n---\n") is None  # YAML 坏
    assert parse_frontmatter("---\n- just-a-list\n---\n") is None  # 不是 mapping


def test_load_skills_skips_broken_entries(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "good", "---\nname: good\ndescription: d\ntags: [x]\n---\n")
    _write_skill(skills, "no-front", "# 没有 frontmatter\n")
    _write_skill(skills, "no-desc", "---\nname: only-name\n---\n")
    (skills / "not-a-dir").write_text("x", encoding="utf-8")     # 文件，不是目录
    (skills / "empty").mkdir()

    got = load_skills(skills)
    assert [s.id for s in got] == ["good"]
    assert got[0].tags == ["x"]


def test_load_skills_tags_fall_back_to_empty_list(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "plain", "---\nname: plain\ndescription: d\n---\n")
    assert load_skills(skills)[0].tags == []


def test_load_skills_missing_dir_returns_empty(tmp_path):
    assert load_skills(tmp_path / "nope") == []


def test_load_skills_on_repo_config():
    """仓库自带的 skill 必须都能进卡片（新增 skill 不用改代码）。"""
    got = load_skills(_REPO_ROOT / "config" / "skills")
    ids = {s.id for s in got}
    assert {"a-stock-data", "factor-mining"} <= ids
    for s in got:
        assert s.description and s.tags


def test_build_agent_card_shape():
    skills = load_skills(_REPO_ROOT / "config" / "skills")
    card = build_agent_card(base_url="http://127.0.0.1:8000", skills=skills,
                            description=default_description("claude_code")).to_a2a()

    assert card["supportedInterfaces"] == [
        {"url": "http://127.0.0.1:8000/a2a", "protocolBinding": "JSONRPC",
         "protocolVersion": "1.0"}]
    assert card["capabilities"]["streaming"] is True
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["provider"]["organization"] == "lquant"
    assert "securitySchemes" not in card


def test_default_description_follows_provider():
    """卡片是给外部调用方看的**发现文档**，不能写死某一个执行体。"""
    assert "Claude Code" in default_description("claude_code")
    assert "Codex" in default_description("codex")
    assert "Claude Code" not in default_description("codex")
    assert "非 LLM" in default_description("mock")
    # 未知取值原样展示（不静默吞掉）
    assert "future_cli" in default_description("future_cli")


def test_description_is_required_not_hardcoded():
    """留默认值就等于留一处会随 provider 过期的假描述，故 description 必填。"""
    with pytest.raises(TypeError):
        build_agent_card(base_url="http://h", skills=[])  # type: ignore[call-arg]


def test_build_agent_card_with_bearer_auth():
    card = build_agent_card(base_url="http://h", skills=[],
                            description=default_description("claude_code"),
                            with_bearer_auth=True).to_a2a()
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert card["securityRequirements"] == [{"bearer": []}]
