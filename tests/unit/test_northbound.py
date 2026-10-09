"""market/collectors/northbound 单元测试（打桩 em_get，不联网）。

覆盖三条容易被静默吞掉的边界：
1. 净买额 2024-08-19 停发 → 之后必须是 NULL + net_published=false，不能是 0；
2. 报表缺行 / 合计不自洽 → fail-loud，不写半条记录；
3. 单位换算（报表百万元 → 元）。
"""

from __future__ import annotations

from datetime import date

import pytest

import lquant.market.collectors.northbound as nb
from lquant.core.errors import DataUnavailable, SourceSchemaChanged
from lquant.market.collectors.northbound import (
    NORTHBOUND_NET_LAST_DATE,
    fetch_northbound,
    fetch_northbound_top10,
)

# 实测样本：2026-10-08（净买额已停发）
_DEAL_TYPES = {"001": 134535.72, "003": 142619.39, "005": 277155.11}


class _Resp:
    def __init__(self, payload) -> None:
        self._p = payload

    def json(self) -> dict:
        return self._p


def _deal_payload(day: str, *, net: dict | None = None, deal: dict | None = None,
                  types=("001", "003", "005")) -> dict:
    amounts = dict(_DEAL_TYPES)
    if deal:
        amounts.update(deal)
    net = net or {}
    data = []
    for t in types:
        data.append({
            "TRADE_DATE": f"{day} 00:00:00", "MUTUAL_TYPE": t,
            "DEAL_AMT": amounts.get(t), "DEAL_NUM": 6824276,
            "BUY_AMT": None, "SELL_AMT": None,
            "NET_DEAL_AMT": net.get(t),
        })
    return {"success": True, "result": {"data": data}}


def test_net_last_date_is_the_measured_cutoff():
    """口径常量必须钉在实测出来的那一天，改它得先重新实测。"""
    assert date(2024, 8, 16) == NORTHBOUND_NET_LAST_DATE
    assert date(2024, 8, 19) > NORTHBOUND_NET_LAST_DATE


def test_deal_amt_unit_is_million_yuan():
    """报表金额单位是百万元：合计 277155.11 → 2.77 万亿分之……换算成元。"""
    assert nb._AMT_UNIT == 1e6


def test_fetch_northbound_after_cutoff_nulls_net(monkeypatch):
    """停发日之后：成交额照落，净买额为 NULL 且 net_published=False。"""
    monkeypatch.setattr(nb, "em_get",
                        lambda url, **kw: _Resp(_deal_payload("2026-10-08")))
    df = fetch_northbound(date(2026, 10, 8))
    assert df.height == 1
    r = df.row(0, named=True)
    assert r["net_published"] is False
    assert r["sh_net_inflow"] is None and r["sz_net_inflow"] is None
    assert r["total_net_inflow"] is None           # 关键：不是 0.0
    assert r["sh_deal_amt"] == pytest.approx(134535.72 * 1e6)
    assert r["total_deal_amt"] == pytest.approx(277155.11 * 1e6)
    assert r["deal_num"] == 6824276


def test_fetch_northbound_before_cutoff_keeps_net(monkeypatch):
    """停发日及以前：净买额是真实历史，必须落库（单位同样 ×1e6）。"""
    payload = _deal_payload("2024-08-16", net={"001": -2568.22, "003": 100.0,
                                                "005": -2468.22})
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(payload))
    r = fetch_northbound(date(2024, 8, 16)).row(0, named=True)
    assert r["net_published"] is True
    assert r["sh_net_inflow"] == pytest.approx(-2568.22 * 1e6)
    assert r["total_net_inflow"] == pytest.approx(-2468.22 * 1e6)


def test_fetch_northbound_net_missing_before_cutoff_fails_loud(monkeypatch):
    """口径声明该日应披露净买额却拿到空 → 报错，而不是静默写 None。"""
    monkeypatch.setattr(nb, "em_get",
                        lambda url, **kw: _Resp(_deal_payload("2024-08-16")))
    with pytest.raises(DataUnavailable, match="净买额应已披露"):
        fetch_northbound(date(2024, 8, 16))


def test_fetch_northbound_missing_type_fails_loud(monkeypatch):
    """缺 001/003/005 任一行 → 报错（旧代码会 KeyError 或写 0）。"""
    payload = _deal_payload("2026-10-08", types=("001", "003"))
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(payload))
    with pytest.raises(DataUnavailable, match=r"缺 MUTUAL_TYPE \['005'\]"):
        fetch_northbound(date(2026, 10, 8))


