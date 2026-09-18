"""pre-push 钩子的「湖是否为空」探针：符号链接的湖绝不能判成空。

回归背景（真实事故）：钩子原来用 `find data/parquet -name '*.parquet'`。
`find` 默认**不跟随符号链接**，而隔离 worktree 的标准姿势正是把
`data/parquet` 软链到主仓的真实湖 —— 于是探测永远返回「没有 parquet」，
钩子就去跑 `lq data demo`，把 300 只合成标的里的 30 只真代码（含
600519.SH / 510300.SH）合成日线覆盖进真实湖（实测 21,300 行 / 3 个年分区，
30 只标的的 2024-01-01~2026-09-18 真实行被合成值替换）。

用例直接抽取钩子里的探针片段在真实文件系统上跑，避免与脚本正文漂移。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "githooks" / "pre-push"

_START = "lake_empty=0"
_END = 'if [ "$lake_empty" = "1" ]'


def _probe_snippet() -> str:
    text = HOOK.read_text(encoding="utf-8")
    i = text.index(_START)
    j = text.index(_END)
    return text[i:j]


def _run_probe(root: Path) -> str:
    """按钩子的真实执行环境（set -euo pipefail）跑探针片段。"""
    script = "set -euo pipefail\n" + _probe_snippet() + '\nprintf "%s" "$lake_empty"'
    r = subprocess.run(
        ["bash", "-c", script],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _parquet_at(p: Path) -> None:
    (p / "daily" / "year=2024").mkdir(parents=True, exist_ok=True)
    (p / "daily" / "year=2024" / "part-0.parquet").write_bytes(b"x")


def test_probe_snippet_is_extractable() -> None:
    """探针片段必须能从脚本里抽出来 —— 抽不出来说明本用例已失焦。"""
    snippet = _probe_snippet()
    assert "find -L data/parquet" in snippet
    assert "-L data/parquet" in snippet


@pytest.mark.parametrize("symlinked", [False, True])
def test_nonempty_lake_never_counts_as_empty(tmp_path: Path,
                                            symlinked: bool) -> None:
    """真实湖（含 parquet）非空 —— 软链形态也不能判成空。"""
    lake = tmp_path / "real-lake"
    _parquet_at(lake)
    root = tmp_path / "root"
    root.mkdir()
    if symlinked:
        (root / "data").mkdir()
        (root / "data" / "parquet").symlink_to(lake)
    else:
        _parquet_at(root / "data" / "parquet")
    assert _run_probe(root) == "0"


def test_symlinked_lake_never_gets_demo_data(tmp_path: Path) -> None:
    """即使软链指向的湖真为空，也不能拿它当「全新 checkout」灌合成数据。

    它指向的是别人的湖：写进去就是污染真实数据目录。
    """
    lake = tmp_path / "empty-lake"
    lake.mkdir()
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "parquet").symlink_to(lake)
    assert _run_probe(root) == "0"


def test_truly_empty_real_dir_still_allows_demo(tmp_path: Path) -> None:
    """全新 checkout（真·空目录）仍要保留演示数据兜底，别把功能改没了。"""
    root = tmp_path / "root"
    (root / "data" / "parquet").mkdir(parents=True)
    assert _run_probe(root) == "1"


def test_missing_lake_dir_allows_demo(tmp_path: Path) -> None:
    """目录都不存在（老 checkout）同样算空 → 兜底生成。"""
    root = tmp_path / "root"
    root.mkdir()
    assert _run_probe(root) == "1"
