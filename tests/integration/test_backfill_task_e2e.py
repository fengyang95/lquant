"""全链路 e2e：fake demo provider → 真实 backfill_pool / 质量门禁 / write_daily /
duckdb data_task → 湖内日线（含 9 新列）→ retry 只补漏 → auto_crosscheck 钩子。

隔离方式沿用 T3 报告结论：LQ_ROOT env + chdir + cache_clear，
不 patch get_settings 模块属性（会污染懒加载绑定）。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from lquant.data.ingest import tasks as tasks_mod
from lquant.data.store.parquet import read_daily

NEW_COLS = [
    "pct_chg", "is_st", "is_suspended", "pe_ttm", "pb_mrq", "ps_ttm",
    "pcf_ncf_ttm", "total_mv", "float_mv",
]


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离 + 拷入真实 providers.yaml
    （crosscheck _cfg 的 yaml 回退要读它）。"""
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    (tmp_path / "config").mkdir()
    shutil.copy2(root / "config" / "providers.yaml",
                 tmp_path / "config" / "providers.yaml")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def seeded_db(fake_settings):
    """临时 DuckDB：1 只在市股 + 1 只退市股 + 1 只 ETF。

    full_backfill 校验退市股存在；在市股只有一只 → 同批不同 end 自然分组
    （000001.SZ end=窗口末，000003.SZ end=退市日），provider 可只对
    000001.SZ 脚本化失败，实现单标的粒度的 partial。
    """
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security ("
            "symbol VARCHAR, sec_type VARCHAR, list_date DATE, delist_date DATE)"
        )
        con.execute(
            "INSERT INTO security VALUES "
            "('000001.SZ','stock',DATE '2000-01-01',NULL),"
            "('000003.SZ','stock',DATE '2000-01-01',DATE '2020-06-30'),"
            "('510300.SH','etf',DATE '2012-01-01',NULL)"
        )
    yield


class DemoProvider:
    """合成日线 provider（demo.py 同款 GBM 思路，简化）：每次调用都成功。

    fail_symbols：这些标的 daily_bars 抛 RuntimeError（脚本化首次失败，
    retry 后换 fail_once=False 即成功）。
    """

    def __init__(self, fail_symbols: set[str] | None = None) -> None:
        self.fail_symbols = set(fail_symbols or [])
        self.calls: list[tuple[list[str], date, date | None]] = []

    def daily_bars(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        bad = [s for s in symbols if s in self.fail_symbols]
        good = [s for s in symbols if s not in self.fail_symbols]
        frames = []
        if bad:
            raise RuntimeError(f"源站拒绝: {bad}")
        for i, sym in enumerate(good):
            frames.append(self._bars(sym, start, end, seed=100 + i))
        return pl.concat(frames) if frames else pl.DataFrame()

    @staticmethod
    def _bars(sym: str, start: date, end: date, seed: int) -> pl.DataFrame:
        import pandas as pd

        days = pd.bdate_range(start, end).date
        n = len(days)
        if not n:
            return pl.DataFrame()
        rng = np.random.default_rng(seed)
        close = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n))) + 1.0
        open_ = close * 0.99
        high = np.maximum(open_, close) * 1.01
        low = np.minimum(open_, close) * 0.99
        volume = rng.uniform(1e6, 5e6, n)
        return pl.DataFrame({
            "trade_date": pl.Series(list(days), dtype=pl.Date),
            "symbol": [sym] * n,
            "open": open_.round(2), "high": high.round(2),
            "low": low.round(2), "close": close.round(2),
            "pre_close": np.concatenate([[close[0]], close[:-1]]).round(2),
            "volume": volume.round(0),
            "amount": (volume * close).round(0),
            "turnover_rate": rng.uniform(0.2, 5, n).round(2),
            "adj_factor": np.ones(n),
            "sec_type": ["stock"] * n,
            **{c: [None] * n for c in NEW_COLS},
        }).with_columns(pl.col("pe_ttm").cast(pl.Float64),
                        pl.col("is_st").cast(pl.Boolean))


