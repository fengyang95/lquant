"""market/collectors/money_flow 单元测试（打桩 em_get，不联网）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

import lquant.market.collectors.money_flow as mf
from lquant.core.errors import DataUnavailable
from lquant.market.collectors.money_flow import (
    fetch_money_flow,
    fetch_northbound,
)


class _Resp:
    def __init__(self, payload) -> None:
        self._p = payload

    def json(self) -> dict:
        return self._p


def test_num_and_as_date_and_norm():
    assert mf._num(None) == 0.0 and mf._num("-") == 0.0 and mf._num("") == 0.0
    assert mf._num("12.5") == 12.5 and mf._num(3) == 3.0
    assert mf._num(object()) == 0.0          # TypeError → 0.0

    assert mf._as_date(None).year >= 2026
    assert mf._as_date("2026-01-05") == date(2026, 1, 5)
    assert mf._as_date("20260105") == date(2026, 1, 5)
    assert mf._as_date(date(2025, 12, 31)) == date(2025, 12, 31)

    assert mf._norm("600000") == "600000.SH"
    assert mf._norm("600000.SH") == "600000.SH"
    assert mf._norm("") == ""                # 解析失败原样返回
    assert mf._norm("abc") == "abc"


def test_fetch_money_flow_demo_deterministic_sorted():
    d = date(2026, 1, 5)
    df = fetch_money_flow(d, top=10, demo=True)
    assert df.height == 10
    assert df["trade_date"].unique().to_list() == [d]
    # 按 main_net_inflow 降序
    vals = df["main_net_inflow"].to_list()
    assert vals == sorted(vals, reverse=True)
    assert df["symbol"].str.contains(r"^\d{6}\.(SH|SZ)$").all()


def test_fetch_money_flow_demo_default_date_today():
    df = fetch_money_flow(demo=True, top=3)
    assert df.height == 3
    from lquant.core.types import today_cn

    # 缺省业务日是 today_cn()，不是进程本地 date.today()
    assert df["trade_date"].unique().to_list() == [today_cn()]


def test_fetch_money_flow_empty_diff(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url: _Resp({"data": None}))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 0 and df["symbol"].dtype == pl.Utf8


def test_fetch_money_flow_diff_as_dict(monkeypatch):
    """diff 为 dict 形态时取 values()。"""
    monkeypatch.setattr(mf, "em_get", lambda url: _Resp({"data": {"diff": {
        "0": {"f12": "600000", "f14": "浦发", "f2": 10.5, "f3": 1.2,
              "f62": 1e8, "f184": 5.0, "f66": 6e7, "f72": 4e7,
              "f78": -3e7, "f84": -2e7}}}}))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600000.SH" and row["name"] == "浦发"
    assert row["main_net_inflow"] == 1e8 and row["medium_net"] == -3e7


def test_fetch_money_flow_rows_and_bad_values(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url: _Resp({"data": {"diff": [
        {"f12": "600000", "f14": "A", "f2": "-", "f3": None, "f62": "1e8",
         "f184": 5.0, "f66": 1, "f72": 2, "f78": 3, "f84": 4},
        {"f12": "abc", "f14": "B", "f2": 1, "f3": 0, "f62": 0, "f184": 0,
         "f66": 0, "f72": 0, "f78": 0, "f84": 0},
    ]}}))
    df = fetch_money_flow("2026-01-05")
    assert df.height == 2
    assert df["close"].to_list()[0] == 0.0     # "-" → 0.0
    assert df["symbol"].to_list()[1] == "abc"  # 无法解析原样保留


def test_fetch_northbound_demo_deterministic():
    d = date(2026, 1, 5)
    df1 = fetch_northbound(d, demo=True)
    df2 = fetch_northbound(d, demo=True)
    assert df1.height == 1 and df1.height == df2.height
    assert df1["trade_date"].to_list() == [d]
    # 同日同种子 → 数值列一致（ts/collected_at 是 now_cn()，必然不同）
    for col in ("sh_net_inflow", "sz_net_inflow", "total_net_inflow"):
        assert df1[col].to_list() == df2[col].to_list()
    assert df1["total_net_inflow"][0] == pytest.approx(
        df1["sh_net_inflow"][0] + df1["sz_net_inflow"][0])


def test_fetch_northbound_primary_hk2sh_hk2sz(monkeypatch):
    """主接口 hk2sh/hk2sz：取每段最后一行的第 2 列为净买额。"""
    calls = []

    def fake_em_get(url):
        calls.append(url)
        return _Resp({"data": {"hk2sh": ["2026-01-05,100.5,x", "2026-01-06,1e8,x"],
                               "hk2sz": ["2026-01-06,3e8,x"]}})

    monkeypatch.setattr(mf, "em_get", fake_em_get)
    df = fetch_northbound(date(2026, 1, 6))
    assert calls == [calls[0]] and len(calls) == 1      # 主接口成功不调 rtmin
    assert df["sh_net_inflow"][0] == 1e8
    assert df["sz_net_inflow"][0] == 3e8
    assert df["total_net_inflow"][0] == pytest.approx(4e8)


def test_fetch_northbound_rtmin_fallback(monkeypatch):
    """主接口无数据时走 rtmin 兜底（s2n 行解析 SH/SZ）。"""
    calls = []

    def fake_em_get(url):
        calls.append(url)
        if "kamt.rtmin" in url:
            return _Resp({"data": {"s2n": [
                "2026-01-05 09:30,沪股通,100.0,50.0,1e8",
                "2026-01-05 09:30,深股通,20.0,10.0,3e8",
                "short,line",                     # 长度 <4 跳过
                "2026-01-05 09:31,未知市场,0,0,7.0",
            ]}})
        return _Resp({"data": None})

    monkeypatch.setattr(mf, "em_get", fake_em_get)
    df = fetch_northbound(date(2026, 1, 5))
    assert len(calls) == 2 and "kamt.rtmin" in calls[1]
    assert df["sh_net_inflow"][0] == 1e8
    assert df["sz_net_inflow"][0] == 3e8
    assert df["total_net_inflow"][0] == pytest.approx(4e8)


def test_fetch_northbound_no_data_raises_data_unavailable(monkeypatch):
    """主接口与 rtmin 兜底均无有效行 → DataUnavailable。"""
    from lquant.core.errors import DataUnavailable

    monkeypatch.setattr(mf, "em_get", lambda url: _Resp({"data": None}))
    with pytest.raises(DataUnavailable, match="北向资金无数据"):
        fetch_northbound(date(2026, 1, 5))


def test_fetch_northbound_rtmin_failure_raises_unavailable(monkeypatch):
    """主接口无数据、rtmin 段炸 → 吞异常后 found=False → DataUnavailable。"""
    def fake_em_get(url):
        if "kamt.rtmin" in url:
            raise RuntimeError("rtmin 炸了")
        return _Resp({"data": None})

    monkeypatch.setattr(mf, "em_get", fake_em_get)
    with pytest.raises(DataUnavailable):
        fetch_northbound(date(2026, 1, 5))


def test_fetch_northbound_first_call_raises_propagates(monkeypatch):
    """源码行为：第一段 kamt 请求不在 try 内，异常直接抛出。"""
    def boom(url):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(mf, "em_get", boom)
    with pytest.raises(RuntimeError):
        fetch_northbound(date(2026, 1, 5))
