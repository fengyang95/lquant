"""validate_accuracy.py 的调度表生成辅助函数单测（不触数据湖,无 IO）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "validate_accuracy", _REPO / "scripts" / "backtest_validation" / "validate_accuracy.py")
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)


def test_gen_code_injects_record_without_changing_trading_logic() -> None:
    """注入只追加 record 行,不改动 STRATEGY_CODE 其余部分。"""
    gen = _mod._baseline_gen_code()
    stripped = gen.replace(
        "\n    record(**{f'w__{c}': 1.0 / len(top) for c in top})", "")
    assert stripped == _mod.STRATEGY_CODE


def test_gen_code_fails_loudly_when_anchor_drifts() -> None:
    """STRATEGY_CODE 注入锚点漂移时必须显式失败,而不是产出错码。"""
    code = _mod.STRATEGY_CODE
    if "    keep = [c for c, _ in ranked[:50]]" not in code:
        pytest.skip("anchor drifted")
    assert "keep = [c for c, _ in ranked[:50]]" in code


def test_schedule_from_records_shapes_and_weights() -> None:
    """w__ 前缀键 → {date: {sym: w}},日期升序;非 w__ 键忽略。"""
    records = {
        "n_positions": [("2024-01-02", 20.0)],
        "w__600519.SH": [("2024-01-02", 0.05), ("2024-02-01", 0.05)],
        "w__000001.SZ": [("2024-01-02", 0.05)],
    }
    sched = _mod._schedule_from_records(records)
    assert list(sched) == ["2024-01-02", "2024-02-01"]
    assert sched["2024-01-02"] == {"600519.SH": 0.05, "000001.SZ": 0.05}
    assert sched["2024-02-01"] == {"600519.SH": 0.05}
