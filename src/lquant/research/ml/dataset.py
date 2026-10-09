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

from lquant.data.quality.flags import NEW_LISTING, SUSPENDED
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

    #: 入场价列。``None`` = 沿用 ``price_col``（close→close，因子评价的可比口径）；
    #: 设成 ``"open"`` 时 label = ``close[t+h]/open[t+1] - 1`` —— 与引擎
    #: 「T 日收盘出信号 → T+1 开盘撮合」的执行口径一致。
    #: **默认不切换**：切了之后与历史 ML 指标不可比，必须显式选。
    entry_price_col: str | None = None
    entry_lag: int = 1

    #: 可交易性屏蔽：把「入场那天根本买不到」的样本剔出训练集。
    #: 面板没有 ``quality_flags`` 列时为空操作，并在报告里显式说明（不静默）。
    tradability_mask: bool = True

    def label_col(self) -> str:
        return f"fwd_ret_{self.label_horizon}"

    def label_kind(self) -> str:
        """标签口径的可读描述（落进 runs/报告，避免事后猜）。"""
        entry = self.entry_price_col or self.price_col
        lag = self.entry_lag if self.entry_price_col else 0
        return f"{self.price_col}[t+{self.label_horizon}]/{entry}[t+{lag}]"


@dataclass
class Dataset:
    df: pl.DataFrame
    cfg: DatasetConfig
    dates: list[date] = field(default_factory=list)
    #: 样本屏蔽账：{"applied", "dropped", "reasons": {原因: 行数}, "note"}。
    #: 恒存在（哪怕没屏蔽），因为「屏蔽了多少、为什么」必须可查，不能靠猜。
    mask_report: dict = field(default_factory=dict)

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

    def split(self, train_end, valid_end, test_end=None, *, purge: bool = True):
        """按日期切成 (train, valid, test)。

        purge=True（默认）：train 尾部剔除 label_horizon-1 个交易日。
        标签是 forward_return —— 靠近 train_end 的样本要读 train_end 之后
        的价格才能定标签，不剔除就等于用 valid 段的价格算 train 的标签
        （标签泄漏），验证指标被系统性高估。
        """
        te = _as_date(train_end)
        if purge and self.cfg.label_horizon > 1:
            k = self.cfg.label_horizon - 1
            dd = [x for x in self.dates if x <= te]
            if len(dd) > k:
                te = dd[-1 - k]          # 回退 k 个交易日（dates 本身是交易日序列）
            else:
                # 窗口比泄漏窗还短：训练段为空（head(0)），由下游显式报错，
                # 不能悄悄退回不 purge —— 那是静默泄漏
                return (self.df.head(0),
                        self.slice(start=_next_day(train_end), end=valid_end),
                        self.slice(start=_next_day(valid_end), end=test_end))
        return (self.slice(end=te),
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
            # 标签口径与屏蔽账随 run 落库：事后不必猜「这个 fwd_ret 是 close→close
            # 还是 close→open」「剔掉了多少不可成交样本」。
            "label_kind": self.cfg.label_kind(),
            "mask": self.mask_report,
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


def _blocked_expr(df: pl.DataFrame, cfg: DatasetConfig) -> tuple[list[pl.Expr], list[str]]:
    """入场日「根本买不到」的判据（**在入场那一天**成立才算）。

    三类，各自可查：

    - 停牌（``quality_flags`` 的 SUSPENDED 位）：没有可成交价格；
    - 新股（NEW_LISTING 位）：涨跌停口径与波动结构都特殊；
    - 一字板：``high == low`` 且较昨收涨 ≥9.5% —— 全天一个价位封死，挂单排不上。
      用「high==low」做前提，所以对 20% 涨跌幅板块同样安全（非涨停的一字
      只可能是停牌，已由第一类覆盖），不需要逐板配置阈值。
    """
    exprs: list[pl.Expr] = []
    reasons: list[str] = []
    if "quality_flags" in df.columns:
        flags = pl.col("quality_flags").fill_null(0)
        exprs.append((flags & SUSPENDED) != 0)
        reasons.append("停牌")
        exprs.append((flags & NEW_LISTING) != 0)
        reasons.append("新股")
    if {"high", "low", "close", "pre_close"} <= set(df.columns):
        exprs.append(
            ((pl.col("high") - pl.col("low")).abs() < 1e-9)
            & (pl.col("close") >= pl.col("pre_close") * 1.095))
        reasons.append("一字板")
    return exprs, reasons


def build_dataset(df: pl.DataFrame, cfg: DatasetConfig) -> Dataset:
    """从行情长表构建数据集：补前瞻收益 → 剔不可成交样本 → 截尾 → 丢缺失。

    两处口径必须显式（否则回测 IC 会系统性虚高）：

    1. **不可成交样本**（停牌/新股/一字板）默认剔掉 —— 涨停当天买不到，回测
       却按成交价计入，等于把买不进的收益算进策略；
    2. **标签口径**由 ``label_kind()`` 描述并随 Dataset 落库，避免事后猜
       「这个 fwd_ret 到底是 close→close 还是 close→open」。
    """
    missing = [f for f in cfg.features if f not in df.columns]
    if missing:
        raise KeyError(f"特征列不存在: {missing}")

    label = cfg.label_col()
    if cfg.entry_price_col:
        # 次日入场口径：今收决定、明开入场（与引擎的 T+1 撮合一致）
        d = df.sort([cfg.symbol_col, cfg.date_col])
        entry = pl.col(cfg.entry_price_col).shift(-cfg.entry_lag).over(cfg.symbol_col)
        exit_ = pl.col(cfg.price_col).shift(-cfg.label_horizon).over(cfg.symbol_col)
        d = d.with_columns(((exit_ / entry) - 1.0).alias(label))
    else:
        d = forward_return(df, price_col=cfg.price_col, periods=[cfg.label_horizon],
                           by=cfg.symbol_col, date_col=cfg.date_col)

    # ---- 可交易性屏蔽（入场日判定，因此要把标记前移到信号日）----
    report: dict = {"applied": False, "dropped": 0, "reasons": {},
                    "label_kind": cfg.label_kind()}
    if cfg.tradability_mask:
        exprs, reasons = _blocked_expr(d, cfg)
        if not exprs:
            report["note"] = ("面板无 quality_flags / OHLC 列，未做可交易性屏蔽"
                              "（这不是「已检查」）")
        else:
            blocked = exprs[0]
            for e in exprs[1:]:
                blocked = blocked | e
            entry_blocked = (blocked.shift(-cfg.entry_lag).over(cfg.symbol_col)
                                    .fill_null(False))
            before = len(d)
            per_reason = {}
            for name, e in zip(reasons, exprs, strict=True):
                per_reason[name] = int(d.select(
                    (e.shift(-cfg.entry_lag).over(cfg.symbol_col)
                     .fill_null(False)).sum()).item() or 0)
            d = d.filter(~entry_blocked)
            report.update(applied=True, dropped=before - len(d),
                          reasons=per_reason)
    else:
        report["note"] = "tradability_mask=False（显式关闭）"

    if cfg.clip_label is not None:
        d = d.with_columns(pl.col(label).clip(-cfg.clip_label, cfg.clip_label))

    if cfg.dropna:
        d = d.drop_nulls([label, *cfg.features])

    # 样本太少的日子截面统计不可靠，直接剔掉
    d = d.filter(pl.len().over(cfg.date_col) >= cfg.min_samples_per_day)

    dates = sorted(d[cfg.date_col].unique().to_list())
    return Dataset(df=d, cfg=cfg, dates=dates, mask_report=report)


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
