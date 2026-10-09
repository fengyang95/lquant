"""CPCV / purged K-fold：切分的**隔离带**必须真的留出来。

purge/embargo 是这类切分唯一有技术含量的部分 —— 区间边界算错一格，
样本外指标就系统性偏乐观，而表面上「一切正常」。所以这里的断言盯着边界。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from lquant.research.ml.cv import (
    PurgedSplit,
    assign_paths,
    combinatorial_purged_splits,
    purged_kfold_splits,
)


def _dates(n: int) -> list[date]:
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


def test_kfold_test_blocks_are_disjoint_and_cover_everything():
    dates = _dates(50)
    splits = purged_kfold_splits(dates, n_splits=5)
    seen = [d for s in splits for lo, hi in s.test
            for d in dates[dates.index(lo):dates.index(hi) + 1]]
    assert sorted(seen) == sorted(dates)          # 每段都被测到，且不重复


def test_train_and_test_never_overlap():
    dates = _dates(50)
    for s in purged_kfold_splits(dates, n_splits=5, purge_bars=3, embargo_bars=2):
        train, test = s.masks(dates)
        assert not (train & test).any()
        assert test.any()


def test_purge_trims_the_tail_before_test():
    """purge=N：测试段之前 N 根训练样本必须去掉（标签窗口会越界）。

    用**中间**那段作测试：第一段之前没有训练块，purge 无从体现。
    """
    dates = _dates(20)
    plain = purged_kfold_splits(dates, n_splits=4)[1]
    purged = purged_kfold_splits(dates, n_splits=4, purge_bars=3)[1]
    assert purged.n_train_days(dates) == plain.n_train_days(dates) - 3
    gap = dates.index(purged.test[0][0]) - dates.index(plain.test[0][0])
    assert gap == 0                                   # 测试段本身不动
    # 紧邻测试段之前的那段训练，尾部少了 3 根
    before = [sp for sp in plain.train if sp[1] < plain.test[0][0]]
    after = [sp for sp in purged.train if sp[1] < purged.test[0][0]]
    assert before and after
    assert dates.index(before[-1][1]) - dates.index(after[-1][1]) == 3


def test_embargo_trims_the_head_after_test():
    dates = _dates(20)
    plain = purged_kfold_splits(dates, n_splits=4)[0]
    emb = purged_kfold_splits(dates, n_splits=4, embargo_bars=2)[0]
    assert emb.n_train_days(dates) == plain.n_train_days(dates) - 2
    # 第一段训练块整体后移 2 根
    assert (emb.train[0][0] - plain.train[0][0]).days == 2


def test_purge_and_embargo_apply_on_both_sides_of_a_middle_test_block():
    """中间测试段：前一块尾部 purge、后一块头部 embargo，两者互不干扰。"""
    dates = _dates(30)
    splits = purged_kfold_splits(dates, n_splits=5, purge_bars=2, embargo_bars=2)
    mid = splits[2]                                   # 第 3 块作测试
    train, test = mid.masks(dates)
    idx = [i for i in range(len(dates)) if test[i]]
    lo, hi = min(idx), max(idx)
    # 测试段紧邻处各空 2 根
    assert not train[lo - 2:lo].any() and not train[hi + 1:hi + 3].any()


def test_combinatorial_splits_cover_all_combinations():
    dates = _dates(60)
    splits = combinatorial_purged_splits(dates, n_splits=5, n_test_splits=2)
    assert len(splits) == 10                          # C(5,2)
    assert len({s.test_blocks for s in splits}) == 10
    for s in splits:
        assert len(s.test_blocks) == 2
        train, test = s.masks(dates)
        assert not (train & test).any()


def test_paths_test_each_block_equally_often():
    """每条路径里每一段被测的次数相同 —— 否则路径之间不可比。"""
    splits = combinatorial_purged_splits(_dates(60), n_splits=6, n_test_splits=2)
    paths = {s.path for s in splits}
    assert paths == {0, 1}
    for p in paths:
        counts = Counter(b for s in splits if s.path == p for b in s.test_blocks)
        assert len(set(counts.values())) == 1
        assert set(counts) == set(range(6))            # 每段都测到


def test_purge_embargo_keep_train_and_test_apart_end_to_end():
    """不变量：训练样本不得落在任何测试段的 purge 窗口内或 embargo 窗口内。"""
    dates = _dates(80)
    purge, embargo = 5, 1
    for s in combinatorial_purged_splits(dates, n_splits=4, n_test_splits=2,
                                         purge_bars=purge, embargo_bars=embargo):
        train, test = s.masks(dates)
        assert not (train & test).any()
        for i in range(len(dates)):
            if not train[i]:
                continue
            # 之后 purge 根内若有测试样本 → 这根训练的标签会越界
            assert not any(test[i + 1:i + 1 + purge]), f"{dates[i]} 离测试段太近（purge）"
            # 之前 embargo 根内若有测试样本 → 特征窗口重叠
            assert not any(test[max(0, i - embargo):i]), f"{dates[i]} 离测试段太近（embargo）"
        assert test.any() and train.any()


def test_purged_split_label_and_masks_ignore_unknown_dates():
    sp = PurgedSplit(train=((date(2026, 1, 5), date(2026, 1, 6)),),
                     test=((date(2026, 1, 7), date(2026, 1, 7)),),
                     test_blocks=(1,))
    assert sp.label == "test=1"
    train, test = sp.masks([date(2026, 1, 5), date(2026, 1, 6)])
    assert train.tolist() == [True, True] and not test.any()   # 测试段不在序列里


def test_assign_paths_validates_argument():
    with pytest.raises(ValueError, match="n_test_splits"):
        assign_paths([], 0)


@pytest.mark.parametrize("kwargs,match", [
    ({"n_splits": 1}, "n_splits"),
    ({"n_splits": 5, "purge_bars": -1}, "不能为负"),
    ({"n_splits": 5, "embargo_bars": -1}, "不能为负"),
])
def test_kfold_rejects_bad_params(kwargs, match):
    with pytest.raises(ValueError, match=match):
        purged_kfold_splits(_dates(20), **kwargs)


def test_cpcv_rejects_bad_params():
    with pytest.raises(ValueError, match="n_test_splits"):
        combinatorial_purged_splits(_dates(30), n_splits=4, n_test_splits=4)
    with pytest.raises(ValueError, match="少于切分段数"):
        combinatorial_purged_splits(_dates(3), n_splits=5, n_test_splits=2)
