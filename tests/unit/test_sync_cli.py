"""`lq sync` CLI：系统级调度（launchd）入口，不依赖服务器进程。"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]


def _fake_jobs() -> list[dict]:
    return [{"sync_id": "close", "name": "收盘采集", "kind": "collect",
             "schedule_time": "15:05", "weekdays": "1,2,3,4,5",
             "params": {"schedule": "close"}, "enabled": True,
             "last_run_at": None, "last_status": None, "last_rows": None,
             "created_at": "2026-09-16", "updated_at": "2026-09-16"}]


def test_sync_status_lists_jobs(monkeypatch):
    import lquant.cli.commands.sync as sync_cmd

    monkeypatch.setattr(sync_cmd.manager, "list_jobs", _fake_jobs)
    res = CliRunner().invoke(sync_cmd.sync, ["status"])
    assert res.exit_code == 0, res.output
    assert "close" in res.output
    assert "15:05" in res.output


def test_sync_status_prints_freshness_sections(monkeypatch):
    """`status` 必须把三块新鲜度都打出来，缺数据时给出「为什么是 -」。

    last_status=ok 不代表数据是新的（空转/断点全跳过/源零返回都记 ok），
    所以新鲜度是唯一能看出「同步有没有真的在跑」的地方 —— 打不出来这一栏
    就白加了。
    """
    import lquant.cli.commands.sync as sync_cmd

    monkeypatch.setattr(sync_cmd.manager, "list_jobs", _fake_jobs)
    monkeypatch.setattr(sync_cmd.manager, "freshness", lambda: {
        "daily_lake": "2026-09-17", "lag_days": 1,
        "news": {"latest": "2026-09-18 09:05:00", "today_rows": 12},
        "financial_pit": {"covered_start": "2016-01-01",
                          "covered_end": "2026-09-01",
                          "symbols": 5549, "marked": 5549}})
    res = CliRunner().invoke(sync_cmd.sync, ["status"])
    assert res.exit_code == 0, res.output
    assert "日线湖最新交易日: 2026-09-17 （落后 1 个交易日）" in res.output
    assert "资讯最新一条: 2026-09-18 09:05:00（今日 12 条）" in res.output
    assert ("PIT 财务覆盖区间: 2016-01-01 ~ 2026-09-01"
            "（5549 只，记账 5549 只）") in res.output


def test_sync_status_freshness_all_missing(monkeypatch):
    """三块都拿不到 → 逐项显示 `-` + 原因，不能整体报错。"""
    import lquant.cli.commands.sync as sync_cmd

    monkeypatch.setattr(sync_cmd.manager, "list_jobs", _fake_jobs)
    monkeypatch.setattr(sync_cmd.manager, "freshness", lambda: {
        "daily_lake": None, "lag_days": None, "news": None,
        "financial_pit": None})
    res = CliRunner().invoke(sync_cmd.sync, ["status"])
    assert res.exit_code == 0, res.output
    assert "日线湖最新交易日: -" in res.output
    assert "news_item 表未建或无数据" in res.output
    assert "无窗口记账" in res.output


def test_sync_tick_calls_manager(monkeypatch):
    import lquant.cli.commands.sync as sync_cmd

    called = {}

    def fake_tick():
        called["n"] = called.get("n", 0) + 1
        return [{"sync_id": "close", "status": "ok", "rows": 12}]

    monkeypatch.setattr(sync_cmd.manager, "tick", fake_tick)
    res = CliRunner().invoke(sync_cmd.sync, ["tick"])
    assert res.exit_code == 0, res.output
    assert called["n"] == 1
    assert "close ok rows=12" in res.output


def test_sync_run_force_single_job(monkeypatch):
    import lquant.cli.commands.sync as sync_cmd

    picked = {}

    def fake_list_jobs():
        return _fake_jobs()

    def fake_run_job(job):
        picked["id"] = job["sync_id"]
        return {"status": "ok", "rows": 3}

    monkeypatch.setattr(sync_cmd.manager, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(sync_cmd.manager, "run_job", fake_run_job)
    res = CliRunner().invoke(sync_cmd.sync, ["run", "close"])
    assert res.exit_code == 0, res.output
    assert picked["id"] == "close"
    assert "ok" in res.output


def test_sync_run_unknown_job_fails_clearly(monkeypatch):
    import lquant.cli.commands.sync as sync_cmd

    monkeypatch.setattr(sync_cmd.manager, "list_jobs", _fake_jobs)
    res = CliRunner().invoke(sync_cmd.sync, ["run", "nope"])
    assert res.exit_code != 0
    assert "nope" in res.output


def test_launchd_plist_valid_and_scheduled():
    """plist 存在、可解析、含交易日多次触发点（覆盖收盘/盘后窗口）。"""
    import plistlib

    p = REPO / "deploy" / "launchd" / "com.lquant.sync.plist"
    assert p.exists(), "deploy/launchd/com.lquant.sync.plist 缺失"
    with p.open("rb") as f:
        cfg = plistlib.load(f)
    intervals = cfg["StartCalendarInterval"]
    hours = {iv["Hour"] for iv in intervals}
    weekdays = {iv["Weekday"] for iv in intervals}
    assert hours >= {9, 15, 19}          # 盘前补齐 / 收盘后 / 盘后窗口
    assert weekdays == {1, 2, 3, 4, 5}   # 仅工作日（节假日由 _trading_day_ok 过滤）
    args = cfg["ProgramArguments"]
    assert "sync" in args and "tick" in args
