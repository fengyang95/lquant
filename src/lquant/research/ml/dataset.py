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
        """按日期切成 (train, valid, test)。

        只认「前一段结束、后一段从次日开始」的**连续**边界。要表达
        purge/embargo 留下的缺口请用 :meth:`split_window`。
        """
        return (self.slice(end=train_end),
                self.slice(start=_next_day(train_end), end=valid_end),
                self.slice(start=_next_day(valid_end), end=test_end))

    def split_window(self, window: dict):
        """按 ``walk_forward_splits`` 给出的**显式闭区间**切 (train, valid, test)。

        为什么不能继续用 :meth:`split`：purge/embargo 的意义就是让相邻两段之间
        留出一条**既不属于前段、也不属于后段**的隔离带。``split`` 只会把后段
        从「前段结束的次日」开始，缺口会被悄悄并进后段 —— 训练集又吃到了测试期
        的邻近样本，泄漏白防了。这里两个端点都用上，缺口被如实跳过。

        ``window`` 形如 ``{"train": (start, end), "valid": ..., "test": ...}``。
        """
        return tuple(self.slice(start=w[0], end=w[1])
                     for w in (window["train"], window["valid"], window["test"]))

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
                        step_months: int = 6,
                        purge_bars: int = 0, embargo_bars: int = 0):
    """滚动前移的 (train, valid, test) 日期边界序列。

    这是唯一能反映实盘的验证方式：每一期只用当时可得的数据训练，
    预测随后的一段时间，再整体前移。

    purge / embargo（默认全 0 = 与历史行为**逐元素一致**）：

    这里的标签是「前瞻 h 日收益」：t 日的样本要看到 t+h 日的价格才定型。
    于是只要 train 的最后一根紧贴 valid/test 的第一根，这根训练样本的标签
    就「见过」测试期了 —— 训练集与测试集在时间上重叠 h-1 期，样本外指标
    系统性偏乐观（越是 h 大越明显）。修复只需在切分处留缺口：

    - ``purge_bars``：在**每个拟合段**（train、valid）的尾部丢掉 N 根。
      取 ``N = h`` 时，train 最后一根的标签恰好止于 train 末端，严格早于
      下一段起点，训练样本的标签再也够不到验证/测试期。
    - ``embargo_bars``：在**每个评估段**（valid、test）的头部再丢掉 N 根。
      光有 purge 时两段仍然紧邻；相邻 bar 的特征窗口（如 20 日均线）、波动与
      成交自相关会让边界处的评估样本与训练样本高度相似，仍然高估泛化。
      留一条隔离带即可，经验取 ``1``（再多会明显浪费评估样本）。

    这就是 Lopez de Prado《Advances in Financial Machine Learning》里
    purged & embargoed CV 的口径；TSP（MIT）的实现也是「先留缺口、再取后段」。

    **返回值是带缺口的闭区间**：purge/embargo 生效后，后段的起点不再等于前段
    终点，缺口两端都不属于任何一段。消费方必须两个端点都用
    （``Dataset.split_window``）—— 只按 ``Dataset.split`` 的连续边界重建，会把
    缺口又并回训练集，等于没做。

    purge/embargo 以**交易日根数**计（真交易日历，不是日历日）。窗口短到被剪空
    时直接抛 :class:`ValueError`，不静默返回一个空的训练段。
    """
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError(
            f"purge_bars/embargo_bars 不能为负："
            f"purge={purge_bars}, embargo={embargo_bars}")
    if not dates:
        return []
    start, end = dates[0], dates[-1]
    # 只有真要做 purge/embargo 时才需要把月历边界吸附到真实交易日索引上；
    # 默认路径完全走原逻辑，保证与改动前逐元素一致。
    grid = sorted(set(dates)) if (purge_bars or embargo_bars) else []
    out = []
    cur = start
    while True:
        tr_s, tr_e = cur, _add_months(cur, train_months)
        va_e = _add_months(tr_e, valid_months)
        te_e = _add_months(va_e, test_months)
        if te_e > end:
            break
        if purge_bars or embargo_bars:
            train, valid, test = _purge_embargo_window(
                grid, tr_s, tr_e, va_e, te_e, purge_bars, embargo_bars)
        else:
            train, valid, test = (tr_s, tr_e), (tr_e, va_e), (va_e, te_e)
        out.append({"train": train, "valid": valid, "test": test})
        cur = _add_months(cur, step_months)
    return out


def _purge_embargo_window(grid: list[date], tr_s: date, tr_e: date,
                          va_e: date, te_e: date,
                          purge_bars: int, embargo_bars: int):
    """把月历边界吸附到交易日网格，再按根数剪出带缺口的三个闭区间。"""
    tr_start = _first_idx_ge(grid, tr_s)
    tr_end = _last_idx_le(grid, tr_e)
    va_start = _first_idx_gt(grid, tr_e)
    va_end = _last_idx_le(grid, va_e)
    te_start = _first_idx_gt(grid, va_e)
    te_end = _last_idx_le(grid, te_e)
    if None in (tr_start, tr_end, va_start, va_end, te_start, te_end):
        raise ValueError(
            f"窗口 train=({tr_s}~{tr_e}) valid=(~{va_e}) test=(~{te_e}) "
            f"在交易日网格上取不到完整区间（数据边界与月历边界不匹配）")
    tr_end = tr_end - purge_bars          # 拟合段尾部：标签会伸进下一段的都剪掉
    va_start = va_start + embargo_bars    # 评估段头部：隔离带
    va_end = va_end - purge_bars
    te_start = te_start + embargo_bars
    if tr_end < tr_start:
        raise ValueError(
            f"purge_bars={purge_bars} 把训练段剪空了："
            f"train=({tr_s}~{tr_e}) 只有 {tr_end - tr_start + 1 + purge_bars} 根可用")
    if va_start > va_end:
        raise ValueError(
            f"purge={purge_bars}/embargo={embargo_bars} 把验证段剪空了："
            f"valid=(~{va_e}) 可用交易日不足")
    if te_start > te_end:
        raise ValueError(
            f"embargo_bars={embargo_bars} 把测试段剪空了：test=(~{te_e}) 可用交易日不足")
    return ((tr_s, grid[tr_end]),
            (grid[va_start], grid[va_end]),
            (grid[te_start], grid[te_end]))


def _first_idx_ge(grid: list[date], d: date):
    """``grid`` 中第一个 >= d 的下标；没有返回 None。"""
    for i, x in enumerate(grid):
        if x >= d:
            return i
    return None


def _first_idx_gt(grid: list[date], d: date):
    """``grid`` 中第一个 > d 的下标；没有返回 None。

    用「严格大于」而不是「>=」是与 ``Dataset.split`` 对齐：边界日归前一段，
    后一段从它之后的第一根开始。
    """
    for i, x in enumerate(grid):
        if x > d:
            return i
    return None


def _last_idx_le(grid: list[date], d: date):
    """``grid`` 中最后一个 <= d 的下标；没有返回 None。"""
    idx = None
    for i, x in enumerate(grid):
        if x > d:
            break
        idx = i
    return idx


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)