def test_fetch_northbound_inconsistent_total_fails_loud(monkeypatch):
    """合计 != 沪 + 深（源站串列）→ 报错，别把错数写进看板。"""
    payload = _deal_payload("2026-10-08", deal={"005": 999999.0})
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(payload))
    with pytest.raises(DataUnavailable, match="不自洽"):
        fetch_northbound(date(2026, 10, 8))


def test_fetch_northbound_zero_deal_amt_fails_loud(monkeypatch):
    payload = _deal_payload("2026-10-08", deal={"001": 0.0})
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(payload))
    with pytest.raises(DataUnavailable, match="成交额非正"):
        fetch_northbound(date(2026, 10, 8))


def test_fetch_northbound_empty_result_fails_loud(monkeypatch):
    monkeypatch.setattr(nb, "em_get",
                        lambda url, **kw: _Resp({"success": True, "result": {"data": []}}))
    with pytest.raises(DataUnavailable, match="无数据"):
        fetch_northbound(date(2026, 10, 3))     # 国庆假期


def test_fetch_northbound_report_gone_raises_schema_changed(monkeypatch):
    """源站撤报表 → 永久错误（SourceSchemaChanged），重试无意义。"""
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(
        {"success": False, "message": "报表配置不存在,RPT_MUTUAL_DEAL_HISTORY"}))
    with pytest.raises(SourceSchemaChanged, match="RPT_MUTUAL_DEAL_HISTORY"):
        fetch_northbound(date(2026, 10, 8))


def test_fetch_northbound_request_error_is_data_unavailable(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(nb, "em_get", boom)
    with pytest.raises(DataUnavailable, match="请求失败"):
        fetch_northbound(date(2026, 10, 8))


def test_fetch_northbound_days_window_sorted_desc(monkeypatch):
    """days>1：多交易日按日期倒序返回（看板近 N 日）。"""
    def fake(url, **kw):
        assert "TRADE_DATE>=" in url.replace("%3E%3D", ">=") or "TRADE_DATE" in url
        return _Resp({"success": True, "result": {"data": [
            {"TRADE_DATE": "2026-10-08 00:00:00", "MUTUAL_TYPE": t,
             "DEAL_AMT": v, "DEAL_NUM": 1, "NET_DEAL_AMT": None}
            for t, v in _DEAL_TYPES.items()
        ] + [
            {"TRADE_DATE": "2026-09-30 00:00:00", "MUTUAL_TYPE": t,
             "DEAL_AMT": v / 2, "DEAL_NUM": 2, "NET_DEAL_AMT": None}
            for t, v in _DEAL_TYPES.items()
        ]}})

    monkeypatch.setattr(nb, "em_get", fake)
    df = fetch_northbound(date(2026, 10, 8), days=10)
    assert df["trade_date"].to_list() == [date(2026, 10, 8), date(2026, 9, 30)]


def test_fetch_northbound_demo_deterministic_and_null_after_cutoff():
    d = date(2026, 1, 5)
    df1 = fetch_northbound(d, demo=True)
    df2 = fetch_northbound(d, demo=True)
    assert df1.height == 1 and df1.height == df2.height
    assert df1["trade_date"].to_list() == [d]
    assert df1["sh_deal_amt"].to_list() == df2["sh_deal_amt"].to_list()
    assert df1["net_published"].to_list() == [False]
    assert df1["total_net_inflow"].to_list() == [None]
    # 停发前的历史日期 demo 仍给净买额
    old = fetch_northbound(date(2024, 8, 15), demo=True)
    assert old["net_published"].to_list() == [True]
    assert old["total_net_inflow"][0] == pytest.approx(
        old["sh_net_inflow"][0] + old["sz_net_inflow"][0])


def test_fetch_northbound_demo_days():
    df = fetch_northbound(date(2026, 10, 8), days=3, demo=True)
    assert df["trade_date"].to_list() == [date(2026, 10, 8), date(2026, 10, 7),
                                          date(2026, 10, 6)]


# ---- 前十大活跃 ----

def _top10_payload(day: str = "2026-10-08") -> dict:
    rows = []
    for t, code, name in (("001", "603259.SH", "药明康德"), ("003", "300308.SZ", "中际旭创")):
        rows.append({"TRADE_DATE": f"{day} 00:00:00", "MUTUAL_TYPE": t,
                     "SECURITY_CODE": code.split(".")[0], "DERIVE_SECURITY_CODE": code,
                     "SECURITY_NAME": name, "RANK": 1, "CLOSE_PRICE": 162.0,
                     "CHANGE_RATE": -3.19, "DEAL_AMT": 2729984203,
                     "MUTUAL_RATIO": 36.72, "NET_BUY_AMT": None})
    return {"success": True, "result": {"data": rows}}


def test_fetch_top10_maps_and_sorts(monkeypatch):
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(_top10_payload()))
    df = fetch_northbound_top10(date(2026, 10, 8))
    assert df.height == 2
    assert df["symbol"].to_list() == ["603259.SH", "300308.SZ"]
    r = df.row(0, named=True)
    assert r["board"] == "沪股通" and r["rank_no"] == 1
    assert r["deal_amt"] == pytest.approx(2729984203)   # 该报表金额单位是元
    assert r["mutual_ratio"] == pytest.approx(36.72)
    assert df.schema["trade_date"] == nb.pl.Date


