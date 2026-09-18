"""批次一覆盖补充：data/mapping.py 派生表达式编译 / 校验错误分支。"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from lquant.core.errors import MappingError
from lquant.data import mapping as m


def _df() -> pl.DataFrame:
    return pl.DataFrame({
        "a": ["1", "2"],
        "b": ["10", "20"],
        "raw_date": ["20240101", "20240102"],
        "ts": ["20240101 09:30:00", "20240102 15:00:00"],
    })


# ---------- 表达式解析 / 规则解析 ----------


def test_parse_expr_syntax_error():
    with pytest.raises(MappingError, match="非法表达式"):
        m._parse_expr("a +")


def test_coerce_derive_rule_dict_form():
    r = m._coerce_derive_rule("x", {"expr": "a + b", "from": ["a", "b"]})
    assert r.expr == "a + b" and r.from_cols == ("a", "b")
    with pytest.raises(MappingError, match="from 必须是"):
        m._coerce_derive_rule("x", {"expr": "a", "from": "bad"})


def test_coerce_derive_rule_dict_errors():
    with pytest.raises(MappingError, match="非空 expr"):
        m._coerce_derive_rule("x", {"expr": ""})
    with pytest.raises(MappingError, match="非空 expr"):
        m._coerce_derive_rule("x", {})
    with pytest.raises(MappingError, match="from 必须是"):
        m._coerce_derive_rule("x", {"expr": "a", "from": [1, 2]})


def test_coerce_rule_string_form():
    r = m._coerce_rule("a + b")
    assert r.expr == "a + b" and r.from_cols == ("a", "b")


# ---------- load_table_mapping ----------


def _write_yaml(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_table_mapping_missing_file(tmp_path):
    with pytest.raises(MappingError, match="映射配置不存在"):
        m.load_table_mapping("daily_bar", "tushare", config_dir=tmp_path)


def test_load_table_mapping_missing_source(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", "sources: {}\n")
    with pytest.raises(MappingError, match="缺少 sources"):
        m.load_table_mapping("daily_bar", "tushare", config_dir=tmp_path)


def test_load_table_mapping_unknown_table(tmp_path):
    _write_yaml(tmp_path / "schema" / "nope.yaml", "sources:\n  a: {}\n")
    with pytest.raises(MappingError, match="未知表"):
        m.load_table_mapping("nope", "a", config_dir=tmp_path)


def test_load_table_mapping_target_not_in_schema(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", """
sources:
  src:
    rename:
      open_price: not_a_col
""")
    with pytest.raises(MappingError, match="不在 SCHEMAS"):
        m.load_table_mapping("daily_bar", "src", config_dir=tmp_path)


def test_load_table_mapping_fill_and_derive_target_errors(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", """
sources:
  src:
    fill:
      not_a_col: 1
    derive:
      not_a_col2: "a + b"
""")
    with pytest.raises(MappingError, match="fill"):
        m.load_table_mapping("daily_bar", "src", config_dir=tmp_path)
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", """
sources:
  src:
    derive:
      not_a_col2: "a + b"
""")
    with pytest.raises(MappingError, match="derive"):
        m.load_table_mapping("daily_bar", "src", config_dir=tmp_path)


def test_load_table_mapping_success(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", """
sources:
  src:
    rename:
      o: open
    fill:
      source: demo
    derive:
      amount: "(open + close) / 2"
    required: [symbol]
