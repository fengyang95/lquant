"""M4c submit 重验回归。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest


@pytest.fixture
def submit_env(tmp_path, monkeypatch):
    """隔离 DuckDB（DDL 全量建表），专供需要真实 factor_def 表的用例。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()
    yield
    get_settings.cache_clear()


def _panel(n_days=120, n_sym=8):
    rng = np.random.default_rng(11)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "volume": float(1e5 + i), "amount": px * 1e4,
                         "turnover_rate": 0.5})
    return pl.DataFrame(rows)


def test_verify_rejects_static_fail():
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register({"name": "bad", "expr": "Ts_Mean($closs,5)", "rationale": "动量效应检验"})
    assert not ok and payload["grade"] == "REJECTED"
    assert payload["reason_code"] == "STATIC_FAIL"


def test_verify_grades_claimed(monkeypatch, tmp_path):
    """claimed 与重算值的分级逻辑：A/B 入库，C/D 归档。"""
    import lquant.factors.mining.submit as sub

    def fake_split(df, covs, expr):
        import polars as pl

        # IC 序列必须有方差：常数序列 std=0 → t=nan，修复后会被 LOW_TSTAT 拒绝
        s = pl.DataFrame({"ic": [0.05, 0.06] * 15, "rank_ic": [0.04, 0.05] * 15})
        return {"train": s, "val": s}

    monkeypatch.setattr(sub, "_panel_with_covs", lambda start=None, covs=None: (_panel(), []))
    monkeypatch.setattr(sub, "_split_eval", fake_split)
    ok, payload = sub.verify_and_register({
        "name": "t_fine", "expr": "Ts_Mean($close,5)", "agent": "tester",
        "rationale": "短期反转效应，train 段 IC 稳定",
        "claimed": {"ic_mean": 0.051},
    })
    assert ok and payload["grade"] == "A"
    ok2, p2 = sub.verify_and_register({
        "name": "t_flip", "expr": "Ts_Mean($close,5)",
        "rationale": "方向对拍用例",
        "claimed": {"ic_mean": -0.05},
    })
    assert not ok2 and p2["grade"] == "D"


def test_verify_rejects_missing_rationale():
    """铁律：无 rationale 的 spec 不得入库 —— 缺失/空串都拒。"""
    import lquant.factors.mining.submit as sub

    for spec in ({"name": "t_nr", "expr": "Ts_Mean($close,5)"},
                 {"name": "t_nr2", "expr": "Ts_Mean($close,5)", "rationale": "   "}):
        ok, payload = sub.verify_and_register(spec)
        assert not ok and payload["reason_code"] == "MISSING_RATIONALE"
        assert payload["stage"] == "G0"


def test_verify_rejects_nan_tstat(monkeypatch):
    """val 段 IC 无法计算 t（序列含 nan → mean/std 均 nan）时不能放行：
    修复前 abs(nan) < thr 恒为 False，claimed 缺省时会把没有样本外
    证据的因子放进库 —— 必须拒绝并归档。"""
    import polars as pl

    import lquant.factors.mining.submit as sub

    def fake_split(df, covs, expr):
        s = pl.DataFrame({"ic": [float("nan")] * 30, "rank_ic": [0.04] * 30})
        return {"train": s, "val": s}

    monkeypatch.setattr(sub, "_panel_with_covs", lambda start=None, covs=None: (_panel(), []))
    monkeypatch.setattr(sub, "_split_eval", fake_split)
    archived = []

    monkeypatch.setattr(sub, "_archive", lambda spec, payload: archived.append(payload))
    ok, payload = sub.verify_and_register({
        "name": "t_nan", "expr": "Ts_Mean($close,5)", "agent": "tester",
        "rationale": "短期反转效应",
    })
    assert not ok and payload["reason_code"] == "LOW_TSTAT"
    assert "nan" in (payload.get("hint") or "") or "无法计算" in (payload.get("hint") or "")
    assert archived, "拒绝件必须归档"


def test_read_industry_retries_lock_then_raises(monkeypatch):
    """DuckDB 文件锁冲突（并行 CLI / 常驻 uvicorn）→ 重试后必须报错，
    绝不能静默返回 None 让行业协变量随机消失（口径分叉）。"""
    import contextlib

    import duckdb

    import lquant.core.db as dbmod
    import lquant.factors.mining.submit as sub

    calls = {"n": 0}

    @contextlib.contextmanager
    def locked_reader():
        calls["n"] += 1
        raise duckdb.IOException("Conflicting lock is held")
        yield  # pragma: no cover

    monkeypatch.setattr(dbmod, "reader", locked_reader)
    monkeypatch.setattr(sub.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="DuckDB"):
        sub._read_industry_df()
    assert calls["n"] == 3


