"""monitor 配置段：字段默认值 + 环境变量覆盖 + 回测 worker 数 clamp。"""
from __future__ import annotations

from lquant.core.config import BACKTEST_WORKERS_MAX, clamp_backtest_workers


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_MONITOR_ENABLED", raising=False)
    monkeypatch.delenv("LQ_MONITOR_DB", raising=False)
    monkeypatch.delenv("LQ_BACKTEST_WORKERS", raising=False)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.monitor_enabled is True
    assert s.monitor_flush_interval_sec == 10
    assert s.monitor_sample_interval_sec == 5
    assert s.monitor_retention_days == 7
    assert s.backtest_workers == 2
    assert BACKTEST_WORKERS_MAX == 4
    assert s.monitor_db_path.endswith("lquant.monitor.duckdb")
    assert str(tmp_path) in s.monitor_db_path  # 与 duckdb_path 同目录


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    monkeypatch.setenv("LQ_BACKTEST_WORKERS", "99")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.monitor_enabled is False
    assert s.monitor_db_path == str(tmp_path / "mon.duckdb")
    assert clamp_backtest_workers(s.backtest_workers) == BACKTEST_WORKERS_MAX
    get_settings.cache_clear()


def test_clamp():
    assert clamp_backtest_workers(-1) == 0
    assert clamp_backtest_workers(0) == 0
    assert clamp_backtest_workers(3) == 3
    assert clamp_backtest_workers(99) == BACKTEST_WORKERS_MAX
