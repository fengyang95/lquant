"""Task 2：baostock 日线扩列（估值/状态/市值）+ 停牌保留。

全部喂模拟原始行（17 列，与 _DAILY_FIELDS 顺序一致），不触网。
_map_daily_raw 产出**源列名** df（frames→DataFrame→cast 段），
schema 对齐（date→trade_date 等）仍由 mapping 引擎完成 ——
本文件的断言一律走 provider 全链路（request(_raw=...)）。
"""
from __future__ import annotations

import pickle

import polars as pl

from lquant.data.providers.baostock import (
    BaoStockProvider,
    _attach_is_st,
    _map_daily_raw,
)
from lquant.data.schema import SCHEMAS

# date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,isST,
# pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM
RAW = [
    ["2024-01-02", "sh.600519", "1700.0", "1720.0", "1690.0", "1710.0", "1705.0",
     "1000000", "1.71e9", "0.8", "1", "0", "0.29", "25.5", "8.2", "6.1", "18.0"],
    ["2024-01-03", "sh.600519", "1710.0", "1725.0", "1700.0", "1715.0",
     "0", "0", "0", "0", "0", "0", "", "", "", "", ""],  # 停牌行，必须保留
]


def _mapped() -> pl.DataFrame:
    """喂 raw 走完整映射管线（yaml rename + derive + coerce），engine 出 schema 列。"""
    p = BaoStockProvider()
    raw = _map_daily_raw(RAW)
    out = p.request("daily_bar", _raw=raw)
    return _attach_is_st(out, raw)


def test_map_daily_raw_keeps_source_names_and_suspended() -> None:
    """_map_daily_raw：源列名 + 停牌行保留 + turn=0/空串 → null。"""
    raw = _map_daily_raw(RAW)
    assert len(raw) == 2                     # 停牌行不再被过滤
    assert raw["is_suspended"].to_list() == [False, True]
    assert raw["turn"].to_list()[:1] == [0.8]
    assert raw["turn"][1] is None            # turn=0 → null（防 float_mv 产 inf）


def test_daily_map_full_columns() -> None:
    df = _mapped()
    assert df.columns == list(SCHEMAS["daily_bar"])
    r0 = df.filter(pl.col("trade_date") == pl.date(2024, 1, 2)).row(0, named=True)
    assert r0["pe_ttm"] == 25.5
    assert r0["pb_mrq"] == 8.2
    assert r0["ps_ttm"] == 6.1
    assert r0["pcf_ncf_ttm"] == 18.0
    assert r0["pct_chg"] == 0.29
    assert r0["is_st"] is False
    assert df["is_suspended"].to_list() == [False, True]
    exp = 1710.0 * 1_000_000 / (0.8 / 100)
    assert abs(r0["float_mv"] - exp) < 1.0
    assert r0["total_mv"] is None            # baostock 无股本列，置 null


def test_suspended_row_kept() -> None:
    df = _mapped()
    assert len(df) == 2
    assert df.filter(pl.col("is_suspended"))["close"][0] == 1715.0


def test_suspended_row_volume_zero_and_valuation_null() -> None:
    df = _mapped()
    susp = df.filter(pl.col("is_suspended")).row(0, named=True)
    assert susp["volume"] == 0.0
    assert susp["pe_ttm"] is None            # 空串 → null
    assert susp["float_mv"] is None          # turn null → derive 除法 → null
    assert susp["is_st"] is False


def test_map_daily_raw_is_picklable_module_function() -> None:
    """模块级函数：spawn 子进程 pickle 兼容（watchdog 约束）。"""
    fn = pickle.loads(pickle.dumps(_map_daily_raw))
    assert fn is _map_daily_raw
