"""JoinQuant API 兼容层。

方言只翻译 API 调用，撮合/费率/T+N 全由内核加 RuleSet 决定 ——
所以 JQ 策略跑出来的成本与原生策略完全一致。
"""
from __future__ import annotations

from datetime import date

import polars as pl


class JQContext:
    """运行时上下文，由方言适配器注入。"""

    def __init__(self, engine, trade_date: date, universe: list[str]) -> None:
        self.engine = engine
        self.current_dt = trade_date
        self.universe = universe
        self.portfolio = None


def attribute_history(security, count=1, unit="1d", fields=("open", "close"),
                      df=True, skip_paused=False, fq="pre"):
    """⚠️ 返回的是【上一单位时间】为止的数据，不含当前 bar。"""
    from lquant.data.store.parquet import read_daily

    lf = read_daily([security])
    out = (lf.filter(pl.col("trade_date") <= _ctx().current_dt)
             .sort("trade_date").tail(count).collect())
    return out.to_pandas() if df else out


def _history_frame(secs: list[str], fields: list[str], count: int,
                   current_dt: date, fq: str | None = "pre",
                   skip_paused: bool = False):
    """read_daily 窗口 → 聚宽布局 DataFrame（index=交易日）。

    与回测口径一致：严格 trade_date <= current_dt，无未来函数。
    fq="pre"：p × f / f_latest（窗口内最新因子归一）；
    fq="post"：p × f；fq=None：原始价。
    """
    from lquant.data.store.parquet import read_daily

    lf = read_daily(symbols=secs, end=current_dt)
    if not lf.collect_schema().names():
        raise ValueError(
            f"无日线数据（数据根目录为空，先同步数据），无法取 {secs} 截至 {current_dt}")
    df = (lf.sort(["symbol", "trade_date"])
            .group_by("symbol", maintain_order=True).tail(count).collect())
    if df.is_empty():
        raise ValueError(f"{secs} 无日线数据（窗口 {count} 天，截至 {current_dt}）")

    # 复权在取字段前完成：price 列 × adj_factor（pre 再除以窗口内最新因子）。
    # 因子缺失的行保留原始价（回填滞后不应把价格变 null）；「最新因子」取
    # 时间序上最后一条非空因子，而非 max（因子理论上单调，但数据修正不保证）。
    price_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    if fq in ("pre", "post") and price_cols and "adj_factor" in df.columns:
        latest = (df.filter(pl.col("adj_factor").is_not_null())
                    .sort("trade_date").group_by("symbol").last()
                    .select(["symbol", pl.col("adj_factor").alias("_f_latest")]))
        df = df.join(latest, on="symbol", how="left")
        filled = pl.col("adj_factor").forward_fill().over(
            "symbol", order_by="trade_date")
        ratio = (pl.col("adj_factor") / pl.col("_f_latest")).fill_null(1.0)
        if fq == "pre":
            df = df.with_columns(filled)
            df = df.with_columns(
                [(pl.col(c) * ratio).alias(c) for c in price_cols])
        else:
            df = df.with_columns(filled)
            df = df.with_columns(
                [(pl.col(c) * pl.col("adj_factor").fill_null(1.0)).alias(c)
                 for c in price_cols])
        df = df.drop("adj_factor", "_f_latest")

    if "avg" in fields or "money" in fields:
        derived = []
        if "avg" in fields and {"open", "high", "low", "close"} <= set(df.columns):
            derived.append(((pl.col("open") + pl.col("high")
                             + pl.col("low") + pl.col("close")) / 4.0).alias("avg"))
        if "money" in fields and "amount" in df.columns:
            derived.append(pl.col("amount").alias("money"))
        if derived:
            df = df.with_columns(derived)

    cells: dict[tuple[str, str], dict[date, float]] = {}
    for s in secs:
        sub = df.filter(pl.col("symbol") == s)
        if skip_paused:
            sub = sub.drop_nulls(subset=["close"])
        for f in fields:
            if f not in sub.columns:
                raise ValueError(
                    f"history 字段 {f!r} 未支持，可用：open/high/low/close/"
                    "volume/money(=amount)/avg/turnover_rate/pe_ttm/...")
            cells[(s, f)] = dict(zip(sub["trade_date"].to_list(),
                                     sub[f].to_list(), strict=True))

    day_index = sorted({d for m in cells.values() for d in m})

    def _cell(s: str, f: str, d: date):
        return cells[(s, f)].get(d)

    try:
        import pandas as pd
    except ImportError:
        if len(fields) == 1:
            f = fields[0]
            return {s: [_cell(s, f, d) for d in day_index] for s in secs}
        if len(secs) == 1:
            return {f: [_cell(secs[0], f, d) for d in day_index] for f in fields}
        return {s: {f: [_cell(s, f, d) for d in day_index] for f in fields}
                for s in secs}

    if len(fields) == 1:
        f = fields[0]
        return pd.DataFrame({s: [_cell(s, f, d) for d in day_index]
                             for s in secs}, index=day_index)
    if len(secs) == 1:
        return pd.DataFrame({f: [_cell(secs[0], f, d) for d in day_index]
                             for f in fields}, index=day_index)
    cols = pd.MultiIndex.from_product([secs, fields])
    return pd.DataFrame({c: [_cell(c[0], c[1], d) for d in day_index]
                         for c in cols}, index=day_index)


