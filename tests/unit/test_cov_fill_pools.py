"""批次一覆盖补充：limit_up.py / dragon_tiger.py 采集器（打桩 em_get）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.core.errors import DataUnavailable
from lquant.market.collectors import dragon_tiger as dt
from lquant.market.collectors import limit_up as lu


class _Resp:
    def __init__(self, payload=None, exc: Exception | None = None) -> None:
        self._payload = payload
        self._exc = exc

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


# ---------- limit_up: 工具函数 ----------


def test_ymd_variants():
    from lquant.core.types import today_cn

    assert lu._ymd(None) == today_cn().strftime("%Y%m%d")
    assert lu._ymd("2024-01-02") == "20240102"
    assert lu._ymd(date(2024, 1, 2)) == "20240102"


def test_ts_to_hhmmss_variants():
    # fbt/lbt 是 HHMMSS 整数（92503=09:25:03，akshare zfill(6) 同口径），
    # 不是 Unix 秒 —— 旧实现 fromtimestamp 产出 1970 年垃圾时刻
    assert lu._ts_to_hhmmss(0) == ""
    assert lu._ts_to_hhmmss(None) == ""
    assert lu._ts_to_hhmmss(92503) == "09:25:03"
    assert lu._ts_to_hhmmss(140100) == "14:01:00"
    assert lu._ts_to_hhmmss("bad") == "bad"


def test_norm_symbol_fallback():
    assert lu._norm_symbol("600000") == "600000.SH"
    assert lu._norm_symbol("AB12") == "AB12"  # parse 失败原样返回


def test_fetch_pool_bad_json(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(exc=ValueError("x")))
    with pytest.raises(DataUnavailable, match="响应非 JSON"):
        lu._fetch_pool("ZT")


def test_fetch_pool_empty_and_rows(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp({}))
    assert lu._fetch_pool("ZT") == []
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(
        {"data": {"pool": [{"c": "600000", "n": "浦发银行"}]}}))
    assert lu._fetch_pool("ZT")[0]["n"] == "浦发银行"
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp({"data": {}}))
    assert lu._fetch_pool("ZT") == []


def test_limit_type_branches():
    assert lu._limit_type({"h": 10000, "l": 10000}) == "一字板"
    assert lu._limit_type({"h": 10000, "l": 9000, "zbc": 2, "lbc": 3}) == "T字板"
    assert lu._limit_type({"h": 10000, "l": 9000, "zbc": 1, "lbc": 1}) == "换手板"
    assert lu._limit_type({}) == "换手板"


# ---------- limit_up: 三个池 ----------


def _zt_items():
    return [{
        "c": "600000", "n": "浦发银行", "p": 12345, "zdp": 10.0, "fund": 1.5e8,
        "hs": 512, "fbt": 93000, "lbt": 150000, "zbc": 1,
        "lbc": 2, "h": 12345, "l": 11000, "hybk": "银行",
    }, {
        "c": "000001", "n": "平安银行", "p": 8000, "zdp": 10.0, "fund": 0,
        "hs": 0, "fbt": None, "lbt": None, "zbc": 0, "lbc": 1,
        "h": 8000, "l": 8000, "hybk": "银行",
    }]


def test_fetch_limit_up_pool_rows(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(
        {"data": {"pool": _zt_items()}}))
    df = lu.fetch_limit_up_pool("2024-01-02")
    assert df["symbol"].to_list() == ["600000.SH", "000001.SZ"]
    assert df["close"].to_list() == [12.345, 8.0]
    assert df["limit_up_type"].to_list() == ["T字板", "一字板"]
    # fbt=93000 按 HHMMSS 口径 → 09:30:00（不是 fromtimestamp 秒数）
    assert df["first_limit_time"][0] == "09:30:00"
    assert df["last_limit_time"][0] == "15:00:00"
    # fund（封单资金）落 seal_amount，量纲与 amount（成交额）分离
    assert df["seal_amount"].to_list() == [1.5e8, 0.0]


def test_fetch_limit_up_pool_empty(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp({}))
    df = lu.fetch_limit_up_pool("2024-01-02")
    assert isinstance(df, pl.DataFrame) and len(df) == 0
    assert "symbol" in df.columns and "limit_up_type" in df.columns


def test_fetch_broken_pool_rows_and_empty(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(
        {"data": {"pool": [{"c": "300001", "n": "X", "p": 5000, "zdp": 3.2,
                            "fund": 1e7, "fbt": 3600, "zbc": 2, "hybk": "医药"}]}}))
    df = lu.fetch_broken_pool("2024-01-02")
    assert df["symbol"][0] == "300001.SZ"
    assert df["open_count"][0] == 2
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(None))
    empty = lu.fetch_broken_pool("2024-01-02")
    assert len(empty) == 0 and "first_limit_time" in empty.columns


def test_fetch_limit_down_pool_rows_and_empty(monkeypatch):
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp(
        {"data": {"pool": [{"c": "600001", "n": "Y", "p": 4000, "zdp": -10.0,
                            "fund": None, "hybk": "地产"}]}}))
    df = lu.fetch_limit_down_pool("2024-01-02")
    assert df["symbol"][0] == "600001.SH"
    assert df["close"][0] == 4.0
    monkeypatch.setattr(lu, "em_get", lambda url: _Resp({"data": None}))
    empty = lu.fetch_limit_down_pool("2024-01-02")
    assert len(empty) == 0 and "amount" in empty.columns


def test_demo_pools_all_kinds():
    up = lu.fetch_limit_up_pool("2024-01-02", demo=True)
    broken = lu.fetch_broken_pool("2024-01-02", demo=True)
    down = lu.fetch_limit_down_pool("2024-01-02", demo=True)
    assert len(up) == 42 and len(broken) == 15 and len(down) == 6
    assert "limit_up_type" in up.columns
    assert "turnover_rate" not in down.columns


# ---------- dragon_tiger ----------


def test_num_variants():
    assert dt._num(None) == 0.0
    assert dt._num("") == 0.0
    assert dt._num("-") == 0.0
    assert dt._num("3.5") == 3.5
    assert dt._num("abc") == 0.0
    assert dt._num(object()) == 0.0


def test_norm_fallback():
    assert dt._norm("600000") == "600000.SH"
    assert dt._norm("ZZZZ") == "ZZZZ"


def test_as_date_variants(monkeypatch):
    # 业务日期走 core.types.today_cn（Asia/Shanghai），不再是模块级 datetime.now()
    # ——桩要打在 today_cn 上，打 datetime 现在不生效（本用例曾是「假绿」）。
    monkeypatch.setattr(dt, "today_cn", lambda: date(2026, 9, 20))  # 周日
    assert dt._as_date(None) == date(2026, 9, 18)  # 昨日周六 → 回退到周五

    # 周六 / 周一两个边界：回退逻辑不能把「昨日=周五」也往前推
    monkeypatch.setattr(dt, "today_cn", lambda: date(2026, 9, 22))  # 周二 → 昨日周一
    assert dt._as_date(None) == date(2026, 9, 21)
    monkeypatch.setattr(dt, "today_cn", lambda: date(2026, 9, 21))  # 周一 → 昨日周日
    assert dt._as_date(None) == date(2026, 9, 18)

    assert dt._as_date("20240102") == date(2024, 1, 2)
    assert dt._as_date("2024-01-02") == date(2024, 1, 2)
    assert dt._as_date(date(2024, 1, 2)) == date(2024, 1, 2)


def test_fetch_dragon_tiger_rows_and_dedup(monkeypatch):
    items = [{
        "SECURITY_CODE": "600000", "SECURITY_NAME_ABBR": "浦发银行",
        "CLOSE_PRICE": "10.5", "CHANGE_RATE": "7.1", "BILLBOARD_NET_AMT": "1e7",
        "BILLBOARD_BUY_AMT": "2e7", "BILLBOARD_SELL_AMT": "1e7",
        "EXPLANATION": "日涨幅偏离值达7%的证券",
    }, {
        "SECURITY_CODE": "600000", "SECURITY_NAME_ABBR": "浦发银行",
        "CLOSE_PRICE": "10.5", "CHANGE_RATE": "7.1", "BILLBOARD_NET_AMT": "5",
        "BILLBOARD_BUY_AMT": "9", "BILLBOARD_SELL_AMT": "4",
        "EXPLANATION": "另一原因",
    }, {
        "SECURITY_CODE": "000001", "SECURITY_NAME_ABBR": "平安银行",
        "CLOSE_PRICE": None, "CHANGE_RATE": "", "BILLBOARD_NET_AMT": "-",
        "BILLBOARD_BUY_AMT": None, "BILLBOARD_SELL_AMT": "",
        "EXPLANATION": None,
    }]
    monkeypatch.setattr(dt, "em_get", lambda url: _Resp(
        {"result": {"data": items}}))
    df = dt.fetch_dragon_tiger("2024-01-02")
    assert df["symbol"].to_list() == ["600000.SH", "000001.SZ"]
    assert df["net_buy"][1] == 0.0  # "-" → 0.0


def test_fetch_dragon_tiger_empty(monkeypatch):
    monkeypatch.setattr(dt, "em_get", lambda url: _Resp({}))
    df = dt.fetch_dragon_tiger("2024-01-02")
    assert len(df) == 0 and "net_buy" in df.columns


def test_dragon_tiger_demo():
    df = dt.fetch_dragon_tiger("2024-01-02", demo=True)
    assert len(df) == 12
    assert df["net_buy"].is_sorted(descending=True)
