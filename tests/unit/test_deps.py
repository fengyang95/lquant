"""server.deps 单元测试。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from lquant.server import deps


@pytest.fixture
def clear_settings_cache():
    deps.settings.cache_clear()
    yield
    deps.settings.cache_clear()


def test_settings_cached(clear_settings_cache):
    s1 = deps.settings()
    s2 = deps.settings()
    assert s1 is s2


def test_provider_returns_provider(clear_settings_cache):
    p = deps.provider()
    from lquant.data.providers import get_provider

    assert p is get_provider()


def test_resolve_symbol_variants(clear_settings_cache):
    assert deps.resolve_symbol("600519") == "600519.SH"
    assert deps.resolve_symbol("600519.SH") == "600519.SH"
    assert deps.resolve_symbol("sh.600519") == "600519.SH"
    assert deps.resolve_symbol("000001") == "000001.SZ"


def test_resolve_symbol_invalid_raises_422(clear_settings_cache):
    with pytest.raises(HTTPException) as ei:
        deps.resolve_symbol("not-a-code")
    assert ei.value.status_code == 422
    assert "not-a-code" in ei.value.detail


def test_bare_code():
    assert deps.bare_code("600519.SH") == "600519"
    # 只有一个点之前的部分，后续点保留
    assert deps.bare_code("600519.SH.x") == "600519"
    assert deps.bare_code("600519") == "600519"