def _task_range() -> dict:
    return {"start": "2024-01-01", "end": "2024-03-31"}


# ---------------------------------------------------------------- e2e


def test_full_backfill_e2e(seeded_db, monkeypatch):
    """create → execute → ok；湖里出现含 9 新列的日线；coverage 统计容忍新列。"""
    from lquant.server.api import data as data_api

    prov = DemoProvider()
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: prov)
    calls = {"n": 0, "kwargs": {}}

    def fake_cc(*a, **kw):
        calls["n"] += 1
        calls["kwargs"] = kw
        return {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0}

    monkeypatch.setattr(
        "lquant.data.ingest.crosscheck.run_crosscheck", fake_cc)

    t = tasks_mod.create_task("full_backfill", _task_range())
    assert t["status"] == "pending"
    assert t["total_symbols"] == 3  # 2 股（1 退市）+ 1 ETF
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "ok", out
    assert out["done_symbols"] == 3
    assert out["rows_written"] > 0

    # 湖里出现日线，含 9 新列
    lake = read_daily().collect()
    assert len(lake) > 0
    for c in NEW_COLS:
        assert c in lake.columns, f"缺新列 {c}"

    # coverage 湖统计：只取 symbol/trade_date 聚合，新列空值不炸
    cov = data_api.coverage()
    assert cov["daily_lake"]["rows"] == len(lake)
    assert cov["daily_lake"]["symbols"] == lake["symbol"].n_unique()

    # auto_crosscheck 默认触发，start/end 照实传入，summary 落 message
    assert calls["n"] == 1
    assert calls["kwargs"].get("start") == "2024-01-01"
    assert calls["kwargs"].get("end") == "2024-03-31"
    assert "crosscheck" in (out["message"] or "")
    assert '"L0"' in (out["message"] or "")


def test_auto_crosscheck_disabled(seeded_db, monkeypatch):
    """params.auto_crosscheck=False → 不触发对拍。"""
    prov = DemoProvider()
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: prov)
    called = []
    monkeypatch.setattr(
        "lquant.data.ingest.crosscheck.run_crosscheck",
        lambda *a, **kw: called.append(kw) or {"summary": {"L0": 1}},
    )
    t = tasks_mod.create_task("full_backfill", {**_task_range(),
                                                "auto_crosscheck": False})
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "ok"
    assert called == []


def test_auto_crosscheck_failure_does_not_break_task(seeded_db, monkeypatch):
    """对拍抛异常 → 只 log，任务终态不变。"""
    prov = DemoProvider()
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: prov)

    def boom(*a, **kw):
        raise RuntimeError("peer down")

    monkeypatch.setattr(
        "lquant.data.ingest.crosscheck.run_crosscheck", boom)
    t = tasks_mod.create_task("full_backfill", _task_range())
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "ok"


def test_retry_e2e_only_refills_failed(seeded_db, monkeypatch):
    """首跑 1 只失败 → partial；retry 后该只入库 → ok，其余不重拉。"""
    prov = DemoProvider(fail_symbols={"000001.SZ"})
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: prov)
    monkeypatch.setattr(
        "lquant.data.ingest.crosscheck.run_crosscheck",
        lambda *a, **kw: {"summary": {"L0": 1}, "issues": [], "flagged_rows": 0},
    )
    t = tasks_mod.create_task("full_backfill", _task_range())
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "partial"
    assert out["failed_symbols"] == ["000001.SZ"]

    lake = read_daily().collect()
    assert "000001.SZ" not in set(lake["symbol"])

    # retry：失败标的恢复成功
    prov.fail_symbols = set()
    out2 = tasks_mod.retry_task(t["task_id"])
    assert out2["status"] == "ok"
    assert out2["done_symbols"] == 3

    lake2 = read_daily().collect()
    assert "000001.SZ" in set(lake2["symbol"])
    # 其余标的没有重复行（upsert 语义，不因 retry 翻倍）
    n_dup = len(lake2) - len(lake2.unique(subset=["symbol", "trade_date"]))
    assert n_dup == 0
