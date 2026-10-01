"""Agent Card：frontmatter 派生 skills、坏 skill 跳过不崩、bearer 声明。"""
from __future__ import annotations

from pathlib import Path

from lquant.agent.a2a.card import (
    build_agent_card,
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
    card = build_agent_card(base_url="http://127.0.0.1:8000", skills=skills).to_a2a()

    assert card["supportedInterfaces"] == [
        {"url": "http://127.0.0.1:8000/a2a", "protocolBinding": "JSONRPC",
         "protocolVersion": "1.0"}]
    assert card["capabilities"]["streaming"] is True
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["provider"]["organization"] == "lquant"
    assert "securitySchemes" not in card


def test_build_agent_card_with_bearer_auth():
    card = build_agent_card(base_url="http://h", skills=[],
                            with_bearer_auth=True).to_a2a()
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert card["securityRequirements"] == [{"bearer": []}]
