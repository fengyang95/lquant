"""腾讯行情适配器：解析器离线单测 + Provider 方法打桩（网络薄壳不测）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.core.errors import CapabilityMissing
from lquant.data.capability import Capability
from lquant.data.providers import tencent as tencent_mod
from lquant.data.providers.tencent import (
    TencentProvider,
    _gtimg_code,
    _to_float,
    _to_num,
    parse_quotes,
)


def test_to_float_and_num() -> None:
    assert _to_float("") is None
    assert _to_float("-") is None
    assert _to_float("0.00") == 0.0      # _to_float 保留 0，归一在 parse 阶段不处理
    assert _to_float("abc") is None
    assert _to_num("0.5") == 0.5
    assert _to_num("-") is None
    assert _to_num("x") is None


def test_gtimg_code() -> None:
    assert _gtimg_code("600519.SH") == "sh600519"
    assert _gtimg_code("000001.SZ") == "sz000001"
    assert _gtimg_code("830799.BJ") == "bj830799"


_PAYLOAD = (
    'v_sh600519="1~贵州茅台~600519~1700.00~1690.00~1695.00~12345~'
    '~~1271.00~1280.00~~" ;\n'
    'v_sz000001="1~平安银行~000001~~~~" ;\n'   # 无价格 → 整行跳过
    'v_sh600000="1~浦发银行~600000~10.00~9.90~9.95~100~' + "~" * 23 +
    '12020101010101' + "~" * 7 + '" ;'          # ts 在下标 30
    'v_sh600036="1~招商银行~600036~30.00~29.90~29.95~100~' + "~" * 23 +
    'not-a-ts' + "~" * 7 + '" ;'                # ts 非法 → None
)


def test_parse_quotes_full() -> None:
    df = parse_quotes(_PAYLOAD)
    assert set(df["symbol"].to_list()) == {"600519.SH", "600000.SH", "600036.SH"}
    row = df.filter(pl.col("symbol") == "600519.SH").row(0, named=True)
    assert row["name"] == "贵州茅台"
    assert row["last"] == 1700.0
    assert row["pre_close"] == 1690.0
    assert row["open"] == 1695.0
    assert row["volume"] == 12345 * 100.0
    assert row["source"] == "tencent"
    assert row["is_stale"] is False
    # 短行情：缺字段 → None
    assert row["high"] is None


def test_parse_quotes_ts_and_stale_fields() -> None:
    df = parse_quotes(_PAYLOAD)
    ok = df.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    bad = df.filter(pl.col("symbol") == "600036.SH").row(0, named=True)
    assert ok["ts"] is not None and bad["ts"] is None


def test_parse_quotes_empty_payload() -> None:
    df = parse_quotes("nothing here")
    assert isinstance(df, pl.DataFrame) and df.is_empty()


def test_realtime_empty_symbols_short_circuit() -> None:
    assert TencentProvider().realtime([]).is_empty()


def test_realtime_uses_fetch(monkeypatch) -> None:
    monkeypatch.setattr(tencent_mod, "_fetch_quotes",
                        lambda codes: _PAYLOAD)
    df = TencentProvider().realtime(["600519.SH", "000001.SZ"])
    assert len(df) == 3


def test_securities_selects_index_columns(monkeypatch) -> None:
    monkeypatch.setattr(tencent_mod, "_fetch_quotes",
                        lambda codes: _PAYLOAD)
    df = TencentProvider().securities()
    assert set(df["symbol"].to_list()) == {"600519.SH", "600000.SH", "600036.SH"}
    assert (df["sec_type"] == "index").all()
    assert not df["is_st"].any()


def test_securities_empty_payload(monkeypatch) -> None:
    monkeypatch.setattr(tencent_mod, "_fetch_quotes", lambda codes: "x")
    assert TencentProvider().securities().is_empty()


def test_unsupported_methods_raise_capability_missing() -> None:
    p = TencentProvider()
    with pytest.raises(CapabilityMissing):
        p.trade_calendar(date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(CapabilityMissing):
        p.daily_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(CapabilityMissing):
        p.minute_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2), "1min")
    with pytest.raises(CapabilityMissing):
        p.adj_factors(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(CapabilityMissing):
        p.financial_pit(["600519.SH"], date(2026, 1, 1), date(2026, 1, 2))
    assert Capability.REALTIME in p.capability


def test_health_true_and_false(monkeypatch) -> None:
    p = TencentProvider()
    monkeypatch.setattr(tencent_mod, "_fetch_quotes",
                        lambda codes: 'v_sh600519="1~茅台~600519~1700.00~~;" ;')
    assert p.health() is True
    monkeypatch.setattr(tencent_mod, "_fetch_quotes",
                        lambda codes: "v_sh600519=\"1~x~600519~~~~\";")
    assert p.health() is False


def test_health_network_error_returns_false(monkeypatch) -> None:
    def boom(codes):
        raise OSError("断网")

    monkeypatch.setattr(tencent_mod, "_fetch_quotes", boom)
    assert TencentProvider().health() is False
