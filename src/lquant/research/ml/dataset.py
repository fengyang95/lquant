"""Dataset：特征、标签、时序切分。

切分必须按**日期边界**，绝不能随机打乱。
随机切分会让训练集里出现「未来」样本，模型在验证集上表现极好，
上线后一塌糊涂 —— 这是 ML 选股最常见也最致命的错误。

标准做法是三段滚动：
train → valid（调参）→ test（只看一次）
实盘用 walk-forward：每隔一段时间把窗口整体前移重训。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from lquant.factors.evaluate.returns import forward_return

__all__ = ["DatasetConfig", "Dataset", "build_dataset", "walk_forward_splits"]


@dataclass
class DatasetConfig:
    features: list[str]
    label_horizon: int = 5
    price_col: str = "close"
    date_col: str = "trade_date"
    symbol_col: str = "symbol"
    clip_label: float | None = 0.2          # 极端收益截尾，避免模型被少数样本带偏
    label_rank: bool = False                # 用截面排名作标签，对异常值更稳健
    dropna: bool = True
    min_samples_per_day: int = 10
    #: 可 fit 的特征处理器声明（见 research/ml/processor.py）。
    #: 例：[{"kind": "clip", "lower": 0.01}, {"kind": "standardize"}]
    #: **一律只在训练段 fit**，valid/test 只 transform —— 不填则不处理。
    processors: list[dict] | None = None

    def label_col(self) -> str:
        return f"fwd_ret_{self.label_horizon}"


@dataclass
class Dataset:
    df: pl.DataFrame
    cfg: DatasetConfig
    dates: list[date] = field(default_factory=list)

    @property
    def features(self) -> list[str]:
        return self.cfg.features

    @property
    def label(self) -> str:
        return self.cfg.label_col()

    def slice(self, start: date | str | None = None,
              end: date | str | None = None) -> pl.DataFrame:
        d = self.df
        if start is not None:
            d = d.filter(pl.col(self.cfg.date_col) >= _as_date(start))
        if end is not None:
            d = d.filter(pl.col(self.cfg.date_col) <= _as_date(end))
        return d

    def split(self, train_end, valid_end, test_end=None):
        """按日期切成 (train, valid, test)。"""
        return (self.slice(end=train_end),
                self.slice(start=_next_day(train_end), end=valid_end),
                self.slice(start=_next_day(valid_end), end=test_end))

    def build_processor(self):
        """按 ``cfg.processors`` 构造（未拟合的）处理器；无声明返回 None。"""
        if not self.cfg.processors:
            return None
        from lquant.research.ml.processor import Pipeline, make_processor

        procs = [make_processor(s) for s in self.cfg.processors]
        return procs[0] if len(procs) == 1 else Pipeline(procs)

    def fit_processor(self, train: pl.DataFrame):
        """**只在训练段** fit 处理器，返回 (processor, transformed_train)。

        这是防泄漏的关键入口：调用方拿到 processor 后只能 ``transform``
        valid/test，绝不能对它们再 fit。``processor is None`` 表示未声明。
        """
        proc = self.build_processor()
        if proc is None:
            return None, train
        proc.fit(train, features=self.features)
        return proc, proc.transform(train)

    def split_processed(self, train_end, valid_end, test_end=None):
        """切分 + 只在 train 上 fit 处理器 → (train, valid, test, processor)。

        valid/test 只做 transform；``fwd_ret`` 标签列原样保留（处理器只动特征列）。
        """
        train, valid, test = self.split(train_end, valid_end, test_end)
        proc, train_p = self.fit_processor(train)
        if proc is None:
            return train, valid, test, None
        return train_p, proc.transform(valid), proc.transform(test), proc

    def xy(self, part: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """返回 (X, y, 日期数组)。y 为前瞻收益或截面排名。"""
        d = part.drop_nulls([self.label]) if self.cfg.dropna else part
        X = np.column_stack([
            d[f].cast(pl.Float64, strict=False).fill_null(0.0).to_numpy()
            for f in self.features
        ])
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        if self.cfg.label_rank:
            # Series.rank()/.count() 是聚合不是窗口 —— 必须用表达式列 .over
            ranked = d.with_columns(
                (pl.col(self.label).rank("average")
                 / pl.col(self.label).count()).over(self.cfg.date_col)
                .alias("__rank"))
            y = ranked["__rank"].to_numpy()
        else:
            y = d[self.label].cast(pl.Float64).to_numpy()
        return X, np.nan_to_num(y, nan=0.0), d[self.cfg.date_col].to_numpy()

    def summary(self) -> dict:
        return {
            "rows": len(self.df),
            "features": len(self.features),
            "label": self.label,
            "start": self.dates[0] if self.dates else None,
            "end": self.dates[-1] if self.dates else None,
            "symbols": self.df[self.cfg.symbol_col].n_unique(),
            "days": self.df[self.cfg.date_col].n_unique(),
        }


def _as_date(v) -> date:
    if isinstance(v, str):
        return date.fromisoformat(v)
    return v


def _next_day(v) -> date:
    from datetime import timedelta
    return _as_date(v) + timedelta(days=1)


def build_dataset(df: pl.DataFrame, cfg: DatasetConfig) -> Dataset:
    """从行情长表构建数据集：补前瞻收益 → 截尾 → 丢缺失 → 过滤稀疏日。"""
    missing = [f for f in cfg.features if f not in df.columns]
    if missing:
        raise KeyError(f"特征列不存在: {missing}")

    d = forward_return(df, price_col=cfg.price_col, periods=[cfg.label_horizon],
                       by=cfg.symbol_col, date_col=cfg.date_col)
    label = cfg.label_col()

    if cfg.clip_label is not None:
        d = d.with_columns(pl.col(label).clip(-cfg.clip_label, cfg.clip_label))

    if cfg.dropna:
        d = d.drop_nulls([label, *cfg.features])

    # 样本太少的日子截面统计不可靠，直接剔掉
    d = d.filter(pl.len().over(cfg.date_col) >= cfg.min_samples_per_day)

    dates = sorted(d[cfg.date_col].unique().to_list())
    return Dataset(df=d, cfg=cfg, dates=dates)


def walk_forward_splits(dates: list[date], train_months: int = 24,
                        valid_months: int = 6, test_months: int = 6,
                        step_months: int = 6):
    """滚动前移的 (train, valid, test) 日期边界序列。

    这是唯一能反映实盘的验证方式：每一期只用当时可得的数据训练，
    预测随后的一段时间，再整体前移。
    """

    if not dates:
        return []
    start, end = dates[0], dates[-1]
    out = []
    cur = start
    while True:
        tr_s, tr_e = cur, _add_months(cur, train_months)
        va_e = _add_months(tr_e, valid_months)
        te_e = _add_months(va_e, test_months)
        if te_e > end:
            break
        out.append({"train": (tr_s, tr_e), "valid": (tr_e, va_e), "test": (va_e, te_e)})
        cur = _add_months(cur, step_months)
    return out


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)
