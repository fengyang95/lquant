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


def _lq_ops_built() -> bool:
    """环境里是否真的编译并安装了 lq_ops（与 test_rust_alignment 的 skipif 同口径）。"""
    try:
        import lq_ops  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _lq_ops_built(), reason="未编译 Rust：先 make rust-build")
def test_rust_available_real():
    # 当前环境 lq_ops 可导入。未编译 Rust 的 worktree / CI 环境没有 lq_ops，
    # 这条环境探针不能不加防护地断 True——否则 pre-push 全量测试必挂
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
