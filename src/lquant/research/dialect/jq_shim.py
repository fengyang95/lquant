"""JoinQuant API 兼容层。

方言只翻译 API 调用，撮合/费率/T+N 全由内核加 RuleSet 决定 ——
所以 JQ 策略跑出来的成本与原生策略完全一致。
"""
from __future__ import annotations

import contextvars
from datetime import date

import polars as pl
from loguru import logger

#: fq 参数里表示「要复权」的取值
_FQ_ADJUSTED = ("pre", "post")


def _strict_adj_default() -> bool:
    """严格复权开关（``LQ_STRICT_ADJ`` / app.yaml ``data.strict_adj``）。

    默认关闭：真实湖里有 1480 行 ETF（akshare-sina 源）没有复权因子，
    硬失败会让这些标的的 fq 请求全部报错。但只要打开，就绝不静默
    用 factor=1.0 顶替 —— 这是「回测跑通但价格口径错了」的唯一防线。
    """
    try:
        from lquant.core.config import get_settings

        return bool(get_settings().strict_adj)
    except Exception:  # noqa: BLE001 - 配置不可用时退回宽松口径，不阻断取数
        return False


def apply_fq(df: pl.DataFrame, fq: str | None, *, strict_adj: bool | None = None,
             where: str = "history") -> pl.DataFrame:
    """按 fq 复权价格列（pre 用窗口内最新因子归一，post 用因子本身）。

    **缺失因子不再静默当 1.0**：

    - ``adj_factor`` 列整个不存在 → 请求了复权却拿到原始价，是最危险的一种；
    - 列存在但窗口内前若干行因子为空（forward_fill 填不上）→ 序列里混入原始价，
      收益率会出现无中生有的跳变。

    两者都按 ``strict_adj`` 决定：True 抛 ``ValueError``（fail-loud），
    False 打一条带标的与坏行数的 warning（不再无声无息）。
    """
    if fq not in _FQ_ADJUSTED:
        return df
    price_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    if not price_cols or df.is_empty():
        return df

    strict = _strict_adj_default() if strict_adj is None else bool(strict_adj)

    if "adj_factor" not in df.columns:
        msg = (f"{where}: 请求了 fq={fq!r} 但行情里没有 adj_factor 列，"
               "将按**原始价**返回（口径与请求不符）")
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        return df

    # 「因子缺失的行保留原始价」是历史行为（回填滞后不应把价格变 null），
    # 但它会让复权序列混入原始价 —— 必须显式计数，不能当没发生。
    # 用表达式而不是 Series 运算：因子列全空时 dtype=Null，Series 的比较会抛
    # NotImplementedError（列在、值全空恰好是最该报出来的一种）。
    ok_series = df["adj_factor"].cast(pl.Float64).is_not_null() & \
        (df["adj_factor"].cast(pl.Float64) > 0)
    if not bool(ok_series.any()):
        # 整窗口都没有可用因子：pre 的归一基准无从谈起，post 也没因子可用
        msg = (f"{where}: 窗口内没有任何有效 adj_factor，无法按 fq={fq} 复权"
               "（已按原始价返回）")
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
        return df

    bad_expr = (pl.col("adj_factor").is_null()
                | (pl.col("adj_factor").cast(pl.Float64) <= 0))
    n_missing = int(df.select(bad_expr.sum()).item() or 0)
    if n_missing:
        bad = (df.filter(bad_expr)
                 .select(["symbol", "trade_date"]).head(5).to_dicts()
               if {"symbol", "trade_date"} <= set(df.columns) else [])
        msg = (f"{where}: {n_missing}/{len(df)} 行缺有效 adj_factor，"
               f"这些行将退回原始价（fq={fq}）。样例: {bad}")
        if strict:
            raise ValueError(msg)
        logger.warning(msg)

    latest = (df.filter(pl.col("adj_factor").is_not_null()
                        & (pl.col("adj_factor").cast(pl.Float64) > 0))
                .sort("trade_date").group_by("symbol").last()
                .select(["symbol", pl.col("adj_factor").alias("_f_latest")]))
    df = df.join(latest, on="symbol", how="left")
    filled = pl.col("adj_factor").forward_fill().over("symbol", order_by="trade_date")
    if fq == "pre":
        ratio = (pl.col("adj_factor") / pl.col("_f_latest")).fill_null(1.0)
        df = df.with_columns(filled)
        df = df.with_columns([(pl.col(c) * ratio).alias(c) for c in price_cols])
    else:
        df = df.with_columns(filled)
        df = df.with_columns(
            [(pl.col(c) * pl.col("adj_factor").fill_null(1.0)).alias(c)
             for c in price_cols])
    return df.drop("adj_factor", "_f_latest")


