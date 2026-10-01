"""yaml_source adapter 单元测试（chdir 隔离 custom.yaml）。"""

from __future__ import annotations

import pytest

from lquant.factors.sources import yaml_source


@pytest.fixture
def yaml_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    # 无全局缓存需要清理；CUSTOM_YAML 是相对路径，chdir 恢复由 monkeypatch 保证


def _write(tmp_path, content: str) -> None:
    d = tmp_path / "config" / "factors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "custom.yaml").write_text(content, encoding="utf-8")


def test_missing_file_returns_empty(yaml_root):
    assert yaml_source.load_custom() == []


def test_empty_yaml_returns_empty(yaml_root):
    _write(yaml_root, "")
    assert yaml_source.load_custom() == []


def test_null_yaml_returns_empty(yaml_root):
    # `factors:` 空值 → None 按空清单处理，不抛 TypeError
    _write(yaml_root, "factors:\n")
    assert yaml_source.load_custom() == []


def test_valid_factor_roundtrip(yaml_root):
    _write(
        yaml_root,
        (
            "factors:\n"
            "  - name: my_alpha\n"
            "    expr: 'Ts_Mean($close, 5)'\n"
            "    category: TS\n"
        ),
    )
    out = yaml_source.load_custom()
    assert len(out) == 1
    it = out[0]
    assert it["name"] == "my_alpha"
    assert it["expression"] == "Ts_Mean($close, 5)"
    assert it["description"] == "TS"
    assert it["source"] == "yaml"
    assert it["source_ref"] == "config/factors/custom.yaml"
    assert it["factor_id"] is None
    assert it["category"] == "TS"


def test_invalid_expr_fail_fast(yaml_root):
    _write(
        yaml_root,
        (
            "factors:\n"
            "  - name: bad\n"
            "    expr: 'Ts_Mean($close 5)'\n"
        ),
    )
    from lquant.core.errors import DSLParseError

    with pytest.raises(DSLParseError):
        yaml_source.load_custom()


def test_factor_id_delegates(yaml_root):
    from lquant.factors.sources.qlib_source import factor_id as fid

    e1 = "Mean($close, 5)"  # Ts_Mean 不在 qlib 翻译表内，会 ValueError
    e2 = "Mean($close, 10)"
    assert yaml_source.factor_id(e1) == fid(e1)
    assert yaml_source.factor_id(e1) == yaml_source.factor_id(e1)
    assert yaml_source.factor_id(e1) != yaml_source.factor_id(e2)
