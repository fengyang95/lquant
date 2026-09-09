"""TushareProvider 测试：tushare SDK 模块一律 mock，不触网。

走仓库真实 config/schema/*.yaml 验证声明式映射交付物；
build_chain 门控测试 monkeypatch os.environ。
"""
from __future__ import annotations

import sys
import types
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
import polars as pl
import pytest

import lquant.data.providers as pv  # noqa: PLC0415
from lquant.core.errors import SourceUnavailable
from lquant.data.capability import Capability
from lquant.data.mapping import load_table_mapping
from lquant.data.providers.tushare import TushareProvider
from lquant.data.schema import SCHEMAS

# pro.daily 返回形态：vol 手、amount 千元、trade_date YYYYMMDD
_DAILY = pd.DataFrame({
    "ts_code": ["600000.SH", "600000.SH"],
    "trade_date": ["20240102", "20240103"],
    "open": [10.0, 10.2],
    "high": [10.5, 10.6],
    "low": [9.8, 10.0],
    "close": [10.2, 10.4],
    "pre_close": [10.1, 10.2],
    "vol": [1000, 1100],          # 手
    "amount": [10000.0, 11000.0],  # 千元
})

# pro.stk_mins 返回形态
_MINUTE = pd.DataFrame({
    "ts_code": ["600000.SH", "600000.SH"],
    "trade_time": ["2024-01-02 09:35:00", "2024-01-02 09:40:00"],
    "open": [10.0, 10.2],
    "high": [10.5, 10.6],
    "low": [9.8, 10.0],
    "close": [10.2, 10.4],
    "vol": [100, 110],             # 手
    "amount": [10000.0, 11000.0],  # 千元
})

_ADJ = pd.DataFrame({
    "ts_code": ["600000.SH", "600000.SH"],
    "trade_date": ["20240102", "20240103"],
    "adj_factor": [1.5, 1.5],
})

# income 宽表：第二行无 ann_date → 整行丢弃
_INCOME = pd.DataFrame({
    "ts_code": ["600000.SH", "600000.SH"],
    "end_date": ["20231231", "20240331"],
    "ann_date": ["20240420", None],
    "revenue": [100.0, 200.0],
    "n_income": [50.0, 80.0],
})

_CAL = pd.DataFrame({
    "exchange": ["SSE", "SSE", "SSE"],
    "cal_date": ["20240101", "20240102", "20240103"],
    "is_open": [0, 1, 1],
})

_BASIC = pd.DataFrame({
    "ts_code": ["600000.SH", "000001.SZ"],
    "name": ["浦发银行", "平安银行"],
    "list_date": ["19991110", "19910403"],
    "delist_date": [None, None],
    "list_status": ["L", "L"],
})


class _FakePro:
    """假 pro_api 客户端：按 api 名捕获参数并返回预设 pandas 表。"""

    def __init__(self, fns: dict[str, Callable[..., pd.DataFrame]]) -> None:
        self._fns = fns
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, api: str) -> Callable[..., pd.DataFrame]:
        def fn(**kw: Any) -> pd.DataFrame:
            self.calls.append((api, kw))
            return self._fns[api](**kw)
        return fn


def _install_fake_ts(
    monkeypatch: pytest.MonkeyPatch,
    fns: dict[str, Callable[..., pd.DataFrame]],
) -> _FakePro:
    """把假 tushare 模块塞进 sys.modules（provider 方法内延迟 import）。"""
    pro = _FakePro(fns)
    mod = types.ModuleType("tushare")
    mod.pro_api = lambda token=None: pro  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tushare", mod)
    return pro


@pytest.fixture
def provider() -> TushareProvider:
    return TushareProvider(token="fake-token")


