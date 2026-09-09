"""MappingProvider 引擎单元测试（TDD 先行）。

test_engine.py 已被回测引擎测试占用，故本文件独立命名。
FakeProvider 通过 tmp config_dir 的 daily_bar.yaml 走真实 load/apply 管线。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lquant.core.errors import DataQualityError, MappingError
from lquant.data.capability import Capability
from lquant.data.providers._engine import MappingProvider

FAKE_YAML = """
sources:
  fake:
    rename:
      code: symbol
      o: open
      h: high
      l: low
      c: close
      amt: amount
    derive:
      trade_date: "strptime(d, '%Y-%m-%d')"
    fill:
      sec_type: stock
    required:
      - symbol
"""

GOOD_RAW: dict[str, list[Any]] = {
    "d": ["2024-01-02", "2024-01-03"],
    "code": ["000001.SZ", "000002.SZ"],
    "o": [10.0, 11.0],
    "h": [10.5, 11.5],
    "l": [9.8, 10.8],
    "c": [10.2, 11.2],
    "amt": [30000.0, 31000.0],
}


class FakeProvider(MappingProvider):
    name = "fake"
    source = "fake"
    capability = frozenset({Capability.DAILY})

    def __init__(self, raw: dict[str, list[Any]] | None = None) -> None:
        self._raw = GOOD_RAW if raw is None else raw

    def _fetch_raw(self, table: str, **params: object) -> pl.DataFrame:
        return pl.DataFrame(self._raw)


def _make_config(tmp_path: Path, yaml_text: str = FAKE_YAML) -> Path:
    (tmp_path / "schema").mkdir(exist_ok=True)
    (tmp_path / "schema" / "daily_bar.yaml").write_text(yaml_text, encoding="utf-8")
    return tmp_path


def test_request_maps_fills_and_coerces(tmp_path: Path) -> None:
    out = FakeProvider().request("daily_bar", config_dir=_make_config(tmp_path))
    assert out["amount"].to_list() == [30000.0, 31000.0]
    assert out["sec_type"].to_list() == ["stock", "stock"]
    assert out["close"].dtype == pl.Float64
    assert out["trade_date"].dtype == pl.Date
    # schema 列序 + 缺列补 null
    assert out["volume"].is_null().all()


def test_request_params_override_fill(tmp_path: Path) -> None:
    out = FakeProvider().request(
        "daily_bar", sec_type="index", config_dir=_make_config(tmp_path)
    )
    assert out["sec_type"].to_list() == ["index", "index"]


def test_request_negative_price_raises(tmp_path: Path) -> None:
    raw = {**GOOD_RAW, "c": [-5.0, 11.2]}
    with pytest.raises(DataQualityError):
        FakeProvider(raw).request("daily_bar", config_dir=_make_config(tmp_path))


def test_request_missing_yaml_raises(tmp_path: Path) -> None:
    with pytest.raises(MappingError):
        FakeProvider().request("daily_bar", config_dir=tmp_path)


def test_post_normalize_hook(tmp_path: Path) -> None:
    class Hooked(FakeProvider):
        def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
            return df.with_columns(pl.lit("hooked").alias("hook"))

    out = Hooked().request("daily_bar", config_dir=_make_config(tmp_path))
    assert out["hook"].to_list() == ["hooked", "hooked"]


MINUTE_YAML = """
sources:
  fake:
    rename:
      code: symbol
      o: open
      h: high
      l: low
      c: close
    derive:
      ts: "strptime(d, '%Y-%m-%d %H:%M')"
    fill:
      freq: 1min
"""


def _make_minute_config(tmp_path: Path) -> Path:
    (tmp_path / "schema").mkdir(exist_ok=True)
    (tmp_path / "schema" / "minute_bar.yaml").write_text(MINUTE_YAML, encoding="utf-8")
    return tmp_path


# ---------- fix round 1：params 分离（fill 白名单） ----------

def test_request_freq_injects_minute_bar(tmp_path: Path) -> None:
    minute_raw = {
        "d": ["2024-01-02 09:30", "2024-01-02 09:31"],
        "code": ["000001.SZ", "000002.SZ"],
        "o": [10.0, 11.0],
        "h": [10.5, 11.5],
        "l": [9.8, 10.8],
        "c": [10.2, 11.2],
    }
    out = FakeProvider(minute_raw).request(
        "minute_bar", freq="5min", config_dir=_make_minute_config(tmp_path)
    )
    assert out["freq"].to_list() == ["5min", "5min"]


def test_request_freq_on_daily_bar_raises(tmp_path: Path) -> None:
    """daily_bar 无 freq 列：显式传 freq 必须 fail-fast，不得静默。"""
    with pytest.raises(MappingError, match="freq"):
        FakeProvider().request("daily_bar", freq="5min", config_dir=_make_config(tmp_path))


def test_request_fetch_params_not_fill(tmp_path: Path) -> None:
    """symbols/start/end 等 canonical fetch 参数绝不进入 fill，也不报错。"""
    captured: dict[str, Any] = {}

    class Recording(FakeProvider):
        def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
            captured.update(params)
            return pl.DataFrame(self._raw)

    out = Recording().request(
        "daily_bar",
        symbols=["000001.SZ", "000002.SZ"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        config_dir=_make_config(tmp_path),
    )
    # 全部 params 到达 _fetch_raw
    assert captured["symbols"] == ["000001.SZ", "000002.SZ"]
    assert captured["start"] == date(2024, 1, 1)
    assert captured["end"] == date(2024, 1, 3)
    # 数据两行、真实 symbol 列未被 pl.lit 常量压扁
    assert out["symbol"].to_list() == ["000001.SZ", "000002.SZ"]
    assert out["amount"].to_list() == [30000.0, 31000.0]


def test_request_raw_passthrough(tmp_path: Path) -> None:
    """_raw 直传：_fetch_raw 不被调用，直接走映射管线。"""
    raw = pl.DataFrame(GOOD_RAW)

    class NoFetch(FakeProvider):
        def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
            raise AssertionError("_fetch_raw 不应在 _raw 直传时被调用")

    out = NoFetch().request("daily_bar", _raw=raw, config_dir=_make_config(tmp_path))
    assert out["amount"].to_list() == [30000.0, 31000.0]
