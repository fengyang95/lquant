"""market/collectors/money_flow 单元测试（打桩 em_get，不联网）。"""

from __future__ import annotations

import re
from datetime import date

import polars as pl
import pytest

import lquant.market.collectors.money_flow as mf
from lquant.core.errors import DataUnavailable
from lquant.market.collectors.money_flow import (
    fetch_money_flow,
    fetch_money_flow_history,
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
    assert df["trade_date"].unique().to_list() == [date.today()]


def test_fetch_money_flow_empty_diff(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": None}))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 0 and df["symbol"].dtype == pl.Utf8


def test_fetch_money_flow_diff_as_dict(monkeypatch):
    """diff 为 dict 形态时取 values()。"""
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": {"diff": {
        "0": {"f12": "600000", "f14": "浦发", "f2": 10.5, "f3": 1.2,
              "f62": 1e8, "f184": 5.0, "f66": 6e7, "f72": 4e7,
              "f78": -3e7, "f84": -2e7}}}}))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600000.SH" and row["name"] == "浦发"
    assert row["main_net_inflow"] == 1e8 and row["medium_net"] == -3e7


def test_fetch_money_flow_rows_and_bad_values(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": {"diff": [
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

    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": None}))
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


# ============================================================ 横截面：全市场翻页
#
# 回归的是这次修掉的核心缺陷：老实现用 fid=f62&po=1&pz=top 取「净流入前 N」，
# 而东财把 pz 静默截断在 100 —— 于是 top=200 实际只有 100 只，且全是净流入，
# 任意一只普通股票在 money_flow 里永远没有行。


def _item(code: str, inflow: float = 1e8) -> dict:
    return {"f12": code, "f14": f"股{code}", "f2": 10.0, "f3": 1.0, "f62": inflow,
            "f184": 5.0, "f66": 6e7, "f72": 4e7, "f78": -3e7, "f84": -2e7}


def _paged(sizes: dict[int, int], total: int | None, calls: list[str]):
    """按 pn 返回指定条数；sizes 里没有的页返回 0 条。"""
    def fake(url, **kw):
        calls.append(url)
        pn = int(re.search(r"[?&]pn=(\d+)", url).group(1))
        diff = [_item(f"{600000 + (pn - 1) * 100 + i:06d}") for i in range(sizes.get(pn, 0))]
        return _Resp({"data": {"total": total, "diff": diff}})
    return fake


def test_fetch_money_flow_paginates_full_market(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mf, "em_get", _paged({1: 100, 2: 100, 3: 50}, 250, calls))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 250
    assert len(calls) == 3
    assert "pz=100" in calls[0] and "pn=1" in calls[0]
    assert "pn=3" in calls[2]
    assert df["source"].unique().to_list() == ["eastmoney"]


def test_fetch_money_flow_stops_once_total_reached(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mf, "em_get", _paged({1: 100}, 100, calls))
    assert fetch_money_flow(date(2026, 1, 5)).height == 100
    assert len(calls) == 1


def test_fetch_money_flow_stops_on_short_page_without_total(monkeypatch):
    """total 缺失也要能靠「短页」收敛，不能无限翻。"""
    calls: list[str] = []
    monkeypatch.setattr(mf, "em_get", _paged({1: 100}, None, calls))
    assert fetch_money_flow(date(2026, 1, 5)).height == 100
    assert len(calls) == 2


def test_fetch_money_flow_respects_max_pages(monkeypatch):
    """每页都满且 total 异常大时必须靠 MAX_PAGES 收敛（防死循环）。"""
    calls: list[str] = []

    def fake(url, **kw):
        calls.append(url)
        diff = [_item(f"{600000 + i:06d}") for i in range(mf.PAGE_SIZE)]
        return _Resp({"data": {"total": 10 ** 9, "diff": diff}})

    monkeypatch.setattr(mf, "em_get", fake)
    df = fetch_money_flow(date(2026, 1, 5))
    assert len(calls) == mf.MAX_PAGES
    assert df.height == mf.MAX_PAGES * mf.PAGE_SIZE


def test_fetch_money_flow_top_limits_rows_and_pages(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mf, "em_get", _paged({1: 100, 2: 100, 3: 100}, 300, calls))
    df = fetch_money_flow(date(2026, 1, 5), top=150)
    assert df.height == 150
    assert len(calls) == 2


def test_fetch_money_flow_page_failure_does_not_return_partial(monkeypatch):
    """后半页失败必须抛，不能返回「只有净流入靠前那些」的半份数据。"""
    def fake(url, **kw):
        if "pn=2" in url:
            raise RuntimeError("第二页炸了")
        return _Resp({"data": {"total": 300,
                               "diff": [_item(f"{600000 + i:06d}") for i in range(100)]}})

    monkeypatch.setattr(mf, "em_get", fake)
    with pytest.raises(RuntimeError, match="第二页炸了"):
        fetch_money_flow(date(2026, 1, 5))


def test_fetch_money_flow_empty_first_page(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": {"total": 0, "diff": []}}))
    df = fetch_money_flow(date(2026, 1, 5))
    assert df.height == 0 and df["source"].dtype == pl.Utf8


def test_fetch_money_flow_demo_marks_source():
    df = fetch_money_flow(date(2026, 1, 5), top=4, demo=True)
    assert df.height == 4
    assert df["source"].unique().to_list() == ["demo"]


def test_pagination_and_history_use_gentler_qps(monkeypatch):
    """全市场一轮 56 次请求，必须走比 em_get 默认 3 qps 更保守的速率。

    push2 集群有 WAF，被封 IP 的代价是资金流整体不可用，多花十几秒划算。
    """
    seen: list = []

    def fake(url, **kw):
        seen.append(kw.get("qps"))
        return _Resp({"data": {"total": 0, "diff": [], "klines": []}})

    monkeypatch.setattr(mf, "em_get", fake)
    fetch_money_flow(date(2026, 1, 5), qps=0.7)
    fetch_money_flow_history("600519", qps=0.8)
    assert seen == [0.7, 0.8]
    assert mf.PAGE_QPS < 3.0 and mf.HIST_QPS < 3.0


def test_secid_maps_exchange():
    assert mf._secid("600519.SH") == "1.600519"
    assert mf._secid("600519") == "1.600519"
    assert mf._secid("000001.SZ") == "0.000001"
    assert mf._secid("430047.BJ") == "0.430047"
    assert mf._secid("abc") == "0.abc"          # 解析失败不抛，原样拼


# ============================================================ 单票历史（逐日）
#
# 这是「随便输一个代码都有资金面」的唯一来源：横截面接口拿不到历史。


# 真实接口返回的 600519 一行，列顺序 f51..f63（已实测对账：
# 大单 + 超大单 == 主力净额，四类净额合计 ≈ 0）。
_HIST_LINE = ("2026-04-17,-986902000.0,-436874.0,987338896.0,-769410672.0,"
              "-217491328.0,-7.24,-0.00,7.24,-5.64,-1.60,1379.22,-3.88,0.00,0.00")


def _hist_resp(klines: list[str], name: str = "贵州茅台") -> _Resp:
    return _Resp({"data": {"code": "600519", "name": name, "klines": klines}})


def test_fetch_money_flow_history_maps_each_field(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _hist_resp([_HIST_LINE]))
    df = fetch_money_flow_history("600519")
    assert df.height == 1
    r = df.row(0, named=True)
    assert r["trade_date"] == date(2026, 4, 17)
    assert r["symbol"] == "600519.SH" and r["name"] == "贵州茅台"
    assert r["main_net_inflow"] == pytest.approx(-986902000.0)
    assert r["small_net"] == pytest.approx(-436874.0)
    assert r["medium_net"] == pytest.approx(987338896.0)
    assert r["large_net"] == pytest.approx(-769410672.0)
    assert r["super_large_net"] == pytest.approx(-217491328.0)
    assert r["main_net_ratio"] == pytest.approx(-7.24)
    assert r["close"] == pytest.approx(1379.22)
    assert r["change_pct"] == pytest.approx(-3.88)
    assert r["source"] == "history"
    # 接口口径自洽性：列一旦错位这条就会炸（比逐个断言更能挡住错位）
    assert r["main_net_inflow"] == pytest.approx(r["large_net"] + r["super_large_net"])
    assert r["main_net_ratio"] == pytest.approx(-7.24)


def test_fetch_money_flow_history_url_shape(monkeypatch):
    seen: list[str] = []

    def fake(url, **kw):
        seen.append(url)
        return _hist_resp([])

    monkeypatch.setattr(mf, "em_get", fake)
    fetch_money_flow_history("000001.SZ")
    assert len(seen) == 1
    assert "secid=0.000001" in seen[0]
    assert "lmt=0" in seen[0] and "klt=101" in seen[0]
    assert "f51" in seen[0] and "f63" in seen[0]


def test_fetch_money_flow_history_days_keeps_latest_ascending(monkeypatch):
    lines = [f"2026-04-{d:02d},1.0,0,0,0,0,1.0,0,0,0,0,10.0,0.0,0,0"
             for d in (10, 11, 12, 13, 14)]
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _hist_resp(lines))
    df = fetch_money_flow_history("600519", days=3)
    assert df["trade_date"].to_list() == [date(2026, 4, 12), date(2026, 4, 13),
                                         date(2026, 4, 14)]


def test_fetch_money_flow_history_skips_malformed_lines(monkeypatch):
    lines = [
        _HIST_LINE,
        "2026-04-18,1,2,3",                                    # 字段不足
        "not-a-date,1,2,3,4,5,6,7,8,9,10,11,12,13,14",         # 日期非法
        "2026-04-20,5.0,0,0,0,0,1.0,0,0,0,0,11.0,0.5,0,0",
    ]
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _hist_resp(lines))
    df = fetch_money_flow_history("600519")
    assert df["trade_date"].to_list() == [date(2026, 4, 17), date(2026, 4, 20)]


def test_fetch_money_flow_history_empty_payload(monkeypatch):
    monkeypatch.setattr(mf, "em_get", lambda url, **kw: _Resp({"data": None}))
    df = fetch_money_flow_history("600519")
    assert df.height == 0
    assert "source" in df.columns and df["symbol"].dtype == pl.Utf8


def test_fetch_money_flow_history_demo_deterministic_per_symbol():
    a = fetch_money_flow_history("600519.SH", days=5, demo=True)
    b = fetch_money_flow_history("600519.SH", days=5, demo=True)
    c = fetch_money_flow_history("000001.SZ", days=5, demo=True)
    assert a.height == 5
    assert a["main_net_inflow"].to_list() == b["main_net_inflow"].to_list()
    assert a["main_net_inflow"].to_list() != c["main_net_inflow"].to_list()
    assert a["source"].unique().to_list() == ["demo"]
    assert a["symbol"].unique().to_list() == ["600519.SH"]
