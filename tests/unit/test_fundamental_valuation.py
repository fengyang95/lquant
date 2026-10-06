"""估值取数：日线湖 PIT 选择 + 符号污染守卫。

估值是三块取数来源里最容易出错的一块，因为它的坑**不会报错**：
PE/PB 为负时，反向打分（越低越好）会把亏损股和净资产为负的票排到全市场
最前面 —— 结果看起来像「找到了深度价值股」，实际是纯噪声。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.fundamental import valuation_frame, valuation_long
from lquant.fundamental.valuation import VALUATION_ITEMS, valuation_items

D1 = date(2026, 3, 30)
D2 = date(2026, 3, 31)
AFTER = date(2026, 4, 2)

SYMS = ["600000.SH", "600001.SH", "600002.SH"]


def _write_lake(root: Path, kind: str, frame: pl.DataFrame) -> None:
    d = root / kind / "year=2026"
    d.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(d / "part-0.parquet")


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """铺一份最小日线湖 + daily_basic 湖，并把 CWD 切过去。"""
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    root = tmp_path / "data" / "parquet"
    _write_lake(root, "daily", pl.DataFrame({
        "symbol": SYMS + SYMS,
        "trade_date": [D1] * 3 + [D2] * 3,
        # 600001.SH 在 D2 变成亏损（PE 转负）、净资产转负（PB 转负）
        "pe_ttm": [10.0, 12.0, 15.0, 8.0, -5.0, 20.0],
        "pb_mrq": [1.5, 2.0, 3.0, 1.2, 2.5, -0.8],
    }))
    _write_lake(root, "daily_basic", pl.DataFrame({
        "symbol": SYMS + SYMS,
        "trade_date": [D1] * 3 + [D2] * 3,
        "dv_ttm": [1.0, 2.0, 3.0, 1.1, 2.1, 3.1],
    }))
    yield tmp_path
    get_settings.cache_clear()


def test_uses_latest_bar_not_after_asof(lake):
    """D2 的 bar 在 D1 观察日上不可见 —— 否则就是前视偏差。"""
    at_d1 = valuation_frame(D1)
    row = at_d1.filter(pl.col("symbol") == "600001.SH").row(0, named=True)
    assert row["pe_ttm"] == pytest.approx(12.0)      # D1 的值，不是 D2 的 -5.0

    at_d2 = valuation_frame(D2)
    assert set(at_d2["symbol"].to_list()) == set(SYMS)


def test_negative_pe_and_pb_are_dropped(lake):
    """负 PE/PB = 亏损 / 净资产为负，不是「便宜」，必须置空。

    且**只置空当项**：不回退到更早的 bar，也不影响同一天的其它估值项。
    """
    got = valuation_frame(AFTER).sort("symbol")
    pe_dead = got.filter(pl.col("symbol") == "600001.SH").row(0, named=True)
    assert pe_dead["pe_ttm"] is None                 # D2 是 -5.0 → 置空
    assert pe_dead["pb"] == pytest.approx(2.5)       # 同日 PB 正常，保留

    pb_dead = got.filter(pl.col("symbol") == "600002.SH").row(0, named=True)
    assert pb_dead["pb"] is None                     # D2 是 -0.8 → 置空
    assert pb_dead["pe_ttm"] == pytest.approx(20.0)

    ok = got.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert ok["pe_ttm"] == pytest.approx(8.0)
    assert ok["pb"] == pytest.approx(1.2)


def test_dividend_yield_keeps_zero(lake):
    """股息率 0（不分红）是有效信息，不能当缺失丢掉。"""
    frame = valuation_frame(AFTER)
    assert frame.filter(pl.col("symbol") == "600002.SH")["dividend_yield"][0] == pytest.approx(3.1)


def test_long_shape_matches_financial_pit(lake):
    """长表形状必须能和 financial_pit 直接 concat 后交给 resolve_pit。"""
    long = valuation_long(AFTER)
    assert set(long.columns) == {"symbol", "stat_date", "pub_date", "item", "value"}
    assert set(long["item"].unique().to_list()) == set(VALUATION_ITEMS)
    # stat_date == pub_date == 该 bar 的交易日（日频量当天收盘可知）
    assert long.filter(pl.col("item") == "valuation.pe_ttm")["stat_date"].unique() \
        .to_list() == [D2]
    # 用字符串比而不是 `== pl.Date`：在 `--cov=<模块路径>` 下 polars 的
    # DataTypeClass.__eq__ 会退化成 False（`pl.Date == pl.Date` 都为假，
    # 与本仓库代码无关，可脱离本项目复现）。断言意图是「是日期列而不是字符串列」，
    # 字符串比同样成立且不受该 quirk 影响。
    assert str(long.schema["stat_date"]) == "Date"
    assert str(long.schema["value"]) == "Float64"


def test_symbol_filter(lake):
    got = valuation_long(AFTER, symbols=["600000.SH"])
    assert set(got["symbol"].unique().to_list()) == {"600000.SH"}


def test_empty_lake_returns_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert valuation_long(AFTER).is_empty()
        assert valuation_frame(AFTER).is_empty()
    finally:
        get_settings.cache_clear()


def test_valuation_items_are_logical_keys():
    assert valuation_items() == tuple(VALUATION_ITEMS)
    assert all(k.startswith("valuation.") for k in VALUATION_ITEMS)


def test_valuation_lake_kinds_are_declared():
    """每个估值列都要说明从哪个湖读 —— daily 与 daily_basic 是两份数据。"""
    kinds = {kind for kind, _ in VALUATION_ITEMS.values()}
    assert kinds == {"daily", "daily_basic"}
    assert VALUATION_ITEMS["valuation.dividend_yield"] == ("daily_basic", "dv_ttm")


def test_missing_column_degrades_gracefully(tmp_path, monkeypatch):
    """湖在、但缺 dv_ttm 列 → 该项为空，其它项照常，而不是整体报错。"""
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    root = tmp_path / "data" / "parquet"
    _write_lake(root, "daily", pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [D2], "pe_ttm": [9.0],
    }))
    try:
        frame = valuation_frame(D2)
        assert frame["pe_ttm"][0] == pytest.approx(9.0)
        assert "dividend_yield" not in frame.columns or frame["dividend_yield"][0] is None
    finally:
        get_settings.cache_clear()


def test_column_present_but_all_values_null_is_skipped(tmp_path, monkeypatch):
    """列在、但窗口内一个有效值都没有 → 该估值项整体缺席，其它项照常。"""
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    root = tmp_path / "data" / "parquet"
    _write_lake(root, "daily", pl.DataFrame({
        "symbol": SYMS, "trade_date": [D2] * 3,
        "pe_ttm": [None, None, None],
        "pb_mrq": [1.5, 2.0, 3.0],
    }))
    try:
        frame = valuation_frame(D2)
        assert "pe_ttm" not in frame.columns or frame["pe_ttm"].to_list() == [None] * 3
        assert frame.sort("symbol")["pb"].to_list() == [1.5, 2.0, 3.0]
        assert valuation_long(D2).filter(
            pl.col("item") == "valuation.pe_ttm").is_empty()
    finally:
        get_settings.cache_clear()


def test_negative_value_only_is_dropped_from_long_table(tmp_path, monkeypatch):
    """最新有值的 PE 全为负 → 取到 bar 后被置空 → 长表里整项不出现。

    与上一条是**不同分支**：上一条在 `_last_bar` 的非空过滤就空了，
    这条是 bar 取到了、但在 `is_not_null` 过滤后为空。前者是数据源没回填，
    后者是「有值的不可用」（亏损）——两者在页面上应当能分辨。
    """
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    root = tmp_path / "data" / "parquet"
    _write_lake(root, "daily", pl.DataFrame({
        "symbol": SYMS, "trade_date": [D2] * 3,
        "pe_ttm": [-5.0, -8.0, -1.0],          # 有值，但都是亏损
        "pb_mrq": [1.5, 2.0, 3.0],
    }))
    try:
        assert valuation_long(D2).filter(
            pl.col("item") == "valuation.pe_ttm").is_empty()
        assert valuation_long(D2).filter(
            pl.col("item") == "valuation.pb").height == 3
    finally:
        get_settings.cache_clear()