def test_daily_mapping_via_engine(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ts(monkeypatch, {"daily": lambda **kw: _DAILY})
    out = provider.daily_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert out.columns == list(SCHEMAS["daily_bar"])
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["trade_date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert out["open"].to_list() == [10.0, 10.2]
    assert out["volume"].to_list() == [100000.0, 110000.0]   # 手 → 股
    assert out["amount"].to_list() == [10_000_000.0, 11_000_000.0]  # 千元 → 元
    assert out["pre_close"].to_list() == [10.1, 10.2]        # 直出
    assert out["sec_type"].to_list() == ["stock", "stock"]
    assert out["source"].to_list() == ["tushare", "tushare"]
    assert out["quality_flags"].to_list() == [0, 0]
    assert out["adj_factor"].to_list() == [1.0, 1.0]


def test_daily_calls_sdk_with_right_args(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = _install_fake_ts(monkeypatch, {"daily": lambda **kw: _DAILY})
    provider.daily_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    api, kw = pro.calls[0]
    assert api == "daily"
    assert kw["ts_code"] == "600000.SH"
    assert kw["start_date"] == "20240101"
    assert kw["end_date"] == "20240103"


def test_minute_freq_translation_and_units(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = _install_fake_ts(monkeypatch, {"stk_mins": lambda **kw: _MINUTE})
    out = provider.minute_bars(
        ["600000.SH"], date(2024, 1, 1), date(2024, 1, 3), freq="5min"
    )
    api, kw = pro.calls[0]
    assert api == "stk_mins"
    assert kw["freq"] == "5min"   # freq 直接翻译给 SDK
    assert out.columns == list(SCHEMAS["minute_bar"])
    assert out["freq"].to_list() == ["5min", "5min"]  # params 覆盖 yaml fill 1min
    assert out["ts"].dt.hour().to_list() == [9, 9]
    assert out["ts"].dt.minute().to_list() == [35, 40]
    assert out["volume"].to_list() == [10000.0, 11000.0]   # 手 → 股
    assert out["amount"].to_list() == [10_000_000.0, 11_000_000.0]  # 千元 → 元
    assert out["adj_factor"].to_list() == [1.0, 1.0]
    assert out["ingested_at"].dtype == pl.Datetime


def test_minute_bad_freq_raises_before_network(provider: TushareProvider) -> None:
    with pytest.raises(ValueError, match="不支持"):
        provider.minute_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), "7min")


def test_minute_permission_error_becomes_source_unavailable(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stk_mins 2000 积分门槛：权限类报错 → SourceUnavailable（可切源）。"""
    def no_permission(**kw: Any) -> pd.DataFrame:
        raise Exception("抱歉，您没有接口访问权限，积分不足")  # noqa: TRY002

    _install_fake_ts(monkeypatch, {"stk_mins": no_permission})
    with pytest.raises(SourceUnavailable, match="权限"):
        provider.minute_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 2))


def test_adj_factor_direct(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ts(monkeypatch, {"adj_factor": lambda **kw: _ADJ})
    out = provider.adj_factors(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert out["factor"].to_list() == [1.5, 1.5]   # 直出，无相除
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["source"].to_list() == ["tushare", "tushare"]
    assert out["trade_date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]


def test_financial_pit_wide_to_long(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ts(monkeypatch, {
        "income": lambda **kw: _INCOME,
        "balancesheet": lambda **kw: pd.DataFrame(),
        "cashflow": lambda **kw: pd.DataFrame(),
    })
    out = provider.financial_pit(["600000.SH"], date(2024, 1, 1), date(2024, 6, 30))
    assert out.columns == list(SCHEMAS["financial_pit"])
    # 第一行拆 2 个 item；第二行无 ann_date 整行丢弃
    assert out.height == 2
    assert out["item"].to_list() == ["income.revenue", "income.n_income"]
    assert out["value"].to_list() == [100.0, 50.0]
    assert out["stat_date"].to_list() == [date(2023, 12, 31)] * 2
    assert out["pub_date"].to_list() == [date(2024, 4, 20)] * 2
    assert out["report_type"].to_list() == ["2023Q4", "2023Q4"]
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["source"].to_list() == ["tushare", "tushare"]


def test_trade_calendar_mapping(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = _install_fake_ts(monkeypatch, {"trade_cal": lambda **kw: _CAL})
    out = provider.trade_calendar(date(2024, 1, 1), date(2024, 1, 3))
    api, kw = pro.calls[0]
    assert api == "trade_cal"
    assert kw["exchange"] == "SSE"
    assert out["trade_date"].to_list() == [
        date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3),
    ]
    assert out["is_open"].to_list() == [False, True, True]  # is_open=0 → False
    assert out["exchange"].to_list() == ["SSE"] * 3


def test_securities_includes_delisted(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = _install_fake_ts(monkeypatch, {
        "stock_basic": lambda **kw: _BASIC if kw.get("list_status") == "L" else pd.DataFrame(),
    })
    out = provider.securities()
    statuses = [c[1]["list_status"] for c in pro.calls]  # L/D/P 全拉，含退市
    assert statuses == ["L", "D", "P"]
    assert out.columns == ["symbol", "name", "sec_type", "board", "is_st",
                           "list_date", "delist_date", "source"]
    assert out["symbol"].to_list() == ["600000.SH", "000001.SZ"]
    assert out["list_date"].to_list() == [date(1999, 11, 10), date(1991, 4, 3)]
    assert out["source"].to_list() == ["tushare", "tushare"]


def test_etf_daily_sec_type_etf(
    provider: TushareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = _install_fake_ts(monkeypatch, {"fund_daily": lambda **kw: _DAILY})
    out = provider.etf_daily_bars(["510300.SH"], date(2024, 1, 1), date(2024, 1, 3))
    api, kw = pro.calls[0]
    assert api == "fund_daily"
    assert kw["ts_code"] == "510300.SH"
    assert out.columns == list(SCHEMAS["daily_bar"])
    assert out["sec_type"].to_list() == ["etf", "etf"]
    assert out["volume"].to_list() == [100000.0, 110000.0]
    assert out["source"].to_list() == ["tushare", "tushare"]


def test_capability_set(provider: TushareProvider) -> None:
    caps = {c.value for c in TushareProvider(token="t").capability}
    assert caps == {
        "daily", "minute_1", "minute_5", "minute_15", "minute_30", "minute_60",
        "adj_factor", "financial_pit", "reference", "calendar", "etf_daily",
    }
    assert TushareProvider(token="t").has(Capability.FINANCIAL_PIT)
    assert TushareProvider(token="t").has(Capability.CALENDAR)


def test_real_yaml_loads() -> None:
    dm = load_table_mapping("daily_bar", "tushare")
    assert dm.rename["ts_code"] == "symbol"
    assert "vol * 100" in dm.derive["volume"].expr
    assert "amount * 1000" in dm.derive["amount"].expr
    assert dm.fill["source"] == "tushare"
    mm = load_table_mapping("minute_bar", "tushare")
    assert "vol * 100" in mm.derive["volume"].expr
    assert mm.fill["freq"] == "1min"


# --------------------------------------------------------------- env 门控
def test_build_chain_skips_tushare_without_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """env_key 声明的环境变量缺失 → build_chain 跳过该源（warning，不抛错）。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    pv.reset_chain()
    with caplog.at_level("WARNING"):
        chain = pv.build_chain()
    names = [p.name for p in chain.providers]
    assert "tushare" not in names
    assert "baostock" in names          # 其余源正常构建，链非空
    assert any("TUSHARE_TOKEN" in r.message for r in caplog.records)
    pv.reset_chain()


def test_build_chain_includes_tushare_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "fake")
    pv.reset_chain()
    chain = pv.build_chain()
    names = [p.name for p in chain.providers]
    assert "tushare" in names
    pv.reset_chain()
