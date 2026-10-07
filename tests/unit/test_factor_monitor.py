"""因子在线监控闭环（factors/monitor.py）：IC 日表落库 + 健康评估 + 告警编排。

隔离姿势沿用 test_backtest_runs：LQ_ROOT + chdir + cache_clear + DDL 全量建表；
数据读取 (_panel) 用 monkeypatch 注入合成面板，不依赖湖里有没有真数据。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

_SYMBOLS = ("600000", "000001", "300750", "600036", "000002", "601318", "600519", "000333")


def _panel_df(n_days: int = 30) -> pl.DataFrame:
    """每股固定漂移的指数增长价 → mom5 与 fwd_ret_1 截面完全单调（IC=1）。"""
    d0 = date(2026, 6, 1)
    rows = []
    for i in range(n_days):
        d = d0 + timedelta(days=i)
        for j, s in enumerate(_SYMBOLS):
            c = 10.0 * (1 + 0.001 * (j + 1)) ** (i + 1)
            rows.append(
                {
                    "trade_date": d,
                    "symbol": s,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "pre_close": c,
                    "volume": 1e5,
                    "amount": 1e5 * c,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_NOTIFY_DB", str(tmp_path / "notify" / "rules.db"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.execute(
        "INSERT INTO factor_def (name, expression, enabled, created_at) "
        "VALUES ('mom5_demo', 'pct_change_5', true, now())"
    )
    con.execute(
        "INSERT INTO factor_def (name, expression, enabled, created_at) "
        "VALUES ('broken_demo', '$nope_col + $close', true, now())"
    )
    con.close()

    from lquant.factors import monitor

    monkeypatch.setattr(monitor, "_panel", lambda start=None, end=None: _panel_df())
    yield monitor
    get_settings.cache_clear()


def _daily_rows(monitor, factor):
    from lquant.core.db import reader

    with reader() as con:
        return con.execute(
            "SELECT trade_date, ic, rank_ic, n FROM factor_ic_daily "
            "WHERE factor = ? ORDER BY trade_date",
            [factor],
        ).pl()


# ---------------- sync_factor_ic ----------------


def test_sync_writes_daily_ic_and_reads_expression_from_def(monitor_env):
    r = monitor_env.sync_factor_ic("mom5_demo")
    assert r["expression"] == "pct_change_5"
    assert r["n_days"] > 10
    rows = _daily_rows(monitor_env, "mom5_demo")
    assert len(rows) == r["n_days"]
    # 每股严格指数增长 → 截面 IC 恒为 1
    assert rows["ic"].min() > 0.999
    assert rows["n"].min() >= 5


def test_sync_is_idempotent_on_same_window(monitor_env):
    a = monitor_env.sync_factor_ic("mom5_demo")
    b = monitor_env.sync_factor_ic("mom5_demo")
    assert a["n_days"] == b["n_days"]
    assert len(_daily_rows(monitor_env, "mom5_demo")) == a["n_days"]


def test_sync_unknown_factor_fails_loudly(monitor_env):
    with pytest.raises(KeyError):
        monitor_env.sync_factor_ic("ghost")


def test_sync_empty_panel_records_detail(monitor_env, monkeypatch):
    monkeypatch.setattr(monitor_env, "_panel", lambda start=None, end=None: _panel_df().head(0))
    r = monitor_env.sync_factor_ic("mom5_demo")
    assert r["n_days"] == 0 and "日线数据为空" in r["detail"]


def test_upsert_rejects_missing_columns(monitor_env):
    with pytest.raises(KeyError):
        monitor_env.upsert_ic_daily(pl.DataFrame({"trade_date": [date(2026, 6, 1)]}), factor="x")


def test_upsert_replaces_snapshot_instead_of_appending(monitor_env):
    # 同因子同日期重复 upsert → 快照替换，不是追加（主键约束会炸追加语义）
    rows = pl.DataFrame(
        {
            "trade_date": [date(2026, 6, 1), date(2026, 6, 2)],
            "ic": [0.1, 0.2],
            "rank_ic": [0.1, 0.2],
            "n": [8, 8],
        }
    )
    monitor_env.upsert_ic_daily(rows, factor="f1")
    rows2 = rows.with_columns(pl.col("ic") + 1.0)
    monitor_env.upsert_ic_daily(rows2, factor="f1")
    got = _daily_rows(monitor_env, "f1")
    assert len(got) == 2
    assert got["ic"].to_list() == [1.1, 1.2]


# ---------------- factor_health ----------------


def test_health_ok_and_degraded_and_no_data(monitor_env):
    monitor_env.sync_factor_ic("mom5_demo")
    hs = {h["factor"]: h for h in monitor_env.factor_health(min_ic=0.0)}
    assert hs["mom5_demo"]["verdict"] == "ok"
    assert hs["mom5_demo"]["n_days"] > 10
    assert hs["mom5_demo"]["last_date"] is not None

    # 门槛抬到 IC 永远够不到的高度 → degraded，原因带数值可见
    hs2 = {h["factor"]: h for h in monitor_env.factor_health(min_ic=1.5)}
    assert hs2["mom5_demo"]["verdict"] == "degraded"
    assert "mean_ic" in hs2["mom5_demo"]["detail"]

    # 从未同步过的因子 → no_data
    hs3 = {h["factor"]: h for h in monitor_env.factor_health("no_such")}
    assert hs3["no_such"]["verdict"] == "no_data"


def test_health_stale_when_sync_gap(monitor_env, monkeypatch):
    # 8 天面板只产出 2 行有效 IC（shift5/fwd_ret 边界）→ 覆盖不足一半窗口 →
    # stale（同步断了要看得见，而不是装作一切正常）
    monkeypatch.setattr(monitor_env, "_panel", lambda start=None, end=None: _panel_df(n_days=8))
    monitor_env.sync_factor_ic("mom5_demo")
    hs = monitor_env.factor_health(window=20)
    assert hs[0]["verdict"] == "stale"
    assert "仅 2 日" in hs[0]["detail"]


def test_health_rejects_bad_window(monitor_env):
    with pytest.raises(ValueError):
        monitor_env.factor_health(window=0)


# ---------------- run_daily_check 编排 ----------------


def test_run_daily_check_end_to_end_with_alert(monitor_env):
    from lquant.notify.rules import TRIGGERED, AlertRule, get_store

    store = get_store()
    store.add(
        AlertRule(
            name="mom5 失效告警",
            target="mom5_demo",
            alert_type="ic_below",
            parameters={"threshold": 1.5},
            cooldown_seconds=0,
        )
    )
    seen = []

    out = monitor_env.run_daily_check(
        factors=["mom5_demo", "broken_demo"], notify_fn=lambda *a, **k: seen.append(a)
    )

    # 单因子表达式炸了不阻断：error 逐条可见
    assert len(out["errors"]) == 1
    assert out["errors"][0]["factor"] == "broken_demo"
    assert "error" in out["errors"][0]
    # 健康评估覆盖正常因子
    hs = {h["factor"]: h for h in out["health"]}
    assert hs["mom5_demo"]["verdict"] == "ok"
    assert "broken_demo" not in hs or hs["broken_demo"]["verdict"] == "no_data"
    # ic_below 规则被 ctx（ic=1.0 <= threshold 1.5）触发，通知真的发出
    assert any(a["status"] == TRIGGERED for a in out["alerts"])
    assert len(seen) >= 1
    assert store.list(enabled_only=True)[0].last_triggered_at is not None


def test_run_daily_check_defaults_to_enabled_factors(monitor_env):
    out = monitor_env.run_daily_check(notify_fn=lambda *a, **k: None)
    # 缺省取 factor_def 全部启用因子（含会炸的 broken_demo —— error 可见）
    names = {r.get("factor") for r in out["synced"]} | {e["factor"] for e in out["errors"]}
    assert {"mom5_demo", "broken_demo"} <= names


def test_run_daily_check_empty_def_is_honest(monitor_env):
    from lquant.core.db import writer

    with writer() as con:
        con.execute("DELETE FROM factor_def")
    out = monitor_env.run_daily_check(notify_fn=lambda *a, **k: None)
    assert out["synced"] == [] and out["errors"] == []
