"""market/collectors/money_flow 单元测试（打桩 em_get，不联网）。"""

from __future__ import annotations

from datetime import date

import polars as pl

import lquant.market.collectors.money_flow as mf
from lquant.market.collectors.money_flow import fetch_money_flow


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
