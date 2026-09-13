"""SecurityRepo.active_symbols 池构成：日线回填必须能剔除指数。"""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_security() -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security ("
            "symbol VARCHAR PRIMARY KEY, name VARCHAR, sec_type VARCHAR, "
            "board VARCHAR, list_date DATE, delist_date DATE, is_st BOOLEAN, "
            "source VARCHAR, updated_at TIMESTAMP)"
        )
        for sym, st in [
            ("000001.SH", "index"),
            ("000001.SZ", "stock"),
            ("510300.SH", "etf"),
            ("160323.SZ", "lof"),
        ]:
            con.execute(
                "INSERT INTO security VALUES (?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL)",
                [sym, st],
            )


def test_active_symbols_excludes_index_on_request(fake_settings):
    _seed_security()
    from lquant.data.store.catalog import SecurityRepo

    all_syms = SecurityRepo().active_symbols()
    assert "000001.SH" in all_syms  # 默认行为不变（jq_shim 语义不动）
    no_idx = SecurityRepo().active_symbols(exclude_index=True)
    assert "000001.SH" not in no_idx
    assert set(no_idx) == {"000001.SZ", "510300.SH", "160323.SZ"}