""")
    tm = m.load_table_mapping("daily_bar", "src", config_dir=tmp_path)
    assert tm.rename == {"o": "open"}
    assert tm.fill == {"source": "demo"}
    assert tm.required == ("symbol",)
    assert tm.derive["amount"].from_cols == ("open", "close")


def test_load_table_mapping_empty_sec(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", "sources:\n  src:\n")
    tm = m.load_table_mapping("daily_bar", "src", config_dir=tmp_path)
    assert tm.rename == {} and tm.derive == {} and tm.fill == {}


# ---------- 表达式编译分支 ----------


def test_compile_name_missing_column():
    with pytest.raises(MappingError, match="不存在的列"):
        m._compile_node(m._parse_expr("nope").body, _df(), "t")


def test_compile_binop_disallowed_op():
    with pytest.raises(MappingError, match="不支持的运算符"):
        m._compile_node(m._parse_expr("a % b").body, _df(), "t")


def test_compile_call_variants():
    with pytest.raises(MappingError, match="只允许调用"):
        m._compile_node(m._parse_expr("upper(a)").body, _df(), "t")
    with pytest.raises(MappingError, match="不允许关键字参数"):
        m._compile_node(m._parse_expr("concat(a, sep='-')").body, _df(), "t")
    with pytest.raises(MappingError, match="至少一个参数"):
        m._compile_node(m._parse_expr("concat()").body, _df(), "t")


def test_compile_strptime_errors():
    with pytest.raises(MappingError, match="需要两个参数"):
        m._compile_node(m._parse_expr("strptime(a)").body, _df(), "t")
    with pytest.raises(MappingError, match="第一参数必须是列名"):
        m._compile_node(m._parse_expr("strptime('x', '%Y')").body, _df(), "t")
    with pytest.raises(MappingError, match="引用不存在"):
        m._compile_node(m._parse_expr("strptime(nope, '%Y')").body, _df(), "t")
    with pytest.raises(MappingError, match="fmt 必须是字符串"):
        m._compile_node(m._parse_expr("strptime(a, b)").body, _df(), "t")


def test_compile_strptime_date_and_datetime():
    df = _df()
    e1 = m._compile_node(m._parse_expr("strptime(raw_date, '%Y%m%d')").body, df, "t")
    out = df.with_columns(e1.alias("d"))
    assert out["d"].dtype == pl.Date
    e2 = m._compile_node(m._parse_expr("strptime(ts, '%Y%m%d %H:%M:%S')").body, df, "t")
    out2 = df.with_columns(e2.alias("d2"))
    assert out2["d2"].dtype == pl.Datetime


# ---------- apply_mapping ----------


def test_apply_mapping_unknown_table():
    tm = m.TableMapping(table="nope", rename={}, derive={}, fill={}, required=())
    with pytest.raises(MappingError, match="未知表"):
        m.apply_mapping(_df(), tm)


def test_apply_mapping_runtime_target_check():
    tm = m.TableMapping(table="daily_bar", rename={"a": "not_a_col"},
                        derive={}, fill={}, required=())
    with pytest.raises(MappingError, match="rename"):
        m.apply_mapping(_df(), tm)


def test_apply_mapping_boolean_cast():
    df = pl.DataFrame({"is_st": ["1", "true", "no", "t"]})
    tm = m.TableMapping(table="security", rename={}, derive={}, fill={}, required=())
    out = m.apply_mapping(df, tm)
    assert out["is_st"].to_list() == [True, True, False, True]


def test_apply_mapping_params_override_fill_and_schema_cast():
    df = pl.DataFrame({"sym": ["600000.SH"], "px": [10.5]})
    tm = m.TableMapping(
        table="daily_bar", rename={"sym": "symbol", "px": "close"},
        derive={"total_mv": m.DeriveRule(expr="close * 100", from_cols=("close",))},
        fill={"source": "yaml"}, required=())
    out = m.apply_mapping(df, tm, params={"source": "runtime"})
    assert out["source"][0] == "runtime"
    assert out["close"][0] == 10.5
    assert out["total_mv"][0] == 1050.0


def test_apply_mapping_missing_cols_become_null():
    df = pl.DataFrame({"symbol": ["600000.SH"]})
    tm = m.TableMapping(table="daily_bar", rename={}, derive={}, fill={}, required=())
    out = m.apply_mapping(df, tm)
    assert out["close"].is_null().all()
    assert out["trade_date"].dtype == pl.Date


# ---------- 静态校验 ----------


def test_walk_static_rejects_bad_nodes():
    errs: list[str] = []
    m._walk_static(m._parse_expr("a.b").body, "t", errs)
    assert errs and "不允许的语法节点" in errs[0]
    errs2: list[str] = []
    m._walk_static(m._parse_expr("a % b").body, "t", errs2)
    assert "不支持的运算符" in errs2[0]


def test_walk_static_call_checks():
    errs: list[str] = []
    m._walk_static(m._parse_expr("upper(a)").body, "t", errs)
    assert "只允许调用" in errs[0]
    errs2: list[str] = []
    m._walk_static(m._parse_expr("concat(a, b)").body, "t", errs2)
    assert errs2 == []


def test_validate_table_config_missing_file(tmp_path):
    errs = m.validate_table_config("daily_bar", tmp_path / "daily_bar.yaml")
    assert errs == ["配置文件不存在: " + str(tmp_path / "daily_bar.yaml")]


def test_validate_table_config_stem_mismatch(tmp_path):
    p = _write_yaml(tmp_path / "other.yaml", "sources:\n  a: {}\n")
    errs = m.validate_table_config("daily_bar", p)
    assert any("文件名应与表名一致" in e for e in errs)


def test_validate_table_config_bad_yaml(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", "sources: [unclosed\n")
    errs = m.validate_table_config("daily_bar", p)
    assert any("YAML 解析失败" in e for e in errs)


def test_validate_table_config_not_dict(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", "- 1\n- 2\n")
    errs = m.validate_table_config("daily_bar", p)
    assert any("顶层必须是 mapping" in e for e in errs)


def test_validate_table_config_missing_sources(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", "foo: 1\n")
    errs = m.validate_table_config("daily_bar", p)
    assert any("缺少 sources 节" in e for e in errs)


def test_validate_table_config_unknown_table(tmp_path):
    p = _write_yaml(tmp_path / "nope.yaml", "sources:\n  a: {}\n")
    errs = m.validate_table_config("nope", p)
    assert any("未知表" in e for e in errs)


def test_validate_table_config_collects_derive_errors(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", """
sources:
  src:
    rename:
      o: not_a_col
    derive:
      bad: "a % b"
      worse: "__import__('os')"
