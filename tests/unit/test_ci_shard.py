"""CI 分片脚本的单测。

核心不变量是「所有片的并集**恰好等于**全部测试文件」—— 漏跑一个文件会让
CI 静默变绿（少跑测试却照样报 passed），这是分片机制里唯一真正危险的失败模式，
所以它必须被显式断言，而不是靠人肉 review 分片逻辑。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

_SPEC = importlib.util.spec_from_file_location("ci_shard", _REPO / "scripts" / "ci_shard.py")
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)


def test_plan_covers_every_file_exactly_once() -> None:
    """并集 == 输入集合，且无重复。漏跑/重跑都必须在这里就炸掉。"""
    weighted = tuple((f"tests/unit/test_{i:03d}.py", i % 7 + 1) for i in range(37))

    shards = _mod.plan_shards(weighted, 3)

    flat = [f for bucket in shards for f in bucket]
    assert sorted(flat) == sorted(f for f, _ in weighted)
    assert len(flat) == len(set(flat))


def test_plan_is_deterministic() -> None:
    """同样的输入必须给出同样的分片 —— 否则重跑 CI 会得到不同的分组。"""
    weighted = tuple((f"tests/unit/test_{i:03d}.py", i) for i in range(20))

    assert _mod.plan_shards(weighted, 3) == _mod.plan_shards(weighted, 3)


def test_plan_balances_by_weight() -> None:
    """大文件要摊开：最重片与最轻片的权重差应小于单个最重文件的权重。"""
    weighted = (("tests/unit/test_big.py", 100), ("tests/unit/test_a.py", 30),
                ("tests/unit/test_b.py", 30), ("tests/unit/test_c.py", 30),
                ("tests/unit/test_d.py", 10))

    shards = _mod.plan_shards(weighted, 3)

    weights = {f: w for f, w in weighted}
    loads = [sum(weights[f] for f in bucket) for bucket in shards]
    assert max(loads) - min(loads) < 100


def test_plan_rejects_bad_shard_count() -> None:
    """片数非法要 fail-fast，而不是静默返回空分片。"""
    with pytest.raises(ValueError):
        _mod.plan_shards((("tests/unit/test_a.py", 1),), 0)


def test_count_tests_matches_def_test_occurrences(tmp_path: Path) -> None:
    """用例数近似 = 文件里 `def test_` 的出现次数（含缩进的类方法）。"""
    f = tmp_path / "test_x.py"
    f.write_text(
        "def test_a():\n    pass\n\n\n"
        "class TestGroup:\n    def test_b(self):\n        pass\n\n"
        "    async def test_c(self):\n        pass\n\n"
        "def helper():\n    pass\n",
        encoding="utf-8",
    )

    assert _mod.count_tests(f) == 3


def test_shards_of_real_repo_partition_all_test_files() -> None:
    """对真实仓库做一次全量校验：任意片数下，并集 == 全部测试文件。"""
    all_files = _mod.test_files(_REPO)
    assert len(all_files) > 100, "测试文件数异常，glob 可能写错了"

    for shards in (2, 3, 4):
        flat = [f for i in range(1, shards + 1)
                for f in _mod.shard_for(i, shards, _REPO)]
        assert sorted(flat) == sorted(all_files)
        assert len(flat) == len(set(flat))


def test_main_prints_space_separated_files_for_shard(capsys) -> None:
    """CLI 输出直接喂给 pytest：空格分隔的单行文件列表。"""
    rc = _mod.main(["2", "3"], _REPO)

    assert rc == 0
    out = capsys.readouterr().out.strip()
    files = out.split()
    assert files, "该片不该为空"
    assert all(f.startswith("tests/") and f.endswith(".py") for f in files)


def test_main_rejects_out_of_range_shard(capsys) -> None:
    """片号越界要报错退出，而不是悄悄跑全量或跑空。"""
    with pytest.raises(SystemExit):
        _mod.main(["4", "3"], _REPO)


def test_main_fails_loudly_on_empty_shard(tmp_path: Path) -> None:
    """空片必须非零退出。

    只打一行警告 + return 0 的话，这个 job 就是「零测试通过 = 绿」—— 正是分片
    最该防的静默失败（片数被调大而文件数不够时就会发生）。
    """
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_only.py").write_text("def test_a():\n    pass\n")

    with pytest.raises(SystemExit) as exc:
        _mod.main(["2", "3"], tmp_path)

    assert exc.value.code == 1


def test_test_files_covers_both_pytest_default_namings(tmp_path: Path) -> None:
    """glob 必须覆盖 pytest 默认 python_files 的两种命名。

    只 glob `test_*.py` 的话，将来有人加一个 `foo_test.py`，全量 pytest 会收集它、
    而任何分片都跑不到它，CI 却照样全绿。
    """
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "tests" / "unit" / "b_test.py").write_text("def test_b():\n    pass\n")
    (tmp_path / "tests" / "unit" / "helper.py").write_text("def helper():\n    pass\n")

    assert _mod.test_files(tmp_path) == ("tests/unit/b_test.py", "tests/unit/test_a.py")
