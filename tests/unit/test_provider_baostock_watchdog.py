"""baostock provider 网络路径补测：watchdog / 请求函数全部打桩，不触网。

覆盖点：watchdog 子进程请求桩 + 逐方法（daily/minute/adj/securities/
details/calendar/etf_meta/financial_pit）+ 纯函数工具。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from lquant.data.providers.baostock import (
    BaoStockProvider,
    _add_years,
    _attach_is_st,
    _bs_code,
    _cal_chunks,
    _guess_sellable_days,
    _guess_track_index,
    _map_daily_raw,
    _quarters,
    _sec_type_of,
    _to_date,
    _year_slices,
)


class WatchStub:
    """替换 run_with_watchdog：按脚本逐次返回或抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple] = []

    def __call__(self, fn, *args, **kwargs):
        self.calls.append((getattr(fn, "__name__", fn), args))
        if not self.script:
            return []                          # 脚本耗尽 → 空结果（逐季循环用）
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def patch_wd(monkeypatch):
    def _install(script):
        stub = WatchStub(script)
        monkeypatch.setattr("lquant.data.watchdog.run_with_watchdog", stub)
        return stub

    return _install


# ---------------------------------------------------------------- 纯函数

def test_add_years_leap_day() -> None:
    assert _add_years(date(2024, 2, 29), 1) == date(2025, 2, 28)
    assert _add_years(date(2023, 3, 1), 2) == date(2025, 3, 1)


def test_cal_chunks_bounds() -> None:
    # <5 年 → 单块
    assert _cal_chunks(date(2020, 1, 1), date(2024, 1, 1)) == [
        (date(2020, 1, 1), date(2024, 1, 1))]
    # 11 年 → 3 块且首尾相接
    chunks = _cal_chunks(date(2015, 1, 1), date(2025, 12, 31))
    assert len(chunks) == 3
    from datetime import timedelta

    for (_, e), (s, _) in zip(chunks, chunks[1:], strict=False):
        assert e + timedelta(days=1) == s


def test_quarters_and_year_slices() -> None:
    assert list(_quarters(date(2023, 6, 1), date(2024, 2, 1))) == [
        (2023, q) for q in (1, 2, 3, 4)] + [(2024, q) for q in (1, 2, 3, 4)]
    assert _year_slices(date(2022, 11, 1), date(2024, 2, 1)) == [
        (date(2022, 11, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 2, 1)),
    ]


def test_bs_code_and_misc() -> None:
    assert _bs_code("600519.SH") == "sh.600519"
    assert _bs_code("000001.SZ") == "sz.000001"
    assert _sec_type_of("510300.SH").value == "etf"
    assert _sec_type_of("garbage") == _sec_type_of("garbage")
    assert _to_date("2024-01-02") == date(2024, 1, 2)
    assert _to_date("") is None and _to_date("0") is None and _to_date("None") is None
    assert _to_date("2024-13-99") is None
    assert _guess_sellable_days("华夏纳指ETF") == 0
    assert _guess_sellable_days("沪深300ETF") == 1
    assert _guess_sellable_days("国债ETF", "5年") == 0
    assert _guess_track_index("沪深300ETF") == "沪深300"
    assert _guess_track_index("ETF") is None
    assert _guess_track_index("") is None
    assert _guess_track_index("创业板 ETF基金") == "创业板"


def test_attach_is_st_raw_mismatch_raises() -> None:
    out = pl.DataFrame({"a": [1]})
    from lquant.core.errors import DataQualityError

    with pytest.raises(DataQualityError):
        _attach_is_st(out, pl.DataFrame({"is_st": ["1", "0"]}))


def test_attach_is_st_passthrough_and_empty() -> None:
    out = pl.DataFrame({"a": [1], "is_st": [True]})
    assert _attach_is_st(out, pl.DataFrame({"is_st": ["1"]})) is out
    out2 = pl.DataFrame({"a": [1]})
    r = _attach_is_st(out2, pl.DataFrame())
    assert r["is_st"].to_list() == [None]
    # raw 有 is_st → 挂回
    out3 = pl.DataFrame({"a": [1, 2]})
    r3 = _attach_is_st(out3, pl.DataFrame({"is_st": ["1", "true"]}))
    assert r3["is_st"].to_list() == [True, True]


