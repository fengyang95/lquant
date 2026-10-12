"""ETF 数据源的三级降级：表缺失 / 未同步 / 湖为空都不能抛，且必须显式可辨。

`etf_meta` → `security.sec_type` → 日线湖代码段，每一级都可能「读不到」：
真实环境里常见的是「没跑 init_db」「没同步 etf_meta」「湖是空的」。这些分支
如果抛异常，ETF 页面/CLI 会在缺数据的机器上直接 500；如果静默返回「全市场」，
就会把个股混进 ETF 口径。这里把两种失败方式都钉住。

全部打桩，不碰 DuckDB / 日线湖。
"""
from __future__ import annotations

import polars as pl
import pytest

# ---------------- etf_meta 读不到 → 空表（带 schema） ----------------

def test_read_etf_meta_degrades_to_empty_frame(monkeypatch):
    class _Boom:
        def all(self):
            raise RuntimeError("no such table: etf_meta")

    monkeypatch.setattr("lquant.data.store.catalog.EtfMetaRepo", _Boom)
    from lquant.data.schema import ETF_META
    from lquant.factors.etf import _read_etf_meta

    df = _read_etf_meta()
    assert isinstance(df, pl.DataFrame)
    assert len(df) == 0
    assert set(df.columns) == set(ETF_META)      # 列集合稳定，调用方不必判 None


def test_read_etf_meta_passes_through_repo_frame(monkeypatch):
    frame = pl.DataFrame({"symbol": ["510300.SH"], "fund_type": ["equity"]})

    class _Repo:
        def all(self):
            return frame

    monkeypatch.setattr("lquant.data.store.catalog.EtfMetaRepo", _Repo)
    from lquant.factors.etf import _read_etf_meta

    assert _read_etf_meta().equals(frame)


# ---------------- 第二级：security 表 ----------------

def test_read_security_etf_symbols_from_repo(monkeypatch):
    class _Repo:
        def etf_symbols(self):
            return ["510300.SH", "159915.SZ"]

    monkeypatch.setattr("lquant.data.store.catalog.SecurityRepo", _Repo)
    from lquant.factors.etf import _read_security_etf_symbols

    assert _read_security_etf_symbols() == ["510300.SH", "159915.SZ"]


def test_read_security_etf_symbols_degrades_on_error(monkeypatch):
    class _Boom:
        def etf_symbols(self):
            raise RuntimeError("security 表未建")

    monkeypatch.setattr("lquant.data.store.catalog.SecurityRepo", _Boom)
    from lquant.factors.etf import _read_security_etf_symbols

    assert _read_security_etf_symbols() == []


# ---------------- 第三级：日线湖代码段 ----------------

def test_read_daily_etf_symbols_filters_stocks(monkeypatch):
    """湖里个股与 ETF 混在一起 —— 兜底也必须只留 ETF/LOF。"""
    frame = pl.DataFrame({"symbol": ["510300.SH", "600000.SH", "159915.SZ", "000001.SZ"]})
    monkeypatch.setattr("lquant.data.store.parquet.read_daily", lambda **kw: frame.lazy())
    from lquant.factors.etf import _read_daily_etf_symbols

    # unique() 不保证行序 → 按集合断言，只钉「个股被剔掉」这条不变量
    assert set(_read_daily_etf_symbols()) == {"510300.SH", "159915.SZ"}


def test_read_daily_etf_symbols_degrades_when_lake_missing(monkeypatch):
    def boom(**_kw):
        raise FileNotFoundError("data/parquet/daily 不存在")

    monkeypatch.setattr("lquant.data.store.parquet.read_daily", boom)
    from lquant.factors.etf import _read_daily_etf_symbols

    assert _read_daily_etf_symbols() == []


# ---------------- 池解析在三级全空时明确拒答 ----------------

def test_resolve_etf_universe_raises_when_all_sources_empty():
    from lquant.factors.etf import resolve_etf_universe

    with pytest.raises(ValueError, match="ETF 池为空"):
        resolve_etf_universe("etf_all", etf_meta=pl.DataFrame(), security_symbols=[],
                             daily_symbols=[])


def test_resolve_etf_universe_daily_fallback_discloses(monkeypatch):
    from lquant.factors.etf import resolve_etf_universe

    symbols, note = resolve_etf_universe(
        "etf_all", etf_meta=pl.DataFrame(), security_symbols=[],
        daily_symbols=["510300.SH", "600000.SH"])
    assert symbols == ["510300.SH"]              # 个股不进 ETF 池
    assert note and "日线湖" in note


# ---------------- load_etf_meta_map：元数据空 → {}（回测照跑） ----------------

@pytest.mark.parametrize("frame", [
    None,
    pl.DataFrame(),
    pl.DataFrame({"fund_type": ["equity"]}),     # 缺 symbol 列
])
def test_load_etf_meta_map_empty_shapes(monkeypatch, frame):
    class _Repo:
        def all(self):
            return frame

    monkeypatch.setattr("lquant.data.store.catalog.EtfMetaRepo", _Repo)
    from lquant.backtest.etf import load_etf_meta_map

    assert load_etf_meta_map() == {}
