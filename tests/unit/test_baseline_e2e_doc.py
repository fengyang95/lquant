"""E2E 文档写回不丢人工补注的回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_baseline_e2e.py"
_SPEC = importlib.util.spec_from_file_location("run_baseline_e2e", _SCRIPT)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

merge_manual_notes = _MOD.merge_manual_notes
extract_manual_notes = _MOD.extract_manual_notes


NEW_DOC = """# 运行记录

## Metrics

| a | b |
|---|---|
| 1 | 2 |
"""

OLD_NOTES = """## 人工补注

1. **数据缺口**:2021-2023 未回填。
2. **前 4 个月 0 持仓是预热伪影**。
"""

OLD_DOC = NEW_DOC + "\n" + OLD_NOTES


def test_extract_returns_notes_to_eof():
    assert extract_manual_notes(OLD_DOC).startswith("## 人工补注")
    assert "预热伪影" in extract_manual_notes(OLD_DOC)


def test_extract_missing_returns_empty():
    assert extract_manual_notes(NEW_DOC) == ""
    assert extract_manual_notes("") == ""


def test_merge_preserves_manual_notes_on_rerun():
    merged = merge_manual_notes(NEW_DOC, OLD_DOC)
    assert merged.startswith("# 运行记录")
    assert "预热伪影" in merged
    assert merged.count("## 人工补注") == 1


def test_merge_idempotent_and_noop():
    # 新文档已带补注段 → 原样返回
    assert merge_manual_notes(NEW_DOC + OLD_NOTES, OLD_DOC) == NEW_DOC + OLD_NOTES
    # 旧文档无补注段 → 原样返回
    assert merge_manual_notes(NEW_DOC, NEW_DOC) == NEW_DOC
