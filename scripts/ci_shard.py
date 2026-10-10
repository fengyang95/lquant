#!/usr/bin/env python3
"""CI 分片：把测试文件均衡分给 N 片，让 pytest 在多个 runner 上并行跑。

用法（CI 里每个分片 job 调一次，输出直接喂给 pytest）：

    .venv/bin/python scripts/ci_shard.py 1 3

分片是**确定性**的（同输入必同分组），并保证「所有片的并集 == 全部测试文件」。
这条不变量是分片机制里唯一真正危险的失败模式 —— 漏掉一个文件，CI 会少跑测试
却照样报绿，所以它有专门的单测（``tests/unit/test_ci_shard.py``）盯着。

为什么不用 ``pytest --collect-only`` 拿真实用例数：那要 import 整个 lquant 包，
每个分片多花 20~30s（3 片就是一分钟出头），比它换来的那点均衡收益更贵。
改用「文件里 ``def test_`` 的出现次数」作廉价近似 —— 参数化会被低估，但均衡
只影响墙钟（差几秒），不影响正确性。
"""
from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

# 必须覆盖 pytest 默认的 python_files（"test_*.py" 与 "*_test.py" 两种）——
# 只 glob 前者的话，将来有人加一个 foo_test.py，全量 pytest 会收集它，而任何
# 分片都跑不到它，CI 却照样全绿。这是分片机制最容易踩的静默漏跑。
_TEST_GLOBS = ("tests/**/test_*.py", "tests/**/*_test.py")
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)


def test_files(root: Path) -> tuple[str, ...]:
    """仓库内全部测试文件（相对 root 的 posix 路径，已排序）。"""
    found = {
        p.relative_to(root).as_posix()
        for pattern in _TEST_GLOBS
        for p in root.glob(pattern)
    }
    return tuple(sorted(found))


def count_tests(path: Path) -> int:
    """文件里 ``def test_`` 的个数（用例数的廉价近似，见模块 docstring）。"""
    return len(_TEST_DEF.findall(path.read_text(encoding="utf-8")))


def plan_shards(
    weighted: Sequence[tuple[str, int]], shards: int
) -> tuple[tuple[str, ...], ...]:
    """贪心分片：按权重降序，每个文件分给当前负载最轻的片。

    纯函数、确定性。返回每片的文件元组（片内按路径排序，便于人读日志）。
    """
    if shards < 1:
        raise ValueError(f"片数必须 >= 1，收到 {shards}")
    buckets: list[list[str]] = [[] for _ in range(shards)]
    loads = [0] * shards
    for path, weight in sorted(weighted, key=lambda kv: (-kv[1], kv[0])):
        lightest = loads.index(min(loads))
        buckets[lightest].append(path)
        loads[lightest] += weight
    return tuple(tuple(sorted(bucket)) for bucket in buckets)


def shard_for(shard: int, shards: int, root: Path) -> tuple[str, ...]:
    """第 shard 片（1-based）要跑的测试文件。"""
    if not 1 <= shard <= shards:
        raise ValueError(f"片号必须在 1..{shards} 之间，收到 {shard}")
    files = test_files(root)
    weighted = tuple((f, count_tests(root / f)) for f in files)
    return plan_shards(weighted, shards)[shard - 1]


def main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    """CLI：``ci_shard.py <片号 1-based> <片数>``，打印该片的文件（空格分隔）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("用法: ci_shard.py <片号 1-based> <片数>", file=sys.stderr)
        raise SystemExit(2)
    try:
        shard, shards = int(args[0]), int(args[1])
        files = shard_for(shard, shards, root if root is not None else Path.cwd())
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not files:
        # 空片 = 这个 job「零测试通过」——分片最危险的静默失败。必须非零退出，
        # 不能只打个警告然后 return 0（那样 CI 会全绿）。
        print(f"错误: 第 {shard}/{shards} 片没有任何测试文件", file=sys.stderr)
        raise SystemExit(1)
    print(" ".join(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
