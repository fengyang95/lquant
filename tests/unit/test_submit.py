"""M4c submit 重验回归。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest


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
