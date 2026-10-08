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


# ---------- 可交易性屏蔽 + 可执行标签口径 ----------

def _flagged_panel() -> pl.DataFrame:
    """3 只标的 × 6 天：S01 第 2 天停牌、S02 第 3 天一字板、S00 全程正常。

    信号日 = 第 1 天（t=1），入场 = 第 2 天（t+1）—— 屏蔽必须按**入场日**判定。
    """
    from lquant.data.quality.flags import SUSPENDED

    d0 = date(2026, 1, 5)
    rows = []
    for k, s in enumerate(["S00", "S01", "S02"]):
        px = 10.0 + k
        for i in range(6):
            close = px * (1.0 + 0.01 * i)
            rows.append({
                "trade_date": d0 + timedelta(days=i), "symbol": s,
                "close": close, "open": close, "high": close, "low": close,
                "pre_close": close / (1.0 + 0.01 * i) if i else close,
                "mom": float(i), "quality_flags": 0,
            })
    df = pl.DataFrame(rows)
    # S01 第 2 天停牌
    susp_day = d0 + timedelta(days=2)
    df = df.with_columns(
        quality_flags=pl.when((pl.col("symbol") == "S01")
                              & (pl.col("trade_date") == susp_day))
        .then(pl.lit(SUSPENDED, dtype=pl.Int32))
        .otherwise(pl.col("quality_flags")))
    # S02 第 3 天一字板（涨停）：high == low 且较昨收 +10%
    zday = d0 + timedelta(days=3)
    df = df.with_columns(
        close=pl.when((pl.col("symbol") == "S02") & (pl.col("trade_date") == zday))
        .then(pl.col("pre_close") * 1.10)
        .otherwise(pl.col("close")))
    return df.with_columns(
        open=pl.col("close"), high=pl.col("close"), low=pl.col("close"))


def test_tradability_mask_drops_unbuyable_samples():
    df = _flagged_panel()
    cfg = DatasetConfig(features=["mom"], label_horizon=2, tradability_mask=True,
                        clip_label=None, min_samples_per_day=1)
    ds = build_dataset(df, cfg)
    assert ds.mask_report["applied"] is True
    assert ds.mask_report["dropped"] > 0
    assert ds.mask_report["reasons"]["停牌"] == 1     # S01 的入场日
    assert ds.mask_report["reasons"]["一字板"] == 1   # S02 的入场日
    # 被剔的正好是「入场那天买不到」的那两个样本（信号日 = 入场日前一天）
    d0 = date(2026, 1, 5)
    kept = {(r["symbol"], r["trade_date"]) for r in ds.df.to_dicts()}
    assert ("S01", d0 + timedelta(days=1)) not in kept
    assert ("S02", d0 + timedelta(days=2)) not in kept
    # 停牌那天本身仍可作为**信号**（只是不能当入场日）
    assert ("S01", d0 + timedelta(days=2)) in kept


def test_mask_disabled_is_explicit():
    df = _flagged_panel()
    ds = build_dataset(df, DatasetConfig(features=["mom"], label_horizon=2,
                                         tradability_mask=False))
    assert ds.mask_report["applied"] is False
    assert "显式关闭" in ds.mask_report["note"]


def test_mask_without_flags_column_is_reported_not_silent():
    """面板没有 quality_flags/OHLC → 空操作，但报告必须写明（不是「已检查」）。"""
    plain = make_panel(20).select(["trade_date", "symbol", "close", "mom"])
    ds = build_dataset(plain, DatasetConfig(features=["mom"], label_horizon=2))
    assert ds.mask_report["applied"] is False
    assert "未做可交易性屏蔽" in ds.mask_report["note"]


def test_executable_label_uses_next_open():
    """entry_price_col='open' → label = close[t+h]/open[t+1]-1（与 T+1 撮合一致）。"""
    d0 = date(2026, 1, 5)
    rows = []
    for i in range(6):
        rows.append({"trade_date": d0 + timedelta(days=i), "symbol": "S00",
                     "close": 10.0 + i, "open": 10.0 + i + 0.5, "mom": float(i)})
    df = pl.DataFrame(rows)
    ds = build_dataset(df, DatasetConfig(features=["mom"], label_horizon=2,
                                         entry_price_col="open", clip_label=None,
                                         tradability_mask=False,
                                         min_samples_per_day=1))
    r0 = ds.df.filter(pl.col("trade_date") == d0).row(0, named=True)
    # close[t+2]=12.0 / open[t+1]=11.5 - 1
    assert abs(r0["fwd_ret_2"] - (12.0 / 11.5 - 1.0)) < 1e-12
    assert ds.cfg.label_kind() == "close[t+2]/open[t+1]"
    assert ds.summary()["label_kind"] == "close[t+2]/open[t+1]"
    assert ds.summary()["mask"]["applied"] is False


def test_default_label_kind_is_close_to_close():
    ds = build_dataset(make_panel(30), DatasetConfig(features=["mom"],
                                                     label_horizon=3))
    assert ds.summary()["label_kind"] == "close[t+3]/close[t+0]"