class JQContext:
    """运行时上下文，由方言适配器注入。"""

    def __init__(self, engine, trade_date: date, universe: list[str]) -> None:
        self.engine = engine
        self.current_dt = trade_date
        self.universe = universe
        self.portfolio = None


def attribute_history(security, count=1, unit="1d", fields=("open", "close"),
                      df=True, skip_paused=False, fq="pre", strict_adj=None):
    """⚠️ 返回的是【上一单位时间】为止的数据，不含当前 bar。

    fq 与 ``history`` 同口径（pre/post/None）。历史上这个函数**收了 fq 却从不复权**，
    调用方拿到的是原始价 —— 静默口径错误，现已在同一个 :func:`apply_fq` 上收口。
    """
    from lquant.data.store.parquet import read_daily

    lf = read_daily([security])
    out = (lf.filter(pl.col("trade_date") <= _ctx().current_dt)
             .sort("trade_date").tail(count).collect())
    out = apply_fq(out, fq, strict_adj=strict_adj,
                   where=f"attribute_history({security})")
    if skip_paused:
        out = out.drop_nulls(subset=["close"])
    field_list = [fields] if isinstance(fields, str) else list(fields)
    keep = [c for c in ("trade_date", "symbol", *field_list) if c in out.columns]
    out = out.select(keep)
    return out.to_pandas() if df else out


def _history_frame(secs: list[str], fields: list[str], count: int,
                   current_dt: date, fq: str | None = "pre",
                   skip_paused: bool = False):
    """read_daily 窗口 → 聚宽布局 DataFrame（index=交易日）。

    与回测口径一致：严格 trade_date <= current_dt，无未来函数。
    fq="pre"：p × f / f_latest（窗口内最新因子归一）；
    fq="post"：p × f；fq=None：原始价。
    """
    from lquant.data.store.parquet import lake_is_empty, read_daily

    lf = read_daily(symbols=secs, end=current_dt)
    df = (lf.sort(["symbol", "trade_date"])
            .group_by("symbol", maintain_order=True).tail(count).collect())
    if df.is_empty():
        # 读函数对空湖返回的是「有 schema 的空帧」，所以「帧为空」既可能是
        # 没同步过、也可能是窗口内确实没这只标的 —— 两者提示不同，需显式问
        # 湖是否为空。只在失败路径探文件系统，history 热路径不付代价。
        if lake_is_empty("daily"):
            raise ValueError(
                f"无日线数据（数据根目录为空，先同步数据），无法取 {secs} 截至 {current_dt}")
        raise ValueError(f"{secs} 无日线数据（窗口 {count} 天，截至 {current_dt}）")

    # 复权：口径与 strict_adj 语义见 apply_fq（缺失因子不再静默当 1.0）
    df = apply_fq(df, fq, where=f"history({','.join(secs[:3])}…)" if len(secs) > 3
                  else f"history({','.join(secs)})")

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
    缺省日走 today_cn()：成分表按业务日生效，服务器时区非 Asia/Shanghai 时
    date.today() 会错位一天（可能取到尚未生效的下一批成分）。
    """
    from lquant.core.types import parse_symbol, today_cn
    from lquant.data.store.catalog import IndexConsRepo

    try:
        sym = parse_symbol(index_symbol)
    except ValueError:
        # 非标准格式原样用；没有成分就返回空（宁可空，不臆造）
        code = index_symbol
    else:
        code = str(sym)
    if date is None:
        date = today_cn()
    return IndexConsRepo().symbols_as_of(code, date)


def get_trade_days(start_date=None, end_date=None, count=None):
    from lquant.core.calendar import trade_days

    return trade_days(start_date, end_date)


def log(*args):
    from loguru import logger

    logger.info(" ".join(map(str, args)))


# 并发安全：模块级全局会让同进程并发跑的两个回测 runner 互相覆盖上下文
# （策略 A 的 get_fundamentals 读到策略 B 绑定的 trade_date/universe —— 前视偏差
# 且结果不可复现）。ContextVar 随线程/任务隔离，同一执行流内语义不变。
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("jq_shim_current", default=None)


def _ctx() -> JQContext:
    cur = _CURRENT.get()
    if cur is None:
        raise RuntimeError("JQ 上下文未初始化")
    return cur


def bind(ctx: JQContext) -> None:
    _CURRENT.set(ctx)
