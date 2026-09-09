"""mapping.py 单元测试：加载 / 校验 / 应用。"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from lquant.core.errors import MappingError
from lquant.data.mapping import (
    TableMapping,
    apply_mapping,
    load_table_mapping,
    validate_table_config,
)
from lquant.data.schema import SCHEMAS

CLEAN_YAML = """
sources:
  baostock:
    rename:
      code: symbol
    derive:
      ts: "strptime(d, '%Y-%m-%d %H:%M')"
    fill:
      freq: 1min
    required:
      - symbol
"""

BAD_TARGET_YAML = """
sources:
  baostock:
    rename:
      not_a_col: oops
"""

BAD_EXPR_YAML = """
sources:
  baostock:
    derive:
      hack: "d.__class__"
"""


def _write(tmp_path: Path, content: str, name: str = "minute_bar") -> Path:
    p = tmp_path / "schema"
    p.mkdir(exist_ok=True)
    f = p / f"{name}.yaml"
    f.write_text(content, encoding="utf-8")
    return f


def _df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "d": ["2024-01-02 09:30", "2024-01-03 09:31"],
            "code": ["000001.SZ", "000002.SZ"],
            "close": [10.0, 11.0],
            "volume": [100.0, 200.0],
            "extra_col": ["x", "y"],
        }
    )


def test_load_table_mapping_reads_source_section(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN_YAML)
    tm = load_table_mapping("minute_bar", "baostock", config_dir=tmp_path)
    assert tm.table == "minute_bar"
    assert tm.rename == {"code": "symbol"}
    assert "ts" in tm.derive
    assert tm.fill == {"freq": "1min"}
    assert tm.required == ("symbol",)
    assert isinstance(tm, TableMapping)


def test_load_missing_source_raises(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN_YAML)
    with pytest.raises(MappingError):
        load_table_mapping("minute_bar", "tushare", config_dir=tmp_path)


def test_apply_mapping_rename_derive_fill(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN_YAML)
    tm = load_table_mapping("minute_bar", "baostock", config_dir=tmp_path)
    out = apply_mapping(_df(), tm)
    assert out.columns == list(SCHEMAS["minute_bar"])
    assert out["symbol"].to_list() == ["000001.SZ", "000002.SZ"]
    assert out["freq"].to_list() == ["1min", "1min"]
    assert out["ts"].dtype == pl.Datetime
    # schema 外列被丢弃
    assert "extra_col" not in out.columns
    # rename 用的源列也不残留
    assert "d" not in out.columns
    assert "code" not in out.columns


def test_apply_mapping_fill_overridden_by_params(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN_YAML)
    tm = load_table_mapping("minute_bar", "baostock", config_dir=tmp_path)
    out = apply_mapping(_df(), tm, params={"freq": "15min"})
    assert out["freq"].to_list() == ["15min", "15min"]


def test_apply_mapping_derive_arith(tmp_path: Path) -> None:
    tm = TableMapping(
        table="daily_bar",
        rename={},
        derive={"amount": "volume * close"},
        fill={},
        required=(),
    )
    df = pl.DataFrame({"volume": [2.0], "close": [3.0]})
    out = apply_mapping(df, tm)
    assert out["amount"].to_list() == [6.0]


def test_apply_mapping_derive_strptime_date(tmp_path: Path) -> None:
    tm = TableMapping(
        table="daily_bar",
        rename={},
        derive={"trade_date": "strptime(d, '%Y-%m-%d')"},
        fill={},
        required=(),
    )
    df = pl.DataFrame({"d": ["2024-01-02"]})
    out = apply_mapping(df, tm)
    assert out["trade_date"].dtype == pl.Date


def test_apply_mapping_rejects_forbidden_node(tmp_path: Path) -> None:
    tm = TableMapping(
        table="minute_bar",
        rename={},
        derive={"bad": "d.__class__"},
        fill={},
        required=(),
    )
    with pytest.raises(MappingError):
        apply_mapping(_df(), tm)


def test_validate_clean_file(tmp_path: Path) -> None:
    path = _write(tmp_path, CLEAN_YAML)
    assert validate_table_config("minute_bar", path) == []


def test_validate_reports_unknown_target(tmp_path: Path) -> None:
    path = _write(tmp_path, BAD_TARGET_YAML)
    errs = validate_table_config("minute_bar", path)
    assert errs and ("not_a_col" in errs[0] or "oops" in errs[0])


def test_validate_reports_forbidden_expr(tmp_path: Path) -> None:
    path = _write(tmp_path, BAD_EXPR_YAML)
    errs = validate_table_config("minute_bar", path)
    assert errs, "应报告非法表达式"


def test_validate_missing_file(tmp_path: Path) -> None:
    errs = validate_table_config("minute_bar", tmp_path / "schema" / "minute_bar.yaml")
    assert errs


BAD_RENAME_LOAD_YAML = """
sources:
  baostock:
    rename:
      d: oops_not_a_col
"""

BAD_FILL_LOAD_YAML = """
sources:
  baostock:
    fill:
      not_a_fill_col: 1min
"""


def test_load_rejects_illegal_rename_target(tmp_path: Path) -> None:
    _write(tmp_path, BAD_RENAME_LOAD_YAML)
    with pytest.raises(MappingError, match="oops_not_a_col"):
        load_table_mapping("minute_bar", "baostock", config_dir=tmp_path)


def test_load_rejects_illegal_fill_key(tmp_path: Path) -> None:
    _write(tmp_path, BAD_FILL_LOAD_YAML)
    with pytest.raises(MappingError, match="not_a_fill_col"):
        load_table_mapping("minute_bar", "baostock", config_dir=tmp_path)
