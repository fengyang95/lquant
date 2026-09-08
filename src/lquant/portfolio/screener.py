"""选池：先过滤，再打分。

顺序不能反。先打分后过滤的问题是：被过滤掉的股票往往分数极端
（ST 票的反转因子、次新股的波动因子都是极值），
它们会占满 TopN 名额，等过滤完才发现剩下的票分数都很平庸。

过滤器的每一条都对应一类实盘买不到的情况：
- ST：涨跌停 5%，流动性差，且有退市风险
- 次新：上市不足 60 日，波动由打新情绪主导，因子失效
- 停牌 / 一字板：挂单根本成交不了
- 流动性不足：冲击成本能吃掉全部超额
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

__all__ = ["FilterConfig", "apply_filters", "score", "screen", "filter_report"]


@dataclass
class FilterConfig:
    exclude_st: bool = True
    min_list_days: int = 60
    min_amount: float = 5_000_000.0      # 日均成交额下限（元）
    min_price: float = 1.0               # 低价股退市风险 + 报价粒度粗
    exclude_suspended: bool = True
    exclude_limit_up: bool = True        # 涨停买不进
    exclude_limit_down: bool = False     # 跌停卖不出（选股时通常不排除）
    max_count: int | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _limit_flag(df: pl.DataFrame, limit: float = 0.10) -> pl.Expr:
    """一字板判定：涨停时最高价 = 最低价 = 收盘价。"""
    return ((pl.col("high") - pl.col("low")).abs() < 1e-9) & \
           (pl.col("close") >= pl.col("pre_close") * (1 + limit) - 1e-9)


def apply_filters(df: pl.DataFrame, cfg: FilterConfig | None = None, *,
                  date_col: str = "trade_date", symbol_col: str = "symbol",
                  verbose: bool = False) -> pl.DataFrame:
    """按配置逐条过滤。缺列的条件自动跳过（不同数据源字段不齐）。"""
    cfg = cfg or FilterConfig()
    out = df
    dropped: dict[str, int] = {}

    def step(name: str, cond: pl.Expr | None) -> None:
        nonlocal out
        if cond is None:
            return
        before = len(out)
        out = out.filter(~cond)
        dropped[name] = before - len(out)

    if cfg.exclude_st and "is_st" in out.columns:
        step("ST", pl.col("is_st").fill_null(False))
    elif cfg.exclude_st and "name" in out.columns:
        step("ST", pl.col("name").cast(pl.Utf8).str.contains("ST|退").fill_null(False))

    if cfg.min_list_days and "list_date" in out.columns:
        step("次新", pl.col("list_date") >
             (pl.col(date_col) - pl.duration(days=cfg.min_list_days)))

    if "amount" in out.columns:
        step("流动性", pl.col("amount").fill_null(0.0) < cfg.min_amount)

    if "close" in out.columns:
        step("低价股", pl.col("close") < cfg.min_price)

    if cfg.exclude_suspended and "volume" in out.columns:
        step("停牌", pl.col("volume").fill_null(0.0) <= 0)

    if cfg.exclude_limit_up and {"high", "low", "close", "pre_close"} <= set(out.columns):
        step("涨停", _limit_flag(out))

    if cfg.exclude_limit_down and {"high", "low", "close", "pre_close"} <= set(out.columns):
        step("跌停", ((pl.col("high") - pl.col("low")).abs() < 1e-9) &
             (pl.col("close") <= pl.col("pre_close") * (1 - 0.10) + 1e-9))

    if verbose:
        for k, v in dropped.items():
            print(f"  过滤 {k}: -{v}")
    return out


def score(df: pl.DataFrame, factors: dict[str, float] | list[str], *,
          method: str = "zscore", out: str = "score",
          date_col: str = "trade_date") -> pl.DataFrame:
    """多因子加权合成。

    factors : {"mom20": 0.6, "bp": 0.4} 或 ["mom20", "bp"]（等权）
    method  : zscore（先标准化再加权）| rank（先排名再加权，更稳健）

    因子必须**同向**：都是越大越好。方向相反的因子（如换手率）
    调用前先取负，否则加权等于互相抵消。
    """
    if isinstance(factors, list):
        factors = {f: 1.0 / len(factors) for f in factors}
    missing = [f for f in factors if f not in df.columns]
    if missing:
        raise KeyError(f"因子列不存在: {missing}")

    parts = []
    for f, w in factors.items():
        col = pl.col(f).cast(pl.Float64, strict=False)
        if method == "rank":
            v = col.rank("average").over(date_col) / col.count().over(date_col)
        else:
            mean = col.mean().over(date_col)
            std = pl.when(col.std().over(date_col) > 1e-12) \
                    .then(col.std().over(date_col)).otherwise(pl.lit(1.0))
            v = (col - mean) / std
        parts.append(v.fill_null(0.0) * w)

    total = parts[0]
    for p in parts[1:]:
        total = total + p
    return df.with_columns(total.alias(out))


def screen(df: pl.DataFrame, factors: dict[str, float] | list[str], *,
           cfg: FilterConfig | None = None, top_n: int = 30,
           date_col: str = "trade_date", symbol_col: str = "symbol",
           ascending: bool = False, verbose: bool = False) -> pl.DataFrame:
    """过滤 → 打分 → 取 TopN。返回当期的选股结果。"""
    cfg = cfg or FilterConfig()
    d = apply_filters(df, cfg, date_col=date_col, symbol_col=symbol_col, verbose=verbose)
    if not len(d):
        return d
    d = score(d, factors, date_col=date_col)
    d = d.sort("score", descending=not ascending)
    if top_n and top_n > 0:
        d = d.head(top_n)
    return d


def filter_report(df: pl.DataFrame, cfg: FilterConfig | None = None, **kw) -> dict:
    """统计每层过滤掉了多少，用于诊断「为什么这个池子这么小」。"""
    cfg = cfg or FilterConfig()
    total = len(df)
    kept = len(apply_filters(df, cfg, **kw))
    return {"total": total, "kept": kept, "dropped": total - kept,
            "kept_ratio": kept / total if total else 0.0, "config": cfg.to_dict()}
