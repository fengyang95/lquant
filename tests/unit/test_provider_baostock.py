"""BaoStockProvider 迁移到 MappingProvider 引擎后的回归测试。

全部通过 _raw 直传 / monkeypatch watchdog，不触网。
使用仓库真实 config/schema/*.yaml（迁移交付物本身）。
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

import lquant.data.watchdog as wd
from lquant.core.errors import DataQualityError
from lquant.data.mapping import load_table_mapping
from lquant.data.providers.baostock import (
    BaoStockProvider,
    _attach_is_st,
    _map_daily_raw,
)
from lquant.data.schema import SCHEMAS

DAILY_ROWS = [
    # date, code, open, high, low, close, preclose, volume, amount(元), turn,
    # tradestatus, isST, pctChg, peTTM, pbMRQ, psTTM, pcfNcfTTM
    [
        "2024-01-02",
        "sh.600000",
        "10.0",
        "10.5",
        "9.8",
        "10.2",
        "10.1",
        "1000",
        "3000",
        "1.5",
        "1",
        "0",
        "0.5",
        "12.0",
        "1.2",
        "2.0",
        "8.0",
    ],
    [
        "2024-01-03",
        "sh.600000",
        "10.2",
        "10.6",
        "10.0",
        "10.4",
        "10.2",
        "1100",
        "3300",
        "1.6",
        "1",
        "1",
        "-0.2",
        "12.4",
        "1.3",
        "2.1",
        "8.2",
    ],
    [
        "2024-01-04",
        "sh.600000",
        "10.4",
        "10.8",
        "10.2",
        "10.6",
        "10.4",
        "0",
        "0",
        "0",
        "0",
        "0",
        "",
        "",
        "",
        "",
        "",
    ],  # 停牌行，保留 is_suspended=True
]


@pytest.fixture
def provider() -> BaoStockProvider:
    return BaoStockProvider()


def _daily_raw() -> pl.DataFrame:
    """模拟 _map_daily_raw 输出：源列名（含 tradestatus/isST），停牌行保留。"""
    return _map_daily_raw(DAILY_ROWS)


def test_daily_mapping_via_engine(provider: BaoStockProvider) -> None:
    raw = _daily_raw()
    out = provider.request("daily_bar", _raw=raw)
    out = _attach_is_st(out, raw)
    assert out.columns == list(SCHEMAS["daily_bar"])
    assert out["symbol"].to_list() == ["600000.SH"] * 3
    assert out["trade_date"].dtype == pl.Date
    assert out["amount"].to_list() == [3000.0, 3300.0, 0.0]  # 已是元，直接透传
    assert out["pre_close"].to_list() == [10.1, 10.2, 10.4]
    assert out["turnover_rate"].to_list()[:2] == [1.5, 1.6]
    assert out["turnover_rate"][2] is None  # 停牌行 turn=0 → null
    assert out["sec_type"].to_list() == ["stock"] * 3
    assert out["source"].to_list() == ["baostock"] * 3
    assert out["quality_flags"].to_list() == [0, 0, 0]
    assert out["adj_factor"].to_list() == [1.0, 1.0, 1.0]
    assert out["is_st"].to_list() == [False, True, False]
    assert out["is_suspended"].to_list() == [False, False, True]
    assert out["pct_chg"].to_list()[:2] == [0.5, -0.2]
    assert out["pe_ttm"].to_list()[:2] == [12.0, 12.4]
    assert out["pe_ttm"][2] is None
    assert out["float_mv"][2] is None
    assert out["total_mv"].is_null().all()
    assert out["ingested_at"].is_null().all()
    assert out["data_version"].is_null().all()


def test_attach_is_st_without_column(provider: BaoStockProvider) -> None:
    """raw 无 is_st（如 _raw 直传外部数据）→ 输出 is_st 全 null，不依赖实例状态。"""
    raw = _daily_raw().drop("is_st")
    out = provider.request("daily_bar", _raw=raw)
    out = _attach_is_st(out, raw)
    assert "is_st" in out.columns
    assert out["is_st"].is_null().all()
    assert out["is_st"].dtype == pl.Boolean


def test_attach_is_st_length_mismatch_raises() -> None:
    raw = _daily_raw()
    out = pl.DataFrame({"x": [1.0]})  # 长度 1 != raw 长度 3
    with pytest.raises(DataQualityError, match="is_st"):
        _attach_is_st(out, raw)


def test_daily_fetch_keeps_suspended_and_converts_is_st(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_daily：停牌行保留（is_suspended=True），watchdog 打桩不触网。"""
    captured: dict[str, object] = {}

    def fake_query(fn: object, *args: object, **kw: object) -> list[list[str]]:
        captured["code"] = args[0]
        return DAILY_ROWS

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    raw = p._fetch_daily(["600000.SH"], date(2024, 1, 1), date(2024, 1, 4))
    assert captured["code"] == "sh.600000"
    assert len(raw) == 3  # 停牌行保留
    assert raw["is_suspended"].to_list() == [False, False, True]
    assert "tradestatus" not in raw.columns and "isST" not in raw.columns