# ---------------------------------------------------------------- fetch 路径

RAW_DAILY = [
    ["2024-01-02", "sz.000001", "10.0", "10.2", "9.9", "10.1", "10.0",
     "1000000", "1.0e7", "0.8", "1", "0", "1.0", "5.0", "1.0", "1.0", "2.0"],
]


def test_fetch_raw_unknown_table() -> None:
    with pytest.raises(NotImplementedError):
        BaoStockProvider()._fetch_raw("nope")


def test_fetch_daily_success_and_retry(patch_wd) -> None:
    stub = patch_wd([
        RuntimeError("baostock 10001001: 用户未登录"),
        RuntimeError("baostock 10001001: 用户未登录"),
        [("sz.000001", RAW_DAILY)],
    ])
    p = BaoStockProvider()
    df = p._fetch_daily(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2))
    assert len(df) == 1
    assert stub.calls[0][0] == "_bs_query_many"


def test_fetch_daily_empty(patch_wd) -> None:
    patch_wd([[]])
    df = BaoStockProvider()._fetch_daily(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2))
    assert df.is_empty()


def test_fetch_daily_runtime_error_propagates(patch_wd) -> None:
    patch_wd([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        BaoStockProvider()._fetch_daily(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2))


def test_daily_bars_end_to_end(patch_wd) -> None:
    patch_wd([[("sz.000001", RAW_DAILY)]])
    df = BaoStockProvider().daily_bars(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2))
    assert "is_st" in df.columns
    assert df["symbol"][0] == "000001.SZ"


def test_fetch_minute_ok_and_bad_freq(patch_wd) -> None:
    rows = [["2024-01-02 10:00", "20240102100000000", "sz.000001",
             "10.0", "10.2", "9.9", "10.1", "1000", "10000.0", "3"]]
    patch_wd([rows])
    df = BaoStockProvider()._fetch_minute(
        ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), "60min")
    assert df["ts"][0].hour == 10 and df["close"][0] == 10.1
    with pytest.raises(ValueError):
        BaoStockProvider()._fetch_minute(
            ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), "7min")


def test_fetch_minute_empty(patch_wd) -> None:
    patch_wd([[]])
    df = BaoStockProvider()._fetch_minute(
        ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), "60min")
    assert df.is_empty()


def test_minute_bars_request(patch_wd) -> None:
    rows = [["2024-01-02 10:00", "20240102100000000", "sz.000001",
             "10.0", "10.2", "9.9", "10.1", "1000", "10000.0", "3"]]
    stub = patch_wd([rows])
    df = BaoStockProvider().minute_bars(
        ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2), "60min")
    assert not df.is_empty()
    assert stub.calls[0][0] == "_bs_query"


def test_post_normalize_minute() -> None:
    p = BaoStockProvider()
    df = pl.DataFrame({
        "symbol": ["600000.SH"],
        "ts": [datetime(2024, 1, 2, 11, 30)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0], "freq": ["60min"],
    })
    out = p._post_normalize(df, "minute_bar")
    assert "ingested_at" in out.columns
    # daily_bar 路径只做符号归一
    d2 = pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2024, 1, 2)]})
    assert p._post_normalize(d2, "daily_bar")["symbol"][0] == "600000.SH"


# ---------------------------------------------------------------- 复权

def test_adj_factors_ok(patch_wd) -> None:
    raw = [["2024-01-02", "sz.000001", "10.0"], ["2024-01-03", "sz.000001", "10.5"]]
    hfq = [["2024-01-02", "sz.000001", "20.0"], ["2024-01-03", "sz.000001", "21.0"]]
    patch_wd([raw, hfq])
    df = BaoStockProvider().adj_factors(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 3))
    assert df["factor"].to_list() == pytest.approx([2.0, 2.0])
    assert df["source"].unique().to_list() == ["baostock"]


