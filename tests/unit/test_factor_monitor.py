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


def test_missing_ic_table_self_heals(tmp_path, monkeypatch):
    """老库缺 ``factor_ic_daily`` 表：写路径与 ``ic-health`` 读路径都要自愈。

    表只在 ``DDL_STATEMENTS`` 里（init_db / 服务启动才执行）—— 修复前写路径
    报 CatalogException（sync 结果里逐条 error），``lq factor ic-health`` 直接
    以裸驱动异常退出。
    """
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    con.execute("CREATE TABLE unrelated (x INTEGER)")  # 库在，但没建 factor_ic_daily
    con.close()

    from lquant.factors import monitor

    # 读路径：无表 → 自愈后按「无记录」回答，不是 CatalogException
    assert monitor.factor_health("ghost")[0]["verdict"] == "no_data"
    # 写路径：落表成功
    rows = pl.DataFrame(
        {
            "trade_date": [date(2026, 6, 1), date(2026, 6, 2)],
            "ic": [0.1, 0.2],
            "rank_ic": [0.1, 0.2],
            "n": [8, 8],
        }
    )
    assert monitor.upsert_ic_daily(rows, factor="healed") == 2
    h = {x["factor"]: x for x in monitor.factor_health("healed")}["healed"]
    assert h["n_days"] == 2 and h["mean_ic"] == pytest.approx(0.15)
    get_settings.cache_clear()


def _nan_ic_rows(n: int = 3) -> pl.DataFrame:
    """全 NaN 的 IC 序列：截面内因子是常数时 pl.corr 的真实返回。"""
    d0 = date(2026, 6, 1)
    return pl.DataFrame(
        {
            "trade_date": [d0 + timedelta(days=i) for i in range(n)],
            "ic": [float("nan")] * n,
            "rank_ic": [float("nan")] * n,
            "n": [8] * n,
        }
    )


def test_health_all_nan_ic_is_degraded_not_ok(monitor_env):
    """窗口内 IC 全为 NaN（因子退化成常数）→ degraded，绝不判 ok。

    NaN 参与 mean/std 会让 ``mean_ic < min_ic`` 与 ``icir < min_icir`` 恒为
    False —— 修复前这种因子显示 ok，监控对它完全失明。
    """
    monitor_env.upsert_ic_daily(_nan_ic_rows(), factor="flat_demo")
    h = {x["factor"]: x for x in monitor_env.factor_health("flat_demo", window=20)}["flat_demo"]
    assert h["verdict"] == "degraded"
    assert h["n_days"] == 3  # 记录在（staleness 仍看得见）
    assert h["n_valid"] == 0  # 但一条有限 IC 都没有
    assert h["mean_ic"] is None
    assert "非有限" in h["detail"]


def test_health_mixed_nan_rows_count_only_finite(monitor_env):
    """有限 + NaN 混合：统计只看有限行，且 staleness 按有限行数判。"""
    rows = pl.DataFrame(
        {
            "trade_date": [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)],
            "ic": [0.2, float("nan"), float("nan")],
            "rank_ic": [0.1, float("nan"), float("nan")],
            "n": [8, 8, 8],
        }
    )
    monitor_env.upsert_ic_daily(rows, factor="mixed_demo")
    h = {x["factor"]: x for x in monitor_env.factor_health("mixed_demo", window=20)}["mixed_demo"]
    # 1 条有限 IC < max(2, 20//2)=10 → stale（不是被 NaN 拉低成 ok/degraded）
    assert h["verdict"] == "stale"
    assert h["n_valid"] == 1 and h["n_days"] == 3
    assert "仅 1 日有有效 IC" in h["detail"]


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


def test_run_daily_check_alerts_on_degenerate_factor(monitor_env):
    """全 NaN IC 的退化因子必须触发 ic_below 告警（含通知真的发出）。

    修复前 ctx 只收 ``mean_ic is not None`` 的健康行，退化因子（mean_ic=None）
    连规则引擎都进不去；即便进去，``nan <= threshold`` 也恒为 False —— 双重
    静默。ic=None/degraded 的 ctx 现在必须命中并说明原因。
    """
    from lquant.notify.rules import TRIGGERED, AlertRule, get_store

    monitor_env.upsert_ic_daily(_nan_ic_rows(), factor="flat_demo")
    store = get_store()
    store.add(
        AlertRule(
            name="flat 失效告警",
            target="flat_demo",
            alert_type="ic_below",
            parameters={"threshold": 0.0},
            cooldown_seconds=0,
        )
    )
    seen = []
    out = monitor_env.run_daily_check(
        factors=["flat_demo"], notify_fn=lambda *a, **k: seen.append(a)
    )
    hits = [a for a in out["alerts"] if a["status"] == TRIGGERED]
    assert hits, out["alerts"]
    assert "缺失/非有限" in hits[0]["detail"]
    assert hits[0]["detail"].startswith("flat_demo")
    assert len(seen) == 1


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
