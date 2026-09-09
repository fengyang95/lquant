"""三源对拍（mock 数据，不触网）+ 启动映射校验接线测试。

对拍：同一标的（600000.SH）同一交易日，构造 baostock / akshare / tushare
三种源形态的 mock 数据，各自走对应 provider 的 request（即对应 yaml 节的
load_table_mapping + apply_mapping），断言归一化后 OHLC 与 volume/amount
一致 —— 这是三份映射 yaml 的回归验证。

校验：validate_all_mappings 对 config/schema/*.yaml 全量静态校验，
任何错误收集后抛 MappingError（fail-fast），信息含文件名与全部错误行。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.core.errors import MappingError
from lquant.data.mapping import validate_all_mappings
from lquant.data.providers.akshare import AkShareProvider
from lquant.data.providers.baostock import BaoStockProvider
from lquant.data.providers.tushare import TushareProvider
from lquant.data.schema import SCHEMAS

# ---- 三源共认的「真实值」（归一化目标：股 / 元） --------------------------
_SYMBOL = "600000.SH"
_DAY = date(2024, 1, 2)
_OPEN, _HIGH, _LOW, _CLOSE, _PRE_CLOSE = 10.0, 10.5, 9.8, 10.2, 10.1
_VOLUME_SHARES = 120_000.0        # 股
_AMOUNT_YUAN = 1_224_000.0        # 元

# baostock：amount 千元（yaml derive ×100）、volume 股、code sh.600000
_BAOSTOCK_RAW = pl.DataFrame({
    "date": ["2024-01-02"],
    "code": ["sh.600000"],
    "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
    "preclose": [10.1],
    "volume": [_VOLUME_SHARES],
    "amount": [_AMOUNT_YUAN / 100],       # 千元（按 yaml 换算系数反推）
    "turn": [0.12],
    "is_st": ["0"],
# 日期解析（_fetch_daily 产出即 pl.Date）与 baostock 源列保持一致
}).with_columns(pl.col("date").str.to_date("%Y-%m-%d"))

# akshare：中文列、volume 手（derive ×100）、amount 已是元直接 rename
_AKSHARE_RAW = pl.DataFrame({
    "股票代码": ["600000"],
    "日期": ["2024-01-02"],
    "开盘": [10.0], "收盘": [10.2], "最高": [10.5], "最低": [9.8],
    "成交量": [_VOLUME_SHARES / 100],      # 手
    "成交额": [_AMOUNT_YUAN],              # 元
}).with_columns(pl.col("日期").str.to_date("%Y-%m-%d"))

# tushare：ts_code 直出、trade_date YYYYMMDD、vol 手（×100）、amount 千元（×1000）
_TUSHARE_RAW = pl.DataFrame({
    "ts_code": ["600000.SH"],
    "trade_date": [_DAY],   # trade_date 解析（YYYYMMDD → Date）在 provider 侧完成
    "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
    "pre_close": [10.1],
    "vol": [_VOLUME_SHARES / 100],         # 手
    "amount": [_AMOUNT_YUAN / 1000],       # 千元
})


def _normalized(provider: BaoStockProvider | AkShareProvider | TushareProvider,
                raw: pl.DataFrame) -> pl.DataFrame:
    """mock 源数据 → request（含 yaml 映射 + coerce + 质量断言）。"""
    return provider.request("daily_bar", _raw=raw)


def test_three_sources_ohlc_consistent() -> None:
    outs = [
        _normalized(BaoStockProvider(), _BAOSTOCK_RAW),
        _normalized(AkShareProvider(), _AKSHARE_RAW),
        _normalized(TushareProvider(token="fake"), _TUSHARE_RAW),
    ]
    for out in outs:
        assert out.height == 1
        assert out["symbol"].to_list() == [_SYMBOL]   # 各源代码格式归一
        assert out["trade_date"].to_list() == [_DAY]
        assert out["open"].to_list() == [_OPEN]
        assert out["high"].to_list() == [_HIGH]
        assert out["low"].to_list() == [_LOW]
        assert out["close"].to_list() == [_CLOSE]


def test_three_sources_volume_amount_consistent() -> None:
    outs = [
        _normalized(BaoStockProvider(), _BAOSTOCK_RAW),
        _normalized(AkShareProvider(), _AKSHARE_RAW),
        _normalized(TushareProvider(token="fake"), _TUSHARE_RAW),
    ]
    base = outs[0]
    for out in outs:
        assert out["volume"].to_list() == pytest.approx(
            [_VOLUME_SHARES], abs=1e-6
        )
        assert out["amount"].to_list() == pytest.approx(
            [_AMOUNT_YUAN], abs=1e-6
        )
        # 源间数值一致（tolerance 0 —— 同一换算终点必须精确相等）
        assert out["volume"].to_list() == base["volume"].to_list()
        assert out["amount"].to_list() == base["amount"].to_list()


def test_output_columns_subset_of_schema() -> None:
    for out in (
        _normalized(BaoStockProvider(), _BAOSTOCK_RAW),
        _normalized(AkShareProvider(), _AKSHARE_RAW),
        _normalized(TushareProvider(token="fake"), _TUSHARE_RAW),
    ):
        assert set(out.columns) <= set(SCHEMAS["daily_bar"])
        assert out.columns == list(SCHEMAS["daily_bar"])


# ------------------------------------------------------- 启动校验接线
def _write_yaml(tmp_path: Path, name: str, text: str) -> Path:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(exist_ok=True)
    path = schema_dir / name
    path.write_text(text, encoding="utf-8")
    return path


_BAD_YAML = """\
table: daily_bar
sources:
  broken:
    rename:
      x: not_a_schema_col
    derive:
      volume: {expr: "vol * 100", from: [vol]}
      bad_col: {expr: "vol + 1", from: [vol]}
"""


def test_validate_all_mappings_collects_all_errors(tmp_path: Path) -> None:
    """坏 yaml → MappingError，信息含文件名与全部错误行（收集式，不短路）。"""
    _write_yaml(tmp_path, "daily_bar.yaml", _BAD_YAML)
    with pytest.raises(MappingError) as exc:
        validate_all_mappings(tmp_path)
    msg = str(exc.value)
    assert "daily_bar.yaml" in msg
    assert "not_a_schema_col" in msg
    assert "bad_col" in msg     # 两条错误都收集，而非只报第一条


def test_validate_all_mappings_empty_dir_ok(tmp_path: Path) -> None:
    """schema 目录缺 yaml / 缺目录都要优雅通过（目录可能只有两份表配置）。"""
    validate_all_mappings(tmp_path)          # 无 schema 子目录
    (tmp_path / "schema").mkdir()
    validate_all_mappings(tmp_path)          # 空目录
