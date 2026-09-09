"""AkShareProvider 测试：akshare SDK 模块一律 mock，不触网。

走仓库真实 config/schema/*.yaml 验证声明式映射交付物。
"""
from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
import polars as pl
import pytest

from lquant.data.capability import Capability
from lquant.data.mapping import load_table_mapping
from lquant.data.providers.akshare import AkShareProvider
from lquant.data.schema import SCHEMAS

# stock_zh_a_hist 返回的中文列（em 接口实际形态）
_DAILY_PANDAS = pd.DataFrame({
    "日期": ["2024-01-02", "2024-01-03"],
    "股票代码": ["600000", "600000"],
    "开盘": [10.0, 10.2],
    "收盘": [10.2, 10.4],
    "最高": [10.5, 10.6],
    "最低": [9.8, 10.0],
    "成交量": [1000, 1100],        # 手
    "成交额": [10000.0, 11000.0],  # 元
})

# stock_zh_a_hist_min_em 返回形态：时间列字符串
_MINUTE_PANDAS = pd.DataFrame({
    "时间": ["2024-01-02 09:35:00", "2024-01-02 09:40:00"],
    "股票代码": ["600000", "600000"],
    "开盘": [10.0, 10.2],
    "收盘": [10.2, 10.4],
    "最高": [10.5, 10.6],
    "最低": [9.8, 10.0],
    "成交量": [100, 110],          # 手
    "成交额": [10000.0, 11000.0],  # 元
})


@pytest.fixture
def provider() -> AkShareProvider:
    return AkShareProvider()


def _install_fake_ak(
    monkeypatch: pytest.MonkeyPatch,
    **fns: Callable[..., pd.DataFrame],
) -> None:
    """把假 akshare 模块塞进 sys.modules（provider 方法内延迟 import）。"""
    mod = types.ModuleType("akshare")
    for name, fn in fns.items():
        setattr(mod, name, fn)
    monkeypatch.setitem(sys.modules, "akshare", mod)