def test_adj_factors_zero_close_and_mismatch(patch_wd) -> None:
    """停牌行（close<=0）因子无定义：沿组内前值 forward-fill，无前值则丢弃。

    此前兜底 1.0 会把除权前的真实因子覆写成 1.0 —— 回测按因子比调整
    份额时凭空缩水持仓（2026-10 审计修复）。
    """
    # 单点停牌行：无前值可沿用 → 整帧为空（绝不输出假因子 1.0）
    raw = [["2024-01-02", "sz.000001", "0"]]
    hfq = [["2024-01-02", "sz.000001", "20.0"]]
    patch_wd([raw, hfq])
    df = BaoStockProvider().adj_factors(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2))
    assert df.is_empty()
    # 长度不一致 → 跳过 → 空帧
    patch_wd([[raw[0]], []])
    assert BaoStockProvider().adj_factors(
        ["000001.SZ"], date(2024, 1, 2), date(2024, 1, 2)).is_empty()


def test_adj_factors_halt_forward_fills_previous_factor(patch_wd) -> None:
    """停牌次日沿用停牌前的最后有效因子，而不是回落 1.0。"""
    raw = [["2024-01-02", "sz.000001", "10.0"], ["2024-01-03", "sz.000001", "0"]]
    hfq = [["2024-01-02", "sz.000001", "20.0"], ["2024-01-03", "sz.000001", "21.0"]]
    patch_wd([raw, hfq])
    df = BaoStockProvider().adj_factors(["000001.SZ"], date(2024, 1, 2), date(2024, 1, 3))
    assert df["factor"].to_list() == pytest.approx([2.0, 2.0])


# ---------------------------------------------------------------- 参考数据

SEC_ROWS = [
    ["sz.000001", "1", "平安银行"],
    ["sh.510300", "1", "沪深300ETF"],
]


def test_securities_nonempty(patch_wd) -> None:
    patch_wd([SEC_ROWS])
    df = BaoStockProvider().securities(date(2024, 1, 2))
    assert set(df["symbol"]) == {"000001.SZ", "510300.SH"}
    assert df["is_st"].to_list() == [False, False]
    assert set(df["sec_type"]) == {"stock", "etf"}


def test_securities_probe_backfill(patch_wd) -> None:
    # 首日为空 → 回退探测；第二次仍空、第三次有数据
    patch_wd([[], [], SEC_ROWS])
    df = BaoStockProvider().securities(date(2024, 1, 2))
    assert not df.is_empty()


def test_securities_all_empty(patch_wd) -> None:
    patch_wd([[]] * 11)
    assert BaoStockProvider().securities(date(2024, 1, 2)).is_empty()


def test_security_details(patch_wd) -> None:
    stub = patch_wd([
        TimeoutError("t"),                      # 超时 → 跳过
        [],                                     # 空 → 跳过
        [["sz.000001", "平安银行", "1991-04-03", "", "1"]],
    ])
    df = BaoStockProvider().security_details(["000001.SZ", "600000.SH", "000002.SZ"])
    assert len(df) == 1
    assert df["symbol"][0] == "000001.SZ"
    assert df["list_date"][0] == date(1991, 4, 3)
    assert df["delist_date"][0] is None
    assert stub.calls[0][1][0] == "sz.000001"


def test_trade_calendar(patch_wd) -> None:
    patch_wd([[["2024-01-02", "1"], ["2024-01-03", "0"]]])
    df = BaoStockProvider().trade_calendar(date(2024, 1, 1), date(2024, 1, 3))
    assert df["is_open"].to_list() == [True, False]
    assert df["exchange"].unique().to_list() == ["SSE"]


def test_trade_calendar_chunked(patch_wd) -> None:
    stub = patch_wd([[["2020-01-02", "1"]], [["2025-06-01", "1"]], [["2031-01-02", "1"]]])
    df = BaoStockProvider().trade_calendar(date(2020, 1, 1), date(2031, 12, 31))
    assert len(df) == 3
    assert len(stub.calls) == 3


