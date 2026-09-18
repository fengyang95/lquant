"""最后冲刺第三波：run_crosscheck 主流程分支（peer 无数据/出 issue/回写失败兜底）。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest


def _primary() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"] * 2,
        "trade_date": [date(2024, 1, 5), date(2024, 1, 8)],
        "close": [10.0, 10.5],
        "pre_close": [9.9, 10.0],
        "volume": [1e6, 1.1e6],
        "amount": [1e7, 1.1e7],
    })


@pytest.fixture
def cc_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_cc(monkeypatch, cc, *, peer_df, diffs, primary=_primary):
    monkeypatch.setattr(cc, "_cfg", lambda: {
        "enabled": True, "peers": ["sina"], "tolerance_pct": 0.01,
        "fields": ["close", "pre_close", "volume", "amount"]})
    monkeypatch.setattr(cc, "_sample_symbols",
                        lambda limit: (["600000.SH"],
                                       date(2024, 1, 5), date(2024, 1, 8)))
    monkeypatch.setattr(cc, "_primary_daily", lambda *a, **kw: primary())
    monkeypatch.setattr(cc, "_peer_daily",
                        lambda name, syms, s, e: peer_df)


def test_run_crosscheck_peer_empty_skipped(cc_env, monkeypatch):
    from lquant.data.ingest import crosscheck as cc

    _patch_cc(monkeypatch, cc, peer_df=pl.DataFrame(), diffs=None)
    out = cc.run_crosscheck()
    assert out["summary"]["L0"] == 1  # peer 无数据 → L0
    assert out["flagged_rows"] == 0


def test_run_crosscheck_writes_issues(cc_env, monkeypatch):
    from lquant.data.ingest import crosscheck as cc

    peer = _primary().with_columns(pl.col("close") * 2)
    diffs = pl.DataFrame({
        "symbol": ["600000.SH"],
        "trade_date": [date(2024, 1, 5)],
        "field": ["close"],
        "primary": [10.0],
        "peer": [20.0],
        "rel_diff": [1.0],
        "level": ["L3"],
        "missing": [False],
    })
    _patch_cc(monkeypatch, cc, peer_df=peer, diffs=diffs)
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.classify_divergence",
        lambda *a, **kw: diffs)
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.summarize",
        lambda d: {"checked": 2, "L1": 0, "L2": 0, "L3": 1})
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.flag_cross_source",
        lambda p, d: p.with_columns(
            pl.lit(0b1000, dtype=pl.Int32).alias("quality_flags")))
    saved = []
    monkeypatch.setattr(cc, "save_issues", lambda issues: saved.extend(issues))
    out = cc.run_crosscheck()
    assert out["summary"]["L3"] == 1
    assert len(saved) == 1
    assert saved[0].rule.startswith("CROSS_SRC_DIFF.sina.")
    assert out["flagged_rows"] == 2  # primary 两行都被打 0b1000 标记


def test_run_crosscheck_write_failure_swallowed(cc_env, monkeypatch):
    from lquant.data.ingest import crosscheck as cc

    peer = _primary().with_columns(pl.col("close") * 2)
    diffs = pl.DataFrame({
        "symbol": ["600000.SH"],
        "trade_date": [date(2024, 1, 5)],
        "field": ["close"],
        "primary": [10.0],
        "peer": [20.0],
        "rel_diff": [1.0],
        "level": ["L3"],
        "missing": [False],
    })
    _patch_cc(monkeypatch, cc, peer_df=peer, diffs=diffs)
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.classify_divergence",
        lambda *a, **kw: diffs)
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.summarize",
        lambda d: {"checked": 2, "L1": 0, "L2": 0, "L3": 1})
    monkeypatch.setattr(
        "lquant.data.quality.crosscheck.flag_cross_source",
        lambda p, d: p.with_columns(
            pl.lit(0b1000, dtype=pl.Int32).alias("quality_flags")))

    def boom(_df):
        raise RuntimeError("lake down")

    monkeypatch.setattr("lquant.data.store.parquet.write_daily", boom)
    saved = []
    monkeypatch.setattr(cc, "save_issues", lambda issues: saved.extend(issues))
    out = cc.run_crosscheck()  # 回写失败只 log，不抛
    assert out["summary"]["L3"] == 1
    assert len(saved) == 1


def test_run_crosscheck_disabled_and_no_peers(cc_env, monkeypatch):
    from lquant.data.ingest import crosscheck as cc

    monkeypatch.setattr(cc, "_cfg", lambda: {
        "enabled": False, "peers": ["sina"], "tolerance_pct": 0.01,
        "fields": ["close"]})
    out = cc.run_crosscheck()
    assert out["summary"]["L0"] == 1
    monkeypatch.setattr(cc, "_cfg", lambda: {
        "enabled": True, "peers": [], "tolerance_pct": 0.01,
        "fields": ["close"]})
    out2 = cc.run_crosscheck()
    assert out2["summary"]["L0"] == 1


def test_run_crosscheck_no_symbols(cc_env, monkeypatch):
    from lquant.data.ingest import crosscheck as cc

    monkeypatch.setattr(cc, "_cfg", lambda: {
        "enabled": True, "peers": ["sina"], "tolerance_pct": 0.01,
        "fields": ["close"]})
    monkeypatch.setattr(cc, "_sample_symbols", lambda limit: ([], None, None))
    out = cc.run_crosscheck()
    assert out["summary"]["L0"] == 1
