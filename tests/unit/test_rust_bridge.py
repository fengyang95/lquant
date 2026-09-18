"""rust_bridge 单元测试：可用性探测 + 降级路径（lq_ops 打桩）。"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, PropertyMock

import pytest

from lquant.factors.ops import rust_bridge


@pytest.fixture
def clean_lq_ops(monkeypatch):
    monkeypatch.delitem(sys.modules, "lq_ops", raising=False)
    yield
    monkeypatch.delitem(sys.modules, "lq_ops", raising=False)


def test_rust_available_real():
    # 当前环境 lq_ops 可导入
    assert rust_bridge.rust_available() is True


def test_rust_available_missing(clean_lq_ops, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "lq_ops",
        None,  # None 在 sys.modules 中会让 import 抛 ImportError
    )
    assert rust_bridge.rust_available() is False


def test_register_no_ops_when_available(clean_lq_ops):
    n = rust_bridge.register_rust_ops()
    assert n == 0


def test_register_degrades_when_missing(clean_lq_ops, monkeypatch):
    monkeypatch.setitem(sys.modules, "lq_ops", None)
    assert rust_bridge.register_rust_ops() == 0


def test_register_swallows_version_error(clean_lq_ops, monkeypatch):
    """lq_ops 导入成功但取 __version__ 炸 → 异常被吞，仍返回 0。"""
    mod = MagicMock(spec=types.ModuleType)
    type(mod).__version__ = PropertyMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "lq_ops", mod)
    assert rust_bridge.register_rust_ops() == 0