def test_minute_mapping_via_engine(provider: BaoStockProvider) -> None:
    raw = pl.DataFrame(
        {
            "code": ["sh.600000", "sh.600000", "sh.600000"],
            "ts": [
                datetime(2024, 1, 2, 10, 30),
                datetime(2024, 1, 2, 11, 30),
                datetime(2024, 1, 2, 14, 0),
            ],
            "open": [10.0, 10.2, 10.4],
            "high": [10.5, 10.6, 10.8],
            "low": [9.8, 10.0, 10.2],
            "close": [10.2, 10.4, 10.6],
            "volume": [1000.0, 1100.0, 1200.0],
            "amount": [10000.0, 11000.0, 12000.0],  # 已是元，不换算
        }
    )
    out = provider.request("minute_bar", _raw=raw, freq="60min")
    assert out.columns[:12] == list(SCHEMAS["minute_bar"])
    assert out["symbol"].to_list() == ["600000.SH"] * 3
    assert out["freq"].to_list() == ["60min"] * 3  # params 覆盖 yaml fill 的 5min
    assert out["amount"].to_list() == [10000.0, 11000.0, 12000.0]
    assert out["adj_factor"].to_list() == [1.0, 1.0, 1.0]
    assert out["source"].to_list() == ["baostock"] * 3
    assert out["ingested_at"].dtype == pl.Datetime
    assert not out["ingested_at"].is_null().any()
    # 60min 边界归一：11:30 → 11:00，14:00 保持
    assert out["ts"].dt.hour().to_list() == [10, 11, 14]
    assert out["ts"].dt.minute().to_list() == [30, 0, 0]