def test_read_industry_recovers_after_transient_lock(monkeypatch):
    """瞬时锁冲突：前两次失败、第三次成功 → 重试后拿到数据。"""
    import contextlib

    import duckdb

    import lquant.core.db as dbmod
    import lquant.factors.mining.submit as sub

    calls = {"n": 0}
    expected = pl.DataFrame({
        "symbol": ["S1"], "std": ["SW"], "code": ["I1"],
        "std_date": [dt.date(2024, 12, 1)]})

    @contextlib.contextmanager
    def flaky_reader():
        calls["n"] += 1
        if calls["n"] < 3:
            raise duckdb.IOException("Conflicting lock is held")

        class FakeResult:
            def pl(self):
                return expected

        class FakeCon:
            def execute(self, q):
                return FakeResult()

        yield FakeCon()

    monkeypatch.setattr(dbmod, "reader", flaky_reader)
    monkeypatch.setattr(sub.time, "sleep", lambda s: None)
    out = sub._read_industry_df()
    assert len(out) == 1 and calls["n"] == 3


def test_panel_with_covs_missing_industry_table(monkeypatch):
    """新环境未建 industry_classify 表（CatalogException）→ 按「无行业数据」
    处理，coverage=0 显式体现在协变量列表里，不抛错。"""
    import contextlib

    import duckdb

    import lquant.core.db as dbmod
    import lquant.data.store.parquet as pq
    import lquant.factors.mining.submit as sub

    @contextlib.contextmanager
    def cat_reader():
        raise duckdb.CatalogException("Table with name industry_classify does not exist")
        yield  # pragma: no cover

    # market_cap 依赖 float_mv、turnover_1m 依赖 turnover_rate —— 面板带全
    panel = _panel().with_columns((pl.col("close") * 1e7).alias("float_mv"))
    monkeypatch.setattr(dbmod, "reader", cat_reader)
    monkeypatch.setattr(pq, "read_daily", lambda start=None: panel.lazy())
    df, covs = sub._panel_with_covs()
    assert "cov_industry_sw1" not in covs
    assert "cov_market_cap" in covs and "cov_turnover_1m" in covs


# ---------------- G0 永远第一步 ----------------
#
# 为拿实际挂载的 cov_cols 拼 G0 白名单，_panel_with_covs 一度被提到 g0_static
# 之前：坏表达式也全湖扫描，读湖失败（锁冲突/湖缺失）时真实原因被 COMPUTE_FAIL
# 掩盖。白名单只需 spec.covs 的声明即可判定字段合法性，不必真实挂载。


def test_verify_runs_g0_before_reading_panel(monkeypatch):
    """G0 未通过就绝不能先读面板：否则 STATIC_FAIL 被 COMPUTE_FAIL 掩盖。"""
    import lquant.factors.mining.submit as sub

    calls = []

    def boom(start=None, covs=None):
        calls.append((start, covs))
        raise RuntimeError("lake read boom")

    monkeypatch.setattr(sub, "_panel_with_covs", boom)
    ok, payload = sub.verify_and_register(
        {"name": "bad_field", "expr": "Ts_Mean($closs,5)", "rationale": "坏字段"}
    )
    assert not ok
    assert calls == [], "G0 未通过却先读了面板"
    assert payload["stage"] == "G0"
    assert payload["reason_code"] == "STATIC_FAIL"
    assert "closs" in payload["hint"] and "close" in payload["hint"]


def test_verify_declared_cov_g0_passes_then_fails_loudly_on_panel(monkeypatch):
    """G0 先行不等于放行：声明的 covariate 挂不上时仍在 RECOMPUTE 显式失败。"""
    import lquant.factors.mining.submit as sub

    def boom(start=None, covs=None):
        raise RuntimeError("money_flow 缺列 ['main_net_ratio']")

    monkeypatch.setattr(sub, "_panel_with_covs", boom)
    ok, payload = sub.verify_and_register(
        {
            "expr": "Ts_Mean(cov_mf_main_ratio,5)",
            "rationale": "看板资金流因子",
            "covs": ["mf_main_ratio"],
        }
    )
    assert not ok
    assert payload["stage"] == "RECOMPUTE"
    assert payload["reason_code"] == "COMPUTE_FAIL"
    assert "main_net_ratio" in payload["hint"]


# ---------------- spec.covs 落库：submit 与复算同一口径 ----------------


