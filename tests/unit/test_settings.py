"""运行时配置：settings_store 纯函数 + Settings.agent 派生。"""
from __future__ import annotations

from lquant.core.config import get_settings
from lquant.core.settings_store import SETTING_DEFS, _parse, coerce_setting, defaults


def test_defs_are_registered():
    assert "providers_order" in SETTING_DEFS
    assert "rebalance_default" in SETTING_DEFS
    assert "price_mode_default" in SETTING_DEFS
    assert "agent.provider" in SETTING_DEFS


def test_coerce_bool():
    assert coerce_setting("factor_cache_enabled", "true") == ("true", None)
    assert coerce_setting("factor_cache_enabled", "0") == ("false", None)
    v, err = coerce_setting("factor_cache_enabled", "potato")
    assert err and "布尔" in err


def test_coerce_enum():
    assert coerce_setting("price_mode_default", "next_vwap") == ("next_vwap", None)
    v, err = coerce_setting("price_mode_default", "instant")
    assert err and "取值必须" in err


def test_agent_provider_enum_accepts_codex():
    """两个无头 CLI 后端都必须是合法取值（claude_code 为默认）。"""
    assert coerce_setting("agent.provider", "codex") == ("codex", None)
    assert coerce_setting("agent.provider", "claude_code") == ("claude_code", None)
    assert coerce_setting("agent.provider", "mock") == ("mock", None)
    _v, err = coerce_setting("agent.provider", "gpt")
    assert err and "取值必须" in err


def test_coerce_list():
    assert coerce_setting("providers_order", "baostock,tencent") == ("baostock,tencent", None)
    v, err = coerce_setting("providers_order", "  ,  ")
    assert err and "不能为空" in err


def test_unknown_key_rejected():
    v, err = coerce_setting("no_such", "x")
    assert err and "未知配置项" in err


def test_parse_roundtrip():
    assert _parse(SETTING_DEFS["factor_cache_enabled"], "true") is True
    assert _parse(SETTING_DEFS["price_mode_default"], "same_close") == "same_close"
    assert _parse(SETTING_DEFS["providers_order"], "a,b") == ["a", "b"]


def test_agent_provider_setting_default_derives_from_config(tmp_path, monkeypatch):
    """agent.provider 默认值从 config/app.yaml 派生（source=config），可被 PUT 覆盖。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app.yaml").write_text(
        "agent:\n  provider: claude_code\n", encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        d = defaults()
        assert d["agent.provider"] == ("claude_code", "config")
    finally:
        get_settings.cache_clear()


def test_agent_provider_from_raw(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app.yaml").write_text(
        "agent:\n  provider: x\n", encoding="utf-8")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.agent.provider == "x"
    finally:
        get_settings.cache_clear()