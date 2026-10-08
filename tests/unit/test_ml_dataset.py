"""research/ml/dataset.py 单测：前瞻标签、按日期切分、walk-forward。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.research.ml.dataset import (
    Dataset,
    DatasetConfig,
    _add_months,
    build_dataset,
    walk_forward_splits,
)

SYMS = [f"S{i:02d}" for i in range(12)]


def make_panel(n_days: int = 40, seed: int = 7) -> pl.DataFrame:
    """12 只标的 × n_days 的长表：动量特征与未来收益正相关。"""
    rng = np.random.default_rng(seed)
    rows = []
    for s in SYMS:
        px = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n_days)))
        mom = np.zeros(n_days)
        if n_days > 5:
            mom[5:] = px[5:] / px[:-5] - 1.0
        for i in range(n_days):
            rows.append({"trade_date": date(2024, 1, 1) + timedelta(days=i),
                         "symbol": s, "close": px[i], "mom": mom[i]})
    return pl.DataFrame(rows)


# ---------- 配置 ----------

def test_label_col_and_defaults():
    cfg = DatasetConfig(features=["mom"])
    assert cfg.label_col() == "fwd_ret_5"
    cfg2 = DatasetConfig(features=["mom"], label_horizon=1)
    assert cfg2.label_col() == "fwd_ret_1"


# ---------- build_dataset ----------

def test_build_dataset_missing_feature_raises():
    with pytest.raises(KeyError, match="特征列不存在"):
        build_dataset(make_panel(), DatasetConfig(features=["nope"]))


def test_build_dataset_basic_and_summary():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"], label_horizon=5))
    assert isinstance(ds, Dataset)
    assert "fwd_ret_5" in ds.df.columns
    # 最后 horizon 天无前瞻收益被丢掉；日期升序
    assert len(ds.dates) > 0
    assert ds.dates == sorted(ds.dates)
    s = ds.summary()
    assert s["label"] == "fwd_ret_5"
    assert s["features"] == 1
    assert s["symbols"] == 12
    assert s["days"] == len(ds.dates)
    assert s["rows"] == len(ds.df)
    assert s["start"] == ds.dates[0] and s["end"] == ds.dates[-1]


def test_build_dataset_clip_label():
    raw = make_panel()
    ds_clip = build_dataset(raw, DatasetConfig(features=["mom"], clip_label=0.05))
    ds_raw = build_dataset(raw, DatasetConfig(features=["mom"], clip_label=None))
    m = ds_clip.df["fwd_ret_5"].abs().max()
    assert m <= 0.05 + 1e-12
    assert ds_raw.df["fwd_ret_5"].abs().max() >= m


def test_build_dataset_min_samples_filter():
    # 只保留 5 只标的 → 全部日子都低于 min_samples_per_day=10，输出为空
    small = make_panel().filter(pl.col("symbol").is_in(SYMS[:5]))
    ds = build_dataset(small, DatasetConfig(features=["mom"], min_samples_per_day=10))
    assert len(ds.df) == 0


def test_build_dataset_keepna():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"], dropna=False))
    # 不 dropna 时最后几天标签为 null 也保留
    assert ds.df["fwd_ret_5"].null_count() > 0


# ---------- 切分与取数 ----------

def test_slice_by_str_dates():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"]))
    part = ds.slice("2024-01-05", "2024-01-10")
    d = part["trade_date"].unique().to_list()
    assert min(d) == date(2024, 1, 5) and max(d) == date(2024, 1, 10)


def test_split_three_parts_disjoint():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"]))
    tr, va, te = ds.split(date(2024, 1, 20), date(2024, 1, 28))
    assert tr["trade_date"].max() == date(2024, 1, 20)
    assert va["trade_date"].min() == date(2024, 1, 21)
    assert va["trade_date"].max() == date(2024, 1, 28)
    assert te["trade_date"].min() == date(2024, 1, 29)


def test_xy_values():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"]))
    X, y, d = ds.xy(ds.df)
    assert X.shape[1] == 1 and X.shape[0] == len(ds.df)
    assert len(y) == len(d) == X.shape[0]
    assert np.isfinite(X).all() and np.isfinite(y).all()


def test_xy_rank_label():
    """label_rank=True：截面排名 / 当日非空数 → (0,1] 归一化。"""
    ds = build_dataset(make_panel(), DatasetConfig(
        features=["mom"], label_rank=True))
    _, yr, _ = ds.xy(ds.df)
    assert yr.min() > 0 and yr.max() <= 1.0 + 1e-9


def test_xy_fillna_feature():
    # 特征列带 null：dropna=False 时应被填 0 而不是 NaN
    df = make_panel().with_columns(
        pl.when(pl.col("trade_date") == date(2024, 1, 3))
        .then(None).otherwise(pl.col("mom")).alias("mom"))
    ds = build_dataset(df, DatasetConfig(features=["mom"], dropna=False))
    X, y, _ = ds.xy(ds.df)
    assert np.isfinite(X).all()


def test_properties():
    ds = build_dataset(make_panel(), DatasetConfig(features=["mom"]))
    assert ds.features == ["mom"]
    assert ds.label == "fwd_ret_5"


# ---------- walk-forward ----------

def test_walk_forward_empty():
    assert walk_forward_splits([]) == []


def test_walk_forward_basic():
    dates = [date(2020, 1, 1) + timedelta(days=i * 30) for i in range(60)]  # ~5 年
    out = walk_forward_splits(dates, train_months=12, valid_months=3,
                              test_months=3, step_months=3)
    assert len(out) > 1
    first, second = out[0], out[1]
    assert first["train"][0] == date(2020, 1, 1)
    # 每期 train 起点按 step 前移
    assert second["train"][0] > first["train"][0]
    for o in out:
        assert o["valid"][0] == o["train"][1]
        assert o["test"][0] == o["valid"][1]
        assert o["test"][1] > o["test"][0]


def test_walk_forward_short_data():
    dates = [date(2024, 1, 1), date(2024, 2, 1)]
    assert walk_forward_splits(dates) == []


def test_walk_forward_default_matches_frozen_baseline():
    """回归保护：默认参数（purge=embargo=0）必须与改动前**逐元素一致**。

    期望值是加 purge/embargo **之前**跑出来的原始输出，逐字冻结在此。
    只要默认路径被动过，这条就会红。
    """
    dates = [date(2020, 1, 1) + timedelta(days=i * 30) for i in range(48)]
    out = walk_forward_splits(dates, train_months=12, valid_months=3,
                              test_months=3, step_months=3)
    expected = [
        {'train': ('2020-01-01', '2021-01-01'), 'valid': ('2021-01-01', '2021-04-01'),
         'test': ('2021-04-01', '2021-07-01')},
        {'train': ('2020-04-01', '2021-04-01'), 'valid': ('2021-04-01', '2021-07-01'),
         'test': ('2021-07-01', '2021-10-01')},
        {'train': ('2020-07-01', '2021-07-01'), 'valid': ('2021-07-01', '2021-10-01'),
         'test': ('2021-10-01', '2022-01-01')},
        {'train': ('2020-10-01', '2021-10-01'), 'valid': ('2021-10-01', '2022-01-01'),
         'test': ('2022-01-01', '2022-04-01')},
        {'train': ('2021-01-01', '2022-01-01'), 'valid': ('2022-01-01', '2022-04-01'),
         'test': ('2022-04-01', '2022-07-01')},
        {'train': ('2021-04-01', '2022-04-01'), 'valid': ('2022-04-01', '2022-07-01'),
         'test': ('2022-07-01', '2022-10-01')},
        {'train': ('2021-07-01', '2022-07-01'), 'valid': ('2022-07-01', '2022-10-01'),
         'test': ('2022-10-01', '2023-01-01')},
        {'train': ('2021-10-01', '2022-10-01'), 'valid': ('2022-10-01', '2023-01-01'),
         'test': ('2023-01-01', '2023-04-01')},
        {'train': ('2022-01-01', '2023-01-01'), 'valid': ('2023-01-01', '2023-04-01'),
         'test': ('2023-04-01', '2023-07-01')},
        {'train': ('2022-04-01', '2023-04-01'), 'valid': ('2023-04-01', '2023-07-01'),
         'test': ('2023-07-01', '2023-10-01')},
    ]
    got = [{k: (str(v[0]), str(v[1])) for k, v in o.items()} for o in out]
    assert got == expected


def test_walk_forward_purge_embargo_creates_gap_by_index():
    """purge/embargo 生效后，用具体索引证明段落之间确实留了缺口。"""
    grid = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(60)]
    w = walk_forward_splits(grid, train_months=6, valid_months=2, test_months=2,
                            step_months=3, purge_bars=3, embargo_bars=1)[0]
    idx = {d: i for i, d in enumerate(grid)}
    # purge=3 剪训练尾：base train end 2024-07-01(idx 26) → 前移 3 根
    assert w["train"][1] == date(2024, 6, 10) and idx[w["train"][1]] == 23
    # embargo=1 再推验证头：base 验证首根 = 2024-07-08(idx 27)，再 +1
    assert w["valid"][0] == date(2024, 7, 15) and idx[w["valid"][0]] == 28
    # 训练末端与验证起点之间恰好 3+1 根「谁都不属于」的隔离带
    assert idx[w["valid"][0]] - idx[w["train"][1]] - 1 == 3 + 1
    # 验证末端与测试起点之间同样是 3+1 根
    assert idx[w["test"][0]] - idx[w["valid"][1]] - 1 == 3 + 1


def test_walk_forward_negative_purge_embargo_raises():
    grid = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(60)]
    with pytest.raises(ValueError, match="不能为负"):
        walk_forward_splits(grid, purge_bars=-1)
    with pytest.raises(ValueError, match="不能为负"):
        walk_forward_splits(grid, embargo_bars=-2)


def test_walk_forward_purge_that_empties_segment_raises():
    grid = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(60)]
    with pytest.raises(ValueError, match="剪空"):
        walk_forward_splits(grid, train_months=1, valid_months=1, test_months=1,
                            step_months=1, purge_bars=999)


# ---------- purged/embargoed CV：与前瞻标签结合 ----------

def test_purged_training_label_ends_before_test_start():
    """标签是前瞻 h 日收益：purge=h 后训练样本的标签终点必须早于测试起点。

    这是 A2 的核心断言 —— 不加 purge 时训练段最后一根的标签会落在验证/测试
    首日，训练集「提前看到」测试期收益。
    """
    h = 5
    ds = build_dataset(make_panel(n_days=400),
                       DatasetConfig(features=["mom"], label_horizon=h))
    grid = ds.dates
    idx = {d: i for i, d in enumerate(grid)}
    win = walk_forward_splits(grid, train_months=6, valid_months=2, test_months=2,
                              step_months=3, purge_bars=h, embargo_bars=1)[0]
    tr, va, te = ds.split_window(win)
    assert len(tr) and len(va) and len(te)
    tr_end = tr["trade_date"].max()
    va_start = va["trade_date"].min()
    te_start = te["trade_date"].min()
    # 训练段最后一根的标签取的是 grid[tr_end + h] 的收盘价 —— 必须落在验证段之前
    assert tr_end == win["train"][1]
    assert grid[idx[tr_end] + h] < va_start
    # 验证段最后一根的标签终点同样早于测试段起点
    va_end = va["trade_date"].max()
    assert grid[idx[va_end] + h] < te_start


def test_purge_removes_exactly_h_train_bars_and_split_window_skips_gap():
    """purge=h 恰好剪掉训练段尾部 h 根；缺口用 split_window 才不会被吞回。"""
    h = 5
    ds = build_dataset(make_panel(n_days=400),
                       DatasetConfig(features=["mom"], label_horizon=h))
    grid = ds.dates
    idx = {d: i for i, d in enumerate(grid)}
    common = dict(train_months=6, valid_months=2, test_months=2, step_months=3)
    base = walk_forward_splits(grid, **common)[0]
    purged = walk_forward_splits(grid, **common, purge_bars=h, embargo_bars=1)[0]
    assert idx[base["train"][1]] - idx[purged["train"][1]] == h

    # split_window 两个端点都用：验证段正好从隔离带之后开始
    _, va_purged, _ = ds.split_window(purged)
    assert va_purged["trade_date"].min() == purged["valid"][0]
    # 而连续边界的 split() 会把隔离带（purged 验证头之前的缺口）并回后段
    _, va_naive, _ = ds.split(purged["train"][1], purged["valid"][1])
    assert va_naive["trade_date"].min() < purged["valid"][0]
    assert va_naive["trade_date"].min() == grid[idx[purged["train"][1]] + 1]



def test_add_months_edge():
    assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)   # 月末截断 + 闰年
    assert _add_months(date(2023, 11, 15), 2) == date(2024, 1, 15)  # 跨年
    assert _add_months(date(2024, 3, 31), 11) == date(2025, 2, 28)


# ----------------------------------------------------------- 特征解析退化分支

def test_resolve_feature_handles_unparsable_suffix():
    """``mom_x`` 这类前缀对但后缀不是数字的名字，不能当成窗口特征。"""
    from lquant.research.ml.panel import resolve_feature

    df = make_panel()
    with pytest.raises(Exception, match="无法解析特征"):
        resolve_feature(df, "MA_x")
    # 窗口 <= 0 同样不算（否则 rolling(0) 会在 Polars 里炸）
    with pytest.raises(Exception, match="无法解析特征"):
        resolve_feature(df, "MA_0")