""")
    errs = m.validate_table_config("daily_bar", p)
    assert any("不在 SCHEMAS" in e for e in errs)
    assert any("不支持的运算符" in e for e in errs)
    assert any("只允许调用" in e for e in errs)


def test_validate_table_config_ok(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", """
sources:
  src:
    rename:
      o: open
    derive:
      amount: "(open + close) / 2"
""")
    assert m.validate_table_config("daily_bar", p) == []


def test_validate_all_mappings_no_dir(tmp_path):
    assert m.validate_all_mappings(config_dir=tmp_path) is None


def test_validate_all_mappings_raises_on_errors(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", """
sources:
  src:
    derive:
      bad: "a % b"
""")
    with pytest.raises(MappingError, match="拒绝启动"):
        m.validate_all_mappings(config_dir=tmp_path)


def test_validate_all_mappings_skips_non_lake_tables(tmp_path):
    _write_yaml(tmp_path / "schema" / "news.yaml", "foo: 1\n")
    assert m.validate_all_mappings(config_dir=tmp_path) is None


def test_validate_all_mappings_ok(tmp_path):
    _write_yaml(tmp_path / "schema" / "daily_bar.yaml", "sources:\n  a: {}\n")
    assert m.validate_all_mappings(config_dir=tmp_path) is None


def test_compile_all_binops_and_fill_loop():
    df = pl.DataFrame({"a": [1.0], "b": [2.0]})
    tm = m.TableMapping(
        table="daily_bar", rename={"a": "close", "b": "open"},
        derive={
            "total_mv": m.DeriveRule(expr="close - open", from_cols=("close", "open")),
            "amount": m.DeriveRule(expr="close * open", from_cols=("close", "open")),
            "pe_ttm": m.DeriveRule(expr="close / open", from_cols=("close", "open")),
        },
        fill={"source": "demo"}, required=())
    out = m.apply_mapping(df, tm)
    assert out["total_mv"][0] == -1.0
    assert out["amount"][0] == 2.0
    assert out["pe_ttm"][0] == 0.5
    assert out["source"][0] == "demo"  # fill（非 params）路径


def test_validate_table_config_bad_derive_expr_and_fill_target(tmp_path):
    p = _write_yaml(tmp_path / "daily_bar.yaml", """
sources:
  src:
    fill:
      not_a_col: 1
    derive:
      bad: "a +"
""")
    errs = m.validate_table_config("daily_bar", p)
    assert any("非法表达式" in e for e in errs)
    assert any("fill: 目标列" in e for e in errs)