def test_etf_meta(patch_wd, monkeypatch) -> None:
    p = BaoStockProvider()
    secs = pl.DataFrame({
        "symbol": ["510300.SH", "600000.SH", "501300.SH"],
        "name": ["沪深300ETF", "平安银行", "国泰纳指LOF"],
        "sec_type": ["etf", "stock", "lof"],
        "board": ["main", "main", "main"],
        "is_st": [False, False, False],
        "trade_status": ["1", "1", "1"],
    })
    monkeypatch.setattr(p, "securities", lambda day=None: secs)
    df = p.etf_meta()
    assert set(df["symbol"]) == {"510300.SH", "501300.SH"}
    assert df["sellable_after_days"].to_list() == [1, 0]
    assert df["track_index"].to_list() == ["沪深300", "国泰纳指"]
    assert df["source"].unique().to_list() == ["baostock"]
    # 全无 ETF → 空
    monkeypatch.setattr(p, "securities", lambda day=None: secs.filter(
        pl.col("sec_type") == "stock"))
    assert p.etf_meta().is_empty()
    monkeypatch.setattr(p, "securities", lambda day=None: pl.DataFrame())
    assert p.etf_meta().is_empty()


# ---------------------------------------------------------------- 财务

HEAD = ["code", "statDate", "pubDate", "roe", "code_x"]
PAYLOAD = [HEAD, ["sz.000001", "2024-03-31", "2024-04-20", "3.5", "junk"]]


def test_financial_pit_ok(patch_wd) -> None:
    patch_wd([PAYLOAD])
    df = BaoStockProvider().financial_pit(
        ["000001.SZ"], date(2024, 1, 1), date(2024, 3, 31),
        kinds=("profit",))
    rows = df.filter(pl.col("item") == "profit.roe")
    assert rows["value"][0] == 3.5
    assert rows["report_type"][0] == "2024Q1"
    assert rows["pub_date"][0] == date(2024, 4, 20)


def test_financial_pit_skip_paths(patch_wd) -> None:
    """数据级脏行跳过不炸（缺 pubDate/空日期/非数字），有效行照常返回。"""
    bad = [["code", "statDate"], ["sz.000001", "2024-03-31"]]  # 缺 pubDate
    nodate = [["code", "statDate", "pubDate", "roe"],
              ["sz.000001", "", "2024-04-20", "1.0"]]           # 无报告期
    nonnum = [["code", "statDate", "pubDate", "roe"],
              ["sz.000001", "2024-03-31", "2024-04-20", "n/a"]]
    patch_wd([
        [HEAD],                     # 只有表头 <2 行
        bad,                        # 缺 pubDate
        nodate,                     # 日期空
        nonnum,                     # 值非数字
    ])
    df = BaoStockProvider().financial_pit(
        ["000001.SZ"], date(2024, 1, 1), date(2024, 3, 31), kinds=("profit",))
    assert df.is_empty()


def test_financial_pit_network_failure_raises(patch_wd) -> None:
    """网络类失败整体报错：本批已拉到的 recs 一并放弃（下次重跑重拉）。

    半成功半失败的批次一旦被记 coverage，缺口就永久不可发现 ——
    宁可失败留给上层批次级重试。
    """
    patch_wd([
        [HEAD, ["sz.000001", "2024-03-31", "2024-04-20", "3.5"]],
        TimeoutError("t"),          # 第二个查询点超时 → 整体失败
    ])
    with pytest.raises(RuntimeError, match="financial_pit 有 1 个查询点失败"):
        BaoStockProvider().financial_pit(
            ["000001.SZ"], date(2024, 1, 1), date(2024, 3, 31), kinds=("profit",))


def test_financial_pit_multiple_quarters(patch_wd) -> None:
    p1 = [HEAD, ["sz.000001", "2023-12-31", "2024-01-15", "3.0"]]
    p2 = [HEAD, ["sz.000001", "2024-03-31", "2024-04-20", "3.5"]]
    patch_wd([p1, p2])
    df = BaoStockProvider().financial_pit(
        ["000001.SZ"], date(2024, 1, 1), date(2024, 3, 31), kinds=("profit",))
    assert len(df) == 2


def test_map_daily_raw_picklable() -> None:
    import pickle

    assert pickle.loads(pickle.dumps(_map_daily_raw)) is _map_daily_raw