def test_minute_5min_no_boundary_shift(provider: BaoStockProvider) -> None:
    """非 60min 的 freq 不做边界归一。"""
    raw = pl.DataFrame(
        {
            "code": ["sh.600000"],
            "ts": [datetime(2024, 1, 2, 11, 30)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [1000.0],
            "amount": [10000.0],
        }
    )
    out = provider.request("minute_bar", _raw=raw, freq="5min")
    assert out["freq"].to_list() == ["5min"]
    assert out["ts"].dt.hour().to_list() == [11]
    assert out["ts"].dt.minute().to_list() == [30]


def test_minute_fetch_parses_time_and_slices_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_minute：time(YYYYmmddHHMMSSmmm) → ts，按年切片。"""
    calls: list[str] = []

    def fake_query(fn: object, *args: object) -> list[list[str]]:
        code, fields, start, end, freq, adj = args
        calls.append(f"{code}|{start}~{end}|{freq}")
        if start.startswith("2023"):
            return []
        return [
            [
                "2024-01-02",
                "20240102113000000",
                code,
                "10.0",
                "10.5",
                "9.8",
                "10.2",
                "1000",
                "10000",
                "3",
            ],
            [
                "2024-01-02",
                "20240102140000000",
                code,
                "10.2",
                "10.6",
                "10.0",
                "10.4",
                "1100",
                "11000",
                "3",
            ],
        ]

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    raw = p._fetch_minute(["600000.SH"], date(2023, 11, 1), date(2024, 2, 1), "60min")
    assert calls == [
        "sh.600000|2023-11-01~2023-12-31|60min",
        "sh.600000|2024-01-01~2024-02-01|60min",
    ]
    assert raw["ts"].dt.hour().to_list() == [11, 14]
    assert raw["ts"].dt.minute().to_list() == [30, 0]
    assert raw["close"].dtype == pl.Float64
    assert "time" not in raw.columns and "date" not in raw.columns


def test_minute_bad_freq_raises_before_network() -> None:
    p = BaoStockProvider()
    with pytest.raises(ValueError, match="60min|不支持"):
        p._fetch_minute(["600000.SH"], date(2024, 1, 1), date(2024, 1, 2), "7min")


def test_empty_raw_short_circuits(provider: BaoStockProvider) -> None:
    """零列/零行 raw → engine 短路返回 schema 形状空表（旧行为是空 df，不炸）。"""
    for bad in (pl.DataFrame(), _daily_raw().clear()):
        out = provider.request("daily_bar", _raw=bad)
        assert out.columns == list(SCHEMAS["daily_bar"])
        assert out.height == 0
        out_m = provider.request("minute_bar", _raw=pl.DataFrame(), freq="60min")
        assert out_m.columns == list(SCHEMAS["minute_bar"])


def test_daily_bars_end_to_end_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daily_bars 全链路（watchdog 打桩）：映射层出 is_st，停牌行保留。"""
    monkeypatch.setattr(wd, "run_with_watchdog", lambda fn, *a, **k: DAILY_ROWS)
    p = BaoStockProvider()
    out = p.daily_bars(["600000.SH"], date(2024, 1, 1), date(2024, 1, 4))
    assert out["is_st"].to_list() == [False, True, False]
    assert out["is_suspended"].to_list() == [False, False, True]
    assert out["amount"].to_list() == [3000.0, 3300.0, 0.0]  # 已是元，直接透传
    assert out["symbol"].to_list() == ["600000.SH"] * 3


def test_trade_calendar_long_range_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    """>5 年的日历请求必须分段 —— baostock 对 >10 年区间会静默挂起。"""
    calls: list[tuple[str, str]] = []

    def fake_query(fn: object, *args: object) -> list[list[str]]:
        start, end = args
        calls.append((str(start), str(end)))
        return [[str(start), "1"]]

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    out = p.trade_calendar(date(1990, 12, 19), date(2035, 12, 31))
    spans = [(date.fromisoformat(s), date.fromisoformat(e)) for s, e in calls]
    assert len(spans) >= 9  # 45 年 ÷ 5 年
    assert all((e - s).days <= 366 * 5 for s, e in spans), spans
    assert spans[0][0] == date(1990, 12, 19)
    assert spans[-1][1] == date(2035, 12, 31)
    # 相邻段无缝衔接
    assert all(spans[i + 1][0] > spans[i][1] for i in range(len(spans) - 1))
    assert len(out) == len(spans)


def test_trade_calendar_short_range_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_query(fn: object, *args: object) -> list[list[str]]:
        calls.append((str(args[0]), str(args[1])))
        return [[str(args[0]), "1"]]

    monkeypatch.setattr(wd, "run_with_watchdog", fake_query)
    p = BaoStockProvider()
    out = p.trade_calendar(date(2024, 1, 1), date(2024, 12, 31))
    assert len(calls) == 1
    assert len(out) == 1


def test_real_yaml_loads() -> None:
    """交付的两份 yaml 与 SCHEMAS 校验兼容（fail-fast 不炸）。"""
    dm = load_table_mapping("daily_bar", "baostock")
    assert dm.rename == {
        "date": "trade_date",
        "code": "symbol",
        "preclose": "pre_close",
        "turn": "turnover_rate",
        "pctChg": "pct_chg",
        "peTTM": "pe_ttm",
        "pbMRQ": "pb_mrq",
        "psTTM": "ps_ttm",
        "pcfNcfTTM": "pcf_ncf_ttm",
    }
    assert "amount" not in dm.derive  # baostock 日线 amount 单位是元，无换算
    assert "float_mv" in dm.derive  # 流通市值推导（close×volume×100/turn）
    mm = load_table_mapping("minute_bar", "baostock")
    assert mm.fill == {"freq": "5min", "source": "baostock", "adj_factor": 1.0}


def test_daily_mapping_empty_volume_not_crash() -> None:
    """回归：指数/停牌行 volume、amount 可能是空串 ""（实测 000001.SH
    2026 年有 5/169 行 volume=""），strict cast 会炸掉整批 → 必须落 null。"""
    rows = [list(DAILY_ROWS[0])]
    rows[0][7] = ""  # volume
    rows[0][8] = ""  # amount
    df = _map_daily_raw(rows)
    assert df["volume"][0] is None
    assert df["amount"][0] is None
    assert df["close"][0] == 10.2  # 正常字段不受影响


def test_securities_falls_back_on_non_trading_day(monkeypatch) -> None:
    """回归：周末/节假日 query_all_stock(day=今天) 返回空，应回退最近
    交易日重试，而不是静默返回空帧（周六跑 reference 空转）。"""
    from datetime import date

    import lquant.data.watchdog as wd

    calls: list[str] = []

    def fake_all_stock(day: str) -> list[list[str]]:
        calls.append(day)
        if day <= "2026-09-11":  # 周五及以前回数据，周末空
            return [["sh.000001", "1", "上证综合指数"]]
        return []

    monkeypatch.setattr(wd, "run_with_watchdog", lambda fn, *a, **k: fake_all_stock(*a))
    monkeypatch.setattr("lquant.data.providers.baostock.today_cn", lambda: date(2026, 9, 12))
    df = BaoStockProvider().securities()
    assert len(df) == 1
    assert df["symbol"][0] == "000001.SH"
    # 今天空 → 回退 09-11（周五）拿到数据后停止
    assert calls[-1] == "2026-09-11"


def _fake_query_2024(code: str, *a, **k) -> list[list[str]]:
    return [
        [
            "2024-01-02",
            code,
            "10.0",
            "10.5",
            "9.8",
            "10.2",
            "10.1",
            "1000",
            "3000",
            "1.5",
            "1",
            "0",
            "0.5",
            "12.0",
            "1.2",
            "2.0",
            "8.0",
        ]
    ]


def test_fetch_daily_parallel_preserves_order(monkeypatch) -> None:
    """线程池并行拉取后必须按入参顺序拼接（顺序影响对拍与断点语义）。"""
    import datetime
    import sys

    def _passthrough(fn, *a, **k):
        return fn(*a, **k)

    # 直接 patch 方法实际读取的对象，不走 monkeypatch 字符串解析：
    # test_provider_akshare 的模块重导入会让 import 图分裂，字符串
    # target 经 getattr 链可能 resolve 到另一个模块对象，patch 落不到
    # _one 闭包真正读取的 __globals__ → 全量套件下偶发走真登录。
    g = BaoStockProvider._fetch_daily.__globals__
    monkeypatch.setitem(g, "_bs_query", _fake_query_2024)
    monkeypatch.setattr(sys.modules["lquant.data.watchdog"], "run_with_watchdog", _passthrough)
    syms = [f"60000{i}.SH" for i in range(10)]
    df = BaoStockProvider()._fetch_daily(
        syms, datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )
    # _fetch_daily 产出源列名（code，baostock 前缀式），symbol 由映射引擎转换
    assert df["code"].to_list() == [f"sh.{600000 + i}" for i in range(10)]
