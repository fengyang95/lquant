"""运行时配置：settings_store 纯函数（类型白名单/coerce/合并）。"""
from __future__ import annotations

from lquant.core.settings_store import SETTING_DEFS, coerce_setting, _parse


def test_defs_are_registered():
    assert "providers_order" in SETTING_DEFS
    assert "rebalance_default" in SETTING_DEFS
    assert "price_mode_default" in SETTING_DEFS


def test_coerce_bool():
    assert coerce_setting("factor_cache_enabled", "true") == ("true", None)
    assert coerce_setting("factor_cache_enabled", "0") == ("false", None)
    v, err = coerce_setting("factor_cache_enabled", "potato")
    assert err and "布尔" in err


def test_coerce_enum():
    assert coerce_setting("price_mode_default", "next_vwap") == ("next_vwap", None)
    v, err = coerce_setting("price_mode_default", "instant")
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