def test_fetch_top10_empty_fails_loud(monkeypatch):
    monkeypatch.setattr(nb, "em_get",
                        lambda url, **kw: _Resp({"success": True, "result": {"data": []}}))
    with pytest.raises(DataUnavailable, match="前十大活跃无数据"):
        fetch_northbound_top10(date(2026, 10, 3))


def test_fetch_top10_skips_rows_without_code(monkeypatch):
    payload = _top10_payload()
    payload["result"]["data"].append({"TRADE_DATE": "2026-10-08 00:00:00",
                                      "MUTUAL_TYPE": "001", "RANK": 9})
    monkeypatch.setattr(nb, "em_get", lambda url, **kw: _Resp(payload))
    assert fetch_northbound_top10(date(2026, 10, 8)).height == 2


def test_fetch_top10_demo_deterministic():
    d = date(2026, 1, 5)
    a, b = fetch_northbound_top10(d, demo=True), fetch_northbound_top10(d, demo=True)
    assert a.height == 20
    assert a["deal_amt"].to_list() == b["deal_amt"].to_list()
    assert set(a["board"].to_list()) == {"沪股通", "深股通"}


def test_as_date_variants():
    assert nb._as_date("2026-01-05") == date(2026, 1, 5)
    assert nb._as_date("20260105") == date(2026, 1, 5)
    assert nb._as_date(date(2025, 12, 31)) == date(2025, 12, 31)


def test_num_distinguishes_null_from_zero():
    assert nb._num(None) is None and nb._num("") is None and nb._num("-") is None
    assert nb._num(0) == 0.0
    assert nb._num(object()) is None


# ---- 老库迁移：把「停发却写成 0」的历史行修成 NULL ----

def _duck():
    import duckdb

    return duckdb.connect()


def test_migrate_northbound_nulls_bogus_zeros():
    """旧库里 2026-09 之后的 0.0 要变成 NULL + net_published=false。

    0 会被下游当成「净买额为零」这个观测值，而真实语义是「口径停发」。
    """
    from lquant.market.schema import _migrate_northbound

    con = _duck()
    con.execute("""CREATE TABLE northbound_flow (
        trade_date DATE, ts TIMESTAMP, sh_net_inflow DOUBLE, sz_net_inflow DOUBLE,
        total_net_inflow DOUBLE, collected_at TIMESTAMP)""")
    con.execute("INSERT INTO northbound_flow VALUES "
                "('2026-09-08', '2026-09-08 20:00:00', -6.7e9, 8.4e8, -5.9e9, NULL),"
                "('2026-09-09', '2026-09-09 18:04:29', 0.0, 0.0, 0.0, NULL),"
                "('2024-08-16', '2024-08-16 18:00:00', -2.5e9, 1.0e9, -1.5e9, NULL)")
    _migrate_northbound(con)
    rows = dict(con.execute(
        "SELECT trade_date, (sh_net_inflow IS NULL, net_published) "
        "FROM northbound_flow ORDER BY trade_date").fetchall())
    assert rows[date(2026, 9, 8)] == (True, False)
    assert rows[date(2026, 9, 9)] == (True, False)
    assert rows[date(2024, 8, 16)] == (False, True)      # 真实历史保留
    cols = {r[0] for r in con.execute("DESCRIBE northbound_flow").fetchall()}
    assert {"sh_deal_amt", "sz_deal_amt", "total_deal_amt", "deal_num",
            "net_published"} <= cols


def test_migrate_northbound_skips_missing_table():
    from lquant.market.schema import _migrate_northbound

    _migrate_northbound(_duck())      # 表不存在：静默返回，交给正常建表


def test_migrate_northbound_idempotent():
    from lquant.market.schema import _migrate_northbound

    con = _duck()
    con.execute("""CREATE TABLE northbound_flow (
        trade_date DATE, ts TIMESTAMP, sh_net_inflow DOUBLE, sz_net_inflow DOUBLE,
        total_net_inflow DOUBLE, collected_at TIMESTAMP)""")
    _migrate_northbound(con)
    _migrate_northbound(con)
    cols = [r[0] for r in con.execute("DESCRIBE northbound_flow").fetchall()]
    assert cols.count("net_published") == 1
