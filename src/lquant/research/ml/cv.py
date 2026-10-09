"""CPCV：组合式 purged & embargoed 交叉验证（Lopez de Prado, AFML ch.7）。

``dataset.walk_forward_splits`` 给的是**单路径**滚动切分：每一期只测一段，
样本外指标是「一条路径上的一个数」。问题在于这个数**对切分方式很敏感** ——
换一个起点、换一次切法，结论可能就翻了，而你无从知道。

CPCV 换成组合切分：把时间轴切成 N 段，任取 k 段做测试，得到 C(N, k) 组；
把组合按规则摊到 k 条路径上，每条路径里每一段都被测过一遍 —— 于是你拿到的是
**绩效分布**而不是一个点，可以直接回答「这个策略是不是挑切分挑出来的」。

purge / embargo 的口径与 ``walk_forward_splits`` **完全一致**（同一套语义写在
两处迟早分叉，所以这里按同一份定义实现）：

- **purge**：训练段的**尾部**丢 N 根。标签是前瞻 h 日收益，不丢的话训练样本
  的标签窗口会覆盖到测试期 → 样本外指标系统性偏乐观。
- **embargo**：测试段**之后**再丢 N 根。相邻 bar 的特征窗口（20 日均线之类）
  与波动自相关会让边界处的评估样本与训练样本高度相似，不留隔离带仍会高估。

只裁剪**紧邻**测试段的训练块即可：连续分块下，标签会越界的只有「测试段前一块
的尾部」和「测试段后一块的头部」，其余训练块与测试期在时间上完全隔开。
把整段训练集按固定根数统一裁一遍是既浪费又不对的做法。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from itertools import combinations

import numpy as np

__all__ = ["PurgedSplit", "assign_paths", "combinatorial_purged_splits",
           "purged_kfold_splits"]


@dataclass(frozen=True)
class PurgedSplit:
    """一次切分：训练段与测试段各自是若干**闭区间**（purge/embargo 会留缺口）。"""

    train: tuple[tuple[date, date], ...]
    test: tuple[tuple[date, date], ...]
    test_blocks: tuple[int, ...] = ()
    #: 属于哪条回测路径（``assign_paths`` 填；未分配时为 -1）
    path: int = -1

    @property
    def label(self) -> str:
        return "test=" + ",".join(str(b) for b in self.test_blocks)

    def masks(self, dates: list[date]) -> tuple[np.ndarray, np.ndarray]:
        """按给定日期序列生成 (train_mask, test_mask) 布尔数组。

        消费方（训练循环）直接用掩码索引样本即可 —— 不必自己实现区间包含判断，
        也就不会出现「训练集按区间、测试集按掩码」这种两套口径。
        """
        index = {d: i for i, d in enumerate(dates)}
        train = np.zeros(len(dates), dtype=bool)
        test = np.zeros(len(dates), dtype=bool)
        for spans, target in ((self.train, train), (self.test, test)):
            for lo, hi in spans:
                i, j = index.get(lo), index.get(hi)
                if i is None or j is None or j < i:
                    continue
                target[i:j + 1] = True
        return train, test

    def n_train_days(self, dates: list[date]) -> int:
        return int(self.masks(dates)[0].sum())


def _blocks(n: int, n_splits: int) -> list[tuple[int, int]]:
    """把 [0, n) 切成 n_splits 个**尽量等长**的连续块（索引闭区间）。

    余数分给前几块（``divmod``），保证「块数恰好等于 n_splits」——
    用 ``np.array_split`` 也行，但它对极短序列会产出空块，
    而空块会让后面的 purge 计算得到负数区间。
    """
    if n_splits < 2:
        raise ValueError(f"n_splits 至少为 2，收到 {n_splits}")
    if n < n_splits:
        raise ValueError(f"交易日数 {n} 少于切分段数 {n_splits}，无法切分")
    size, extra = divmod(n, n_splits)
    out, cur = [], 0
    for b in range(n_splits):
        length = size + (1 if b < extra else 0)
        out.append((cur, cur + length - 1))
        cur += length
    return out


def _train_spans(blocks: list[tuple[int, int]], test_ids: set[int],
                 purge: int, embargo: int) -> list[tuple[int, int]]:
    """训练段的索引区间：去掉测试块，再对紧邻块做 purge / embargo。"""
    out = []
    for i, (s, e) in enumerate(blocks):
        if i in test_ids:
            continue
        if (i + 1) in test_ids:          # 紧接测试段之前 → 尾部 purge
            e -= purge
        if (i - 1) in test_ids:          # 紧接测试段之后 → 头部 embargo
            s += embargo
        if e >= s:
            out.append((s, e))
    return out


def _to_dates(spans: list[tuple[int, int]], dates: list[date]) -> tuple[tuple[date, date], ...]:
    return tuple((dates[s], dates[e]) for s, e in spans)


def purged_kfold_splits(dates: list[date], n_splits: int = 5,
                        purge_bars: int = 0,
                        embargo_bars: int = 0) -> list[PurgedSplit]:
    """purged & embargoed K-fold（顺序切分，每段各当一次测试）。

    这是 CPCV 的退化形态（k=1），保留它是因为很多场景只想要「K 折的
    purged 版本」，不需要组合那层。
    """
    _check(purge_bars, embargo_bars)
    blocks = _blocks(len(dates), n_splits)
    out = []
    for i in range(n_splits):
        train = _train_spans(blocks, {i}, purge_bars, embargo_bars)
        out.append(PurgedSplit(train=_to_dates(train, dates),
                               test=_to_dates([blocks[i]], dates),
                               test_blocks=(i,)))
    return out


def combinatorial_purged_splits(dates: list[date], n_splits: int = 6,
                                n_test_splits: int = 2, purge_bars: int = 0,
                                embargo_bars: int = 0) -> list[PurgedSplit]:
    """CPCV：全部 C(n_splits, n_test_splits) 组切分。

    组数随 n_splits 增长很快（N=6,k=2 → 15 组；N=8,k=2 → 28 组），
    每组都要重训一次模型 —— 这是**刻意的成本**：单条路径给你的那个夏普，
    本来就不知道是策略的还是一次切分的运气。
    """
    _check(purge_bars, embargo_bars)
    if not 1 <= n_test_splits < n_splits:
        raise ValueError(
            f"n_test_splits 必须落在 [1, n_splits)，收到 {n_test_splits}/{n_splits}")
    blocks = _blocks(len(dates), n_splits)
    out = []
    for combo in combinations(range(n_splits), n_test_splits):
        ids = set(combo)
        out.append(PurgedSplit(
            train=_to_dates(_train_spans(blocks, ids, purge_bars, embargo_bars), dates),
            test=_to_dates([blocks[i] for i in combo], dates),
            test_blocks=combo))
    return assign_paths(out, n_test_splits)


def assign_paths(splits: list[PurgedSplit], n_test_splits: int) -> list[PurgedSplit]:
    """把组合摊成 ``n_test_splits`` 条回测路径。

    规则：按「第一个测试块」分组，组内依次分到 0..k-1 条路径。这样每条路径里
    **每一段被测的次数相同**，路径之间的绩效才可比 —— 随便编号会让某条路径
    多测几段，比较就变成「谁的测试段更极端」而不是「谁更稳」。
    """
    if n_test_splits < 1:
        raise ValueError(f"n_test_splits 至少为 1，收到 {n_test_splits}")
    by_first: dict[int, int] = {}
    out = []
    for sp in splits:
        key = sp.test_blocks[0] if sp.test_blocks else -1
        idx = by_first.get(key, 0)
        by_first[key] = idx + 1
        out.append(replace(sp, path=idx % n_test_splits))
    return out


def _check(purge_bars: int, embargo_bars: int) -> None:
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError(
            f"purge_bars/embargo_bars 不能为负：purge={purge_bars}, "
            f"embargo={embargo_bars}")