def test_verify_persists_declared_covs(submit_env, monkeypatch):
    """spec.covs 必须落库：否则 audit/run 复算改用 DEFAULT_COVS，IC 口径分叉。"""
    import json

    import lquant.factors.mining.submit as sub
    from lquant.core.db import reader

    def fake_split(df, covs, expr):
        s = pl.DataFrame({"ic": [0.05, 0.06] * 15, "rank_ic": [0.04, 0.05] * 15})
        return {"train": s, "val": s}

    monkeypatch.setattr(sub, "_panel_with_covs", lambda start=None, covs=None: (_panel(), []))
    monkeypatch.setattr(sub, "_split_eval", fake_split)
    ok, payload = sub.verify_and_register(
        {
            "name": "cov_persist",
            "expr": "Ts_Mean($close,5)",
            "rationale": "口径落库验证",
            "covs": ["lhb_on_board", "mf_main_ratio"],
        }
    )
    assert ok, payload
    with reader() as con:
        got = con.execute(
            "SELECT covs FROM factor_def WHERE name = 'cov_persist'"
        ).fetchone()[0]
    assert json.loads(got) == ["lhb_on_board", "mf_main_ratio"]

    from lquant.cli.commands.factor import _registered_covs

    assert _registered_covs("Ts_Mean($close,5)") == ["lhb_on_board", "mf_main_ratio"]


def test_verify_persists_null_covs_for_default_universe(submit_env, monkeypatch):
    """未声明 covs → 落 NULL，复算读回 None（走 DEFAULT_COVS，向后兼容）。"""
    import lquant.factors.mining.submit as sub
    from lquant.core.db import reader

    def fake_split(df, covs, expr):
        s = pl.DataFrame({"ic": [0.05, 0.06] * 15, "rank_ic": [0.04, 0.05] * 15})
        return {"train": s, "val": s}

    monkeypatch.setattr(sub, "_panel_with_covs", lambda start=None, covs=None: (_panel(), []))
    monkeypatch.setattr(sub, "_split_eval", fake_split)
    ok, _ = sub.verify_and_register(
        {"name": "cov_null", "expr": "Ts_Mean($close,5)", "rationale": "缺省口径"}
    )
    assert ok
    with reader() as con:
        got = con.execute("SELECT covs FROM factor_def WHERE name = 'cov_null'").fetchone()[0]
    assert got is None

    from lquant.cli.commands.factor import _registered_covs

    assert _registered_covs("Ts_Mean($close,5)") is None


def test_factor_def_covs_migration_adds_covs_idempotently(submit_env):
    """老库增列迁移：covs 补列幂等（复算读回注册口径的前提）。

    covs 单独迁移、不改变 ``ensure_factor_def_columns`` 对四个结构列的计数契约，
    但挂在同一条启动 ensure 链路上。
    """
    import duckdb

    from lquant.core.config import get_settings
    from lquant.data.store.ddl import ensure_factor_def_columns, ensure_factor_def_covs

    con = duckdb.connect(str(get_settings().duckdb_path))
    con.execute("ALTER TABLE factor_def DROP COLUMN covs")
    assert ensure_factor_def_covs(con) == 1
    assert ensure_factor_def_covs(con) == 0
    # 四条基础列都在 → 计数仍为 0，但 covs 会被同链路补回
    con.execute("ALTER TABLE factor_def DROP COLUMN covs")
    assert ensure_factor_def_columns(con) == 0
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    con.close()
    assert "covs" in cols


def test_load_segments_uses_registered_covs(submit_env, monkeypatch):
    """audit/report/robust 的复算走 factor_def.covs，不再偷偷换成 DEFAULT_COVS。"""
    import lquant.factors.mining.submit as sub
    from lquant.cli.commands.factor import _load_segments
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT INTO factor_def (name, expression, covs, created_at) "
            "VALUES ('segged', 'Ts_Mean($close,5)', '[\"mf_main_ratio\"]', now())"
        )
    seen = {}

    def fake_panel(start=None, covs=None):
        seen["covs"] = covs
        dates = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(10)]
        return (
            pl.DataFrame({"trade_date": dates * 2, "symbol": ["S0"] * 10 + ["S1"] * 10}),
            ["cov_mf_main_ratio"],
        )

    monkeypatch.setattr(sub, "_panel_with_covs", fake_panel)
    monkeypatch.setattr(sub, "prepare_segment", lambda df, covs, expr, dates, **kw: df)
    seg, cov_cols, days = _load_segments(None, "Ts_Mean($close,5)")
    assert seen["covs"] == ["mf_main_ratio"]
    assert cov_cols == ["cov_mf_main_ratio"]
    assert days["train_days"] > 0