def history(count=1, unit="1d", field="avg", security_list=None, df=True,
            skip_paused=False, fq="pre"):
    if unit != "1d":
        raise ValueError(f"history 仅支持日频，收到 unit={unit!r}")
    if not df:
        raise ValueError("history 目前仅支持 df=True（聚宽研究环境默认即 df）")
    fields = [field] if isinstance(field, str) else list(field or ["close"])
    secs = [str(s) for s in (security_list or _ctx().universe)]
    return _history_frame(secs, fields, int(count), _ctx().current_dt,
                          fq=fq, skip_paused=skip_paused)


def get_fundamentals(query, date=None):
    """聚宽 get_fundamentals：严格 PIT，pub_date <= 当前日。

    query 由 lquant.research.dialect.fundamentals 构造
    （query(valuation.pe_ratio, income.net_profit).filter(...)...）。
    """
    from datetime import date as _date

    from lquant.research.dialect import fundamentals as _fd

    if not isinstance(query, _fd.Query):
        raise ValueError(
            "get_fundamentals 需要 fundamentals.query(...) 构造的查询对象，"
            f"收到 {type(query).__name__}")
    if date is None:
        day = _ctx().current_dt
    else:
        day = date if isinstance(date, _date) else _date.fromisoformat(str(date))
    out = _fd.resolve(query, day)
    try:
        import importlib.util

        if importlib.util.find_spec("pandas") is None:
            return out
        return out.to_pandas()
    except ImportError:
        return out


def order_target_value(security, value):
    _ctx().engine.order_target_value(security, value)


def order_target_percent(security, percent):
    _ctx().engine.order_target_percent(security, percent)


def order(security, amount):
    _ctx().engine.order(security, amount)


def get_all_securities(types=None, date=None):
    from lquant.data.store.catalog import SecurityRepo

    return pl.DataFrame({"symbol": SecurityRepo().active_symbols()}).to_pandas()


def get_index_stocks(index_symbol, date=None):
    """指数当下成分（默认今天）。date 指定时按该日已生效成分返回（防前视）。

    index_symbol 用 JQ 习惯的 '000300.XSHG' 或平台内码 '000300.SH' 皆可。
    """
    from datetime import date as _date  # 参数名 date 遮蔽了模块级类名，用别名引用

    from lquant.core.types import parse_symbol
    from lquant.data.store.catalog import IndexConsRepo

    try:
        sym = parse_symbol(index_symbol)
    except ValueError:
        # 非标准格式原样用；没有成分就返回空（宁可空，不臆造）
        code = index_symbol
    else:
        code = str(sym)
    if date is None:
        date = _date.today()
    return IndexConsRepo().symbols_as_of(code, date)


def get_trade_days(start_date=None, end_date=None, count=None):
    from lquant.core.calendar import trade_days

    return trade_days(start_date, end_date)


def log(*args):
    from loguru import logger

    logger.info(" ".join(map(str, args)))


_CURRENT: JQContext | None = None


def _ctx() -> JQContext:
    if _CURRENT is None:
        raise RuntimeError("JQ 上下文未初始化")
    return _CURRENT


def bind(ctx: JQContext) -> None:
    global _CURRENT
    _CURRENT = ctx
