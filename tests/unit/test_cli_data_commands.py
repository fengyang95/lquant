"""`lq data` CLI 子命令覆盖补齐：下层采集函数一律打桩，只测 CLI 参数流与输出。"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import polars as pl
import pytest
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["data", *args])


def _patch_mod(monkeypatch, dotted, name, fn):
    import importlib

    mod = importlib.import_module(dotted)
    monkeypatch.setattr(mod, name, fn)


def test_reference_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.reference", "sync_reference",
               lambda **kw: {"calendar": 1, "security": 2} | {"skip_details": kw["skip_details"]})
    r = _invoke("reference", "--skip-details", "--detail-limit", "5")
    assert r.exit_code == 0, r.output
    assert "reference done" in r.output


def test_sync_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.daily", "backfill_daily",
               lambda **kw: 42)
    r = _invoke("sync", "--start", "2020-01-01", "--end", "2020-12-31")
    assert r.exit_code == 0, r.output
    assert "done 42" in r.output


def test_etf_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.etf_meta", "sync_etf_meta",
               lambda syms=None: len(syms or []))
    _patch_mod(monkeypatch, "lquant.data.ingest.etf_meta", "enrich_from_akshare",
               lambda: 7)
    r = _invoke("etf", "--symbols", "510300.SH,159915.SZ", "--enrich")
    assert r.exit_code == 0, r.output
    assert "etf_meta done 2" in r.output
    assert "enrich done 7" in r.output


def test_minute_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.minute", "backfill_minute",
               lambda syms, **kw: len(syms))
    r = _invoke("minute", "--symbols", "510300.SH,159915.SZ", "--freq", "15min")
    assert r.exit_code == 0, r.output
    assert "done 2" in r.output


def test_financial_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.financial", "backfill_financial",
               lambda syms, **kw: len(syms))
    r = _invoke("financial", "--symbols", "600000.SH,000001.SZ", "--provider", "baostock")
    assert r.exit_code == 0, r.output
    assert "done 2" in r.output


def test_financial_all_and_missing_args(monkeypatch):
    import lquant.data.store.catalog as catalog_mod

    _patch_mod(monkeypatch, "lquant.data.ingest.financial", "backfill_financial",
               lambda syms, **kw: len(syms))
    monkeypatch.setattr(catalog_mod.SecurityRepo, "stock_symbols",
                        lambda self: ["600000.SH", "000001.SZ", "600519.SH"])
    r = _invoke("financial", "--all")
    assert r.exit_code == 0, r.output
    assert "done 3" in r.output

    r2 = _invoke("financial")
    assert r2.exit_code != 0
    assert "--symbols 与 --all 必须给一个" in r2.output


def test_basic_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.daily_basic", "backfill_daily_basic",
               lambda **kw: {"rows": 10, "merged": 3})
    r = _invoke("basic", "--no-merge")
    assert r.exit_code == 0, r.output
    assert "basic done" in r.output


def test_index_cons_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.index_cons", "sync_index_cons",
               lambda codes=None: {"codes": len(codes or [])})
    r = _invoke("index-cons", "--indexes", "000300.SH,000905.SH")
    assert r.exit_code == 0, r.output
    assert "index-cons done" in r.output


class _RaisingReader:
    def __enter__(self):
        raise RuntimeError("db down")

    def __exit__(self, *a):
        return False


class _OkReader:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        con = SimpleNamespace(execute=lambda sql: SimpleNamespace(
            fetchone=lambda: (7, "2024-01-02", "2026-06-30")))
        return con

    def __exit__(self, *a):
        return False


def test_status_cmd_error_branch(monkeypatch):
    import lquant.core.db as db_mod

    monkeypatch.setattr(db_mod, "reader", lambda: _RaisingReader())
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    assert "RuntimeError" in r.output
    assert "daily parquet 年分区:" in r.output


def test_status_cmd_ok(monkeypatch):
    """reader 正常返回 → 打印各表行数（成功分支）。"""
    import lquant.core.db as db_mod

    class _Ok:
        def __enter__(self):
            return SimpleNamespace(execute=lambda sql: SimpleNamespace(
                fetchone=lambda: (7,)))

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db_mod, "reader", lambda: _Ok())
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    assert "security: 7" in r.output
    assert "financial_pit: 7" in r.output


def test_demo_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.demo", "generate_demo",
               lambda **kw: {"symbols": 3, "days": 5})
    r = _invoke("demo", "--start", "2025-01-01")
    assert r.exit_code == 0, r.output
    assert "demo data done" in r.output


def _issue(sev, rule):
    return SimpleNamespace(severity=sev, rule=rule, detail="d")


def test_check_pass(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [])
    r = _invoke("check")
    assert r.exit_code == 0, r.output
    assert "quality: PASS" in r.output


def test_check_warn_only(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [_issue("warn", "R1"), _issue("warn", "R2")])
    r = _invoke("check")
    assert r.exit_code == 0, r.output
    assert "2 条 issue" in r.output


def test_check_fatal_exits_2(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.quality.pipeline", "run_lake_checks",
               lambda **kw: [_issue("fatal", "DUP_KEY"), _issue("error", "R9")])
    r = _invoke("check")
    assert r.exit_code == 2
    assert "DUP_KEY" in r.output


def test_crosscheck_cmd(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.ingest.crosscheck", "run_crosscheck",
               lambda **kw: {"summary": {"L1": 1, "L2": 0, "L3": 0},
                             "issues": [_issue("error", "X_L1")],
                             "flagged_rows": 12})
    r = _invoke("crosscheck", "--peers", "a,b", "--limit", "50")
    assert r.exit_code == 0, r.output
    assert "crosscheck:" in r.output
    assert "[L1] 1 条 issue" in r.output
    assert "12 行" in r.output


def test_fields_cmd(monkeypatch):
    df = pl.DataFrame({"close": [1.0, None, 3.0], "volume": [1, 2, 3]})
    _patch_mod(monkeypatch, "lquant.data.store.parquet", "read_daily",
               lambda start=None: df.lazy())
    r = _invoke("fields")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    cov = {d["field"]: d["coverage"] for d in body}
    assert cov["close"] == 0.6667 and cov["volume"] == 1.0


def test_fields_empty_raises(monkeypatch):
    _patch_mod(monkeypatch, "lquant.data.store.parquet", "read_daily",
               lambda start=None: pl.DataFrame().lazy())
    r = _invoke("fields")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output
