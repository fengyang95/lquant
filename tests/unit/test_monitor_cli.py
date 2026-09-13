"""lq worker：preflight、K clamp、supervisor 子进程数与 respawn。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lquant.monitor import worker as wk


def test_spawn_worker_preflight_fails(monkeypatch, capsys):
    monkeypatch.setattr(wk, "_redis_ok", lambda: False)
    with pytest.raises(SystemExit) as ei:
        wk.spawn_worker("general-0", ["lquant-default"])
    assert ei.value.code == 1
    assert "Redis" in capsys.readouterr().err


def test_supervisor_spawns_expected_children(monkeypatch):
    procs = []

    def fake_spawn(name, queues):
        p = MagicMock()
        p.name = name
        p.is_alive.side_effect = [True] * 3 + [False]  # 第一次检查活着，第二次死
        p.pid = 123
        procs.append(p)
        return p

    monkeypatch.setattr(wk, "_spawn_process", fake_spawn)
    monkeypatch.setattr(wk, "_redis_ok", lambda: True)
    stop_after = {"n": 0}

    def fake_sleep(_):
        stop_after["n"] += 1
        if stop_after["n"] >= 3:
            raise KeyboardInterrupt

    with patch.object(wk.time, "sleep", fake_sleep), \
            patch.object(wk.signal, "signal"), \
            pytest.raises(KeyboardInterrupt):  # 不装真实信号处理器
        wk.run_supervisor(general=1, backtest=2)
    assert len(procs) == 3  # 1 通用 + 2 回测


def test_general_zero_allowed(monkeypatch):
    procs = []

    def fake_spawn(name, queues):
        p = MagicMock()
        p.is_alive.return_value = True
        p.pid = 1
        procs.append(p)
        return p

    monkeypatch.setattr(wk, "_spawn_process", fake_spawn)
    monkeypatch.setattr(wk, "_redis_ok", lambda: True)

    def fake_sleep(_):
        wk.stop_supervisor()

    monkeypatch.setattr(wk.time, "sleep", fake_sleep)
    wk.run_supervisor(general=0, backtest=1)  # stop_supervisor → 循环退出
    assert len(procs) == 1
    assert procs[0].name.startswith("backtest-")


def test_cli_defaults_from_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LQ_BACKTEST_WORKERS", raising=False)
    from lquant.cli.commands.worker import _resolve_counts

    general, backtest = _resolve_counts(general=1, backtest=None)
    assert general == 1 and backtest == 2


def test_cli_clamps(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from lquant.cli.commands.worker import _resolve_counts

    general, backtest = _resolve_counts(general=1, backtest=99)
    assert backtest == 4
