"""回填断点窗口化回归测试(G2 调试发现)。

根因: backfill_daily 固定断点名 "daily" 不含窗口, 换窗口静默 done 0。
"""
from __future__ import annotations

from datetime import date

import pytest

from lquant.core.config import get_settings

pytest.importorskip("duckdb")


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_window_cp_name_differs_across_windows(fake_env):
    from lquant.data.ingest.daily import _window_cp_name

    n1 = _window_cp_name(date(2021, 1, 1), date(2023, 12, 31), True)
    n2 = _window_cp_name(date(2024, 1, 1), date(2025, 12, 31), True)
    n4 = _window_cp_name(date(2021, 1, 1), date(2023, 12, 31), False)
    assert n1 != n2
    assert n1 == _window_cp_name(date(2021, 1, 1), date(2023, 12, 31), True)
    assert n1 != n4
    assert n1.startswith("daily:")


def test_backfill_daily_uses_windowed_checkpoint(fake_env, monkeypatch):
    """旧窗口断点占满时, 新窗口回填不得静默返回 0(不再撞旧断点)。"""
    from lquant.data.ingest import daily as daily_mod

    captured = {}

    def fake_pool(pool, start, end=None, batch_size=1, cp_name="daily", **kw):
        captured["cp_name"] = cp_name
        return {"done": 5, "failed": [], "rows": 5, "early_stopped": False}

    monkeypatch.setattr(daily_mod, "backfill_pool", fake_pool)
    monkeypatch.setattr("lquant.data.store.catalog.SecurityRepo.active_symbols",
                        lambda self, exclude_index=True: ["600000.SH"])
    n = daily_mod.backfill_daily(full=True, start="2021-01-01", end="2023-12-31")
    assert n == 5
    assert captured["cp_name"] == "daily:2021-01-01:2023-12-31:full=True"