def test_daily_mapping_via_engine(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ak(
        monkeypatch,
        stock_zh_a_hist=lambda **kw: _DAILY_PANDAS,
    )
    out = provider.daily_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert out.columns == list(SCHEMAS["daily_bar"])
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["trade_date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert out["open"].to_list() == [10.0, 10.2]
    assert out["volume"].to_list() == [100000.0, 110000.0]  # 手 → 股
    assert out["amount"].to_list() == [10000.0, 11000.0]    # 元直传
    assert out["sec_type"].to_list() == ["stock", "stock"]
    assert out["source"].to_list() == ["akshare", "akshare"]
    assert out["quality_flags"].to_list() == [0, 0]
    assert out["adj_factor"].to_list() == [1.0, 1.0]
    assert out["pre_close"].is_null().all()   # akshare 日线无 preclose
    assert out["turnover_rate"].is_null().all()


def test_daily_calls_sdk_with_right_args(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_hist(**kw: Any) -> pd.DataFrame:
        captured.update(kw)
        return _DAILY_PANDAS

    _install_fake_ak(monkeypatch, stock_zh_a_hist=fake_hist)
    provider.daily_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert captured["symbol"] == "600000"
    assert captured["period"] == "daily"
    assert captured["start_date"] == "20240101"
    assert captured["end_date"] == "20240103"
    assert captured["adjust"] == ""


def test_minute_freq_injection_and_ts_parse(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_min(**kw: Any) -> pd.DataFrame:
        captured.update(kw)
        return _MINUTE_PANDAS

    _install_fake_ak(monkeypatch, stock_zh_a_hist_min_em=fake_min)
    out = provider.minute_bars(
        ["600000.SH"], date(2024, 1, 1), date(2024, 1, 3), freq="5min"
    )
    assert captured["period"] == "5"          # freq → period
    assert out.columns == list(SCHEMAS["minute_bar"])
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["freq"].to_list() == ["5min", "5min"]  # params 覆盖 yaml fill 1min
    # 时间列字符串 → ts（bar 结束时刻语义保持）
    assert out["ts"].dt.hour().to_list() == [9, 9]
    assert out["ts"].dt.minute().to_list() == [35, 40]
    assert out["volume"].to_list() == [10000.0, 11000.0]  # 手 → 股
    assert out["adj_factor"].to_list() == [1.0, 1.0]
    assert out["ingested_at"].dtype == pl.Datetime
    assert not out["ingested_at"].is_null().any()


def test_minute_bad_freq_raises_before_network(provider: AkShareProvider) -> None:
    with pytest.raises(ValueError, match="不支持"):
        provider.minute_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), "7min")


def test_minute_60min_boundary_normalized(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60min 边界归一与 baostock 一致：11:30 → 11:00；非 60min 不受影响。"""
    def fake_min(**kw: Any) -> pd.DataFrame:
        return pd.DataFrame({
            "时间": ["2024-01-02 10:00:00", "2024-01-02 11:30:00",
                     "2024-01-02 14:00:00"],
            "股票代码": ["600000"] * 3,
            "开盘": [10.0, 10.2, 10.4],
            "收盘": [10.1, 10.3, 10.5],
            "最高": [10.2, 10.4, 10.6],
            "最低": [9.9, 10.1, 10.3],
            "成交量": [100, 110, 120],
            "成交额": [10000.0, 11000.0, 12000.0],
        })

    _install_fake_ak(monkeypatch, stock_zh_a_hist_min_em=fake_min)
    out = provider.minute_bars(
        ["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), freq="60min"
    )
    # 11:30 → 11:00，10:00 / 14:00 保持
    assert out["ts"].dt.hour().to_list() == [10, 11, 14]
    assert out["ts"].dt.minute().to_list() == [0, 0, 0]

    out5 = provider.minute_bars(
        ["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), freq="5min"
    )
    # 非 60min 不做边界归一
    assert out5["ts"].dt.hour().to_list() == [10, 11, 14]
    assert out5["ts"].dt.minute().to_list() == [0, 30, 0]


def test_etf_daily_goes_through_daily_bar_with_sec_type(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_etf(**kw: Any) -> pd.DataFrame:
        captured.update(kw)
        # 还原 fund_etf_hist_em 真实形态：不带「股票代码」列
        return _DAILY_PANDAS.drop(columns=["股票代码"])

    _install_fake_ak(monkeypatch, fund_etf_hist_em=fake_etf)
    out = provider.etf_daily_bars(["510300.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert captured["symbol"] == "510300"
    assert out.columns == list(SCHEMAS["daily_bar"])
    assert out["symbol"].to_list() == ["510300.SH", "510300.SH"]
    assert out["sec_type"].to_list() == ["etf", "etf"]  # params 注入覆盖 fill
    assert out["volume"].to_list() == [100000.0, 110000.0]
    assert out["source"].to_list() == ["akshare", "akshare"]


def test_adj_factors_two_pull_division(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_hist(**kw: Any) -> pd.DataFrame:
        calls.append(kw["adjust"])
        if kw["adjust"] == "hfq":
            return pd.DataFrame({
                "日期": ["2024-01-02", "2024-01-03"],
                "收盘": [20.4, 20.8],   # 后复权（首日除权翻倍）
            })
        return pd.DataFrame({
            "日期": ["2024-01-02", "2024-01-03"],
            "收盘": [10.2, 10.4],
        })

    _install_fake_ak(monkeypatch, stock_zh_a_hist=fake_hist)
    out = provider.adj_factors(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert calls == ["hfq", ""]   # 后复权 / 不复权 两次拉取
    assert out["factor"].to_list() == pytest.approx([2.0, 2.0])   # 后复权 factor ≥ 1
    assert out["symbol"].to_list() == ["600000.SH", "600000.SH"]
    assert out["source"].to_list() == ["akshare", "akshare"]
    assert out["trade_date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]


def test_adj_factors_date_misalignment_no_wrong_factor(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行数相同但日期错开：按 trade_date join，只保留交集日期，因子不错位。"""
    def fake_hist(**kw: Any) -> pd.DataFrame:
        if kw["adjust"] == "hfq":
            return pd.DataFrame({
                "日期": ["2024-01-03", "2024-01-04"],
                "收盘": [10.3, 10.4],
            })
        return pd.DataFrame({
            "日期": ["2024-01-02", "2024-01-03"],
            "收盘": [10.2, 10.3],
        })

    _install_fake_ak(monkeypatch, stock_zh_a_hist=fake_hist)
    out = provider.adj_factors(["600000.SH"], date(2024, 1, 1), date(2024, 1, 4))
    assert out["trade_date"].to_list() == [date(2024, 1, 3)]   # 仅交集
    assert out["factor"].to_list() == pytest.approx([1.0])


def test_adj_factors_partial_overlap_keeps_common_dates(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_hist(**kw: Any) -> pd.DataFrame:
        if kw["adjust"] == "hfq":
            return pd.DataFrame({"日期": ["2024-01-02"], "收盘": [20.4]})
        return pd.DataFrame({"日期": ["2024-01-02", "2024-01-03"], "收盘": [10.2, 10.4]})

    _install_fake_ak(monkeypatch, stock_zh_a_hist=fake_hist)
    out = provider.adj_factors(["600000.SH"], date(2024, 1, 1), date(2024, 1, 3))
    assert out["trade_date"].to_list() == [date(2024, 1, 2)]
    assert out["factor"].to_list() == pytest.approx([2.0])


def test_securities_mapping(
    provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ak(
        monkeypatch,
        stock_info_a_code_name=lambda: pd.DataFrame({
            "code": ["600000", "000001", "510300"],
            "name": ["浦发银行", "平安银行", "沪深300ETF"],
        }),
    )
    out = provider.securities()
    assert out.columns == ["symbol", "name", "sec_type", "board", "is_st",
                           "list_date", "delist_date", "source"]
    assert out["symbol"].to_list() == ["600000.SH", "000001.SZ", "510300.SH"]
    assert out["sec_type"].to_list() == ["stock", "stock", "etf"]
    assert out["board"].to_list() == ["main", "main", "unknown"]  # ETF 无板块概念
    assert out["is_st"].to_list() == [False, False, False]
    assert out["source"].to_list() == ["akshare"] * 3


def test_securities_empty(provider: AkShareProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    def empty_secs() -> pd.DataFrame:
        return pd.DataFrame()

    _install_fake_ak(monkeypatch, stock_info_a_code_name=empty_secs)
    assert provider.securities().height == 0


def test_import_failure_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """akshare provider 模块 import 失败 → _import_all 吞掉不注册。"""
    import lquant.data.providers as pv  # noqa: PLC0415

    sys.modules.pop("lquant.data.providers.akshare", None)
    # baostock 可能已被其他测试文件导入（装饰器不再重跑），一并弹出让其重新注册
    sys.modules.pop("lquant.data.providers.baostock", None)

    real_import = importlib.import_module

    def fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name == "lquant.data.providers.akshare":
            raise ImportError("mocked: akshare unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    # 模拟全新进程：移除已注册条目，只看 _import_all 的注册结果
    monkeypatch.setattr(pv.PROVIDERS, "_items", {})
    monkeypatch.setattr(pv.PROVIDERS, "_meta", {})
    pv._import_all()
    assert "akshare" not in pv.PROVIDERS
    assert "baostock" in pv.PROVIDERS  # 其他 provider 正常注册


def test_capability_set(provider: AkShareProvider) -> None:
    caps = {c.value for c in AkShareProvider().capability}
    assert caps == {
        "daily", "minute_1", "minute_5", "minute_15", "minute_30", "minute_60",
        "adj_factor", "reference", "etf_daily",
    }
    assert not AkShareProvider().has(Capability.FINANCIAL_PIT)
    assert not AkShareProvider().has(Capability.CALENDAR)


def test_real_yaml_loads() -> None:
    dm = load_table_mapping("daily_bar", "akshare")
    assert dm.rename["股票代码"] == "symbol"
    assert dm.rename["日期"] == "trade_date"
    assert "volume" in dm.derive
    assert dm.fill["source"] == "akshare"
    mm = load_table_mapping("minute_bar", "akshare")
    assert mm.fill["freq"] == "1min"
    assert "volume" in mm.derive
