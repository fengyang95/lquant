"""data/ingest/etf_meta 覆盖补齐：provider/repo/akshare 全打桩，离线验证分层回退。"""
from __future__ import annotations

import pandas as pd
import polars as pl
import pytest

import lquant.data.ingest.etf_meta as em


@pytest.fixture()
def fake_repo(monkeypatch):
    saved = {}

    class Repo:
        def upsert(self, df):
            saved["df"] = df
            return len(df)

    class SecRepo:
        def etf_symbols(self):
            return ["510300.SH", "159915.SZ"]

    monkeypatch.setattr(em, "EtfMetaRepo", Repo)
    monkeypatch.setattr(em, "SecurityRepo", SecRepo)
    return saved


def test_sync_etf_meta_success(fake_repo, monkeypatch):
    class P:
        def etf_meta(self, symbols):
            assert symbols is None
            return pl.DataFrame({
                "symbol": ["510300.SH", "159915.SZ"],
                "sellable_after_days": [0, 1],
            })

    import lquant.data.providers as dp
    monkeypatch.setattr(dp, "get_provider", lambda: P())

    n = em.sync_etf_meta()
    assert n == 2
    assert fake_repo["df"]["sellable_after_days"].to_list() == [0, 1]


def test_sync_etf_meta_empty(fake_repo, monkeypatch):
    class P:
        def etf_meta(self, symbols):
            return pl.DataFrame()

    import lquant.data.providers as dp
    monkeypatch.setattr(dp, "get_provider", lambda: P())
    assert em.sync_etf_meta() == 0


def test_sync_etf_meta_no_sellable_col(fake_repo, monkeypatch):
    class P:
        def etf_meta(self, symbols):
            return pl.DataFrame({"symbol": ["510300.SH"]})

    import lquant.data.providers as dp
    monkeypatch.setattr(dp, "get_provider", lambda: P())
    n = em.sync_etf_meta(["510300.SH"])
    assert n == 1
    # 无 sellable_after_days 列时 T+0 计数退化为 0
    assert fake_repo["df"]["symbol"].to_list() == ["510300.SH"]


def _install_akshare(monkeypatch, pdf):
    import types

    ak = types.ModuleType("akshare")
    ak.fund_etf_spot_em = lambda: pdf
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)


def test_enrich_success(fake_repo, monkeypatch):
    pdf = pd.DataFrame({
        "代码": ["510300", "159915", "000000"],
        "流通市值": [2e8, 3e8, 9e8],   # 元 → 亿元
        "成交额": [1e6, 2e6, 9e6],
        "名称": ["沪深300ETF", "创业板ETF", "x"],
    })
    _install_akshare(monkeypatch, pdf)
    n = em.enrich_from_akshare()
    assert n == 2
    df = fake_repo["df"]
    assert df["symbol"].to_list() == ["510300.SH", "159915.SZ"]
    assert df["fund_size"].to_list() == [2.0, 3.0]
    assert df["source"].to_list() == ["akshare", "akshare"]


def test_enrich_akshare_missing(fake_repo, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "akshare", None)
    monkeypatch.delenv("LQ_ENRICH_FORCE", raising=False)
    assert em.enrich_from_akshare() == 0


def test_enrich_fetch_fail(fake_repo, monkeypatch):
    import types

    ak = types.ModuleType("akshare")
    def boom():
        raise RuntimeError("net down")

    ak.fund_etf_spot_em = boom
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_ok := ak)
    n = em.enrich_from_akshare()
    assert n == 0


def test_enrich_no_code_col(fake_repo, monkeypatch):
    _install_akshare(monkeypatch, pd.DataFrame({"XX": [1]}))
    assert em.enrich_from_akshare() == 0


def test_enrich_no_overlap(fake_repo, monkeypatch):
    _install_akshare(monkeypatch, pd.DataFrame({
        "代码": ["000000"], "流通市值": [1e8],
    }))
    assert em.enrich_from_akshare() == 0


def test_sync_etf_wrapper(fake_repo, monkeypatch):
    class P:
        def etf_meta(self, symbols):
            return pl.DataFrame({"symbol": ["510300.SH"]})

    import lquant.data.providers as dp
    monkeypatch.setattr(dp, "get_provider", lambda: P())
    assert em.sync_etf(enrich=False) == {"etf_meta": 1}

    _install_akshare(monkeypatch, pd.DataFrame({
        "代码": ["510300"], "流通市值": [2e8],
    }))
    out = em.sync_etf(enrich=True)
    assert out == {"etf_meta": 1, "enrich": 1}
