"""个股分析取数层。

三条硬约束，全部由本模块负责，上层角度计算只管算：

1. **PIT（无未来函数）**：所有带 ``asof`` 的读取都强制 ``<= asof``。财务走
   ``pub_date <= asof``（公告日之前查不到就是查不到），估值/行情走
   ``trade_date <= asof``，新闻走 ``published_at <= asof``。回测里的前视偏差
   绝大多数来自「顺手取最新一期」，这里不给这个机会。
2. **空表是常态不是错误**：表没建 / 没同步 → 返回空帧 + 由上层给出 ``hint``，
   绝不抛异常把整个分析打挂。数据湖可以只有行情没有财务。
3. **asof 默认取湖内最新交易日**，而不是 ``today_cn()`` —— 数据同步通常滞后于
   自然日，用自然日会得到一个「未来」的观察点，把尚未同步的数据当成缺失。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.logging import get_logger
from lquant.core.types import today_cn

log = get_logger(__name__)

#: 默认基准指数（相对强度的分母）。依次尝试，取第一个有数据的。
BENCHMARK_CANDIDATES: tuple[str, ...] = ("000300.SH", "000001.SH", "399006.SZ")

#: 行情回看窗口（自然日）。需覆盖 MA60 / 120 日动量 / 一年波动率，留足预热。
BAR_LOOKBACK_DAYS = 500

#: 估值历史回看窗口（自然日）——分位需要足够长的历史才有意义。
VALUATION_LOOKBACK_DAYS = 1250

#: 资金流 / 新闻回看窗口（自然日）。
FLOW_LOOKBACK_DAYS = 60
NEWS_LOOKBACK_DAYS = 30


@dataclass
class MarketData:
    """一次分析所需的全部原始数据（取不到的项留空帧而不是缺字段）。"""

    symbol: str
    asof: date
    name: str | None = None
    sec_type: str | None = None
    board: str | None = None
    is_st: bool = False
    industry: str | None = None
    industry_code: str | None = None

    bars: pl.DataFrame = field(default_factory=pl.DataFrame)
    benchmark: pl.DataFrame = field(default_factory=pl.DataFrame)
    benchmark_symbol: str | None = None

    valuation: pl.DataFrame = field(default_factory=pl.DataFrame)
    valuation_cross: pl.DataFrame = field(default_factory=pl.DataFrame)

    #: 本票 PIT 财务长表：symbol, item, stat_date, pub_date, value
    financial: pl.DataFrame = field(default_factory=pl.DataFrame)
    #: 同行业 PIT 财务截面（用于算行业分位）
    financial_peers: pl.DataFrame = field(default_factory=pl.DataFrame)
    peer_count: int = 0

    money_flow: pl.DataFrame = field(default_factory=pl.DataFrame)
    news: pl.DataFrame = field(default_factory=pl.DataFrame)

    #: 数据层面的提示（湖是空的 / 该票没有行情 …）
    notes: list[str] = field(default_factory=list)


def resolve_asof(asof: date | str | None) -> date:
    """确定观察日：显式给定优先，否则用湖内最新交易日。"""
    if asof is not None:
        if isinstance(asof, str):
            return date.fromisoformat(asof)
        return asof
    from lquant.data.store.parquet import latest_trade_date

    latest = latest_trade_date()
    return latest if latest is not None else today_cn()


def _safe(fn, *args, **kwargs):
    """读底层数据的统一降级包装：任何异常 → None（由调用方转成空帧 + note）。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - 表未建 / 湖缺失 / 源不可用
        log.debug("security loader 降级: %s", e)
        return None


# ---------------------------------------------------------------- 行情


def load_bars(symbol: str, asof: date,
              lookback_days: int = BAR_LOOKBACK_DAYS) -> pl.DataFrame:
    """个股日线（截至 asof，含 asof 当日）。"""
    from lquant.data.store.parquet import read_daily

    start = asof - timedelta(days=lookback_days)
    lf = _safe(read_daily, [symbol], start=start, end=asof)
    if lf is None:
        return pl.DataFrame()
    df = _safe(lambda: lf.collect())
    if df is None or df.is_empty():
        return pl.DataFrame()
    cols = [c for c in ("trade_date", "open", "high", "low", "close",
                        "volume", "amount", "turnover_rate") if c in df.columns]
    return df.select(cols).sort("trade_date")


def load_benchmark(asof: date, lookback_days: int = BAR_LOOKBACK_DAYS
                   ) -> tuple[pl.DataFrame, str | None]:
    """基准指数日线。返回 ``(帧, 指数代码)``；都没有则空帧。

    **只在「该指数确实没有数据」时降级到下一个候选**；读取异常直接判为不可用。

    为什么异常不降级：沪深300 与 上证指数 的超额收益不是一回事，静默换一个基准
    会让同一观察日的结论在两次页面刷新之间变化（实测：DuckDB 跨进程锁冲突时
    000300 读失败 → 悄悄改用 000001，综合分从 60 跳到 65）。宁可这一个角度
    报「暂时读不到基准」，也不给一个不可比、不可复现的数字。
    """
    start = asof - timedelta(days=lookback_days)
    for code in BENCHMARK_CANDIDATES:
        try:
            df = _read_index(code, start, asof)
        except Exception as e:  # noqa: BLE001 - 读失败不该伪装成「没有这个指数」
            log.warning("基准指数 %s 读取失败，不降级到其他基准: %s", code, e)
            return pl.DataFrame(), None
        if not df.is_empty():
            return df, code
    return pl.DataFrame(), None


def _read_index(index_symbol: str, start: date, end: date) -> pl.DataFrame:
    """从 ``index_daily`` 表读指数日线（表缺失时抛给 _safe 处理）。"""
    from lquant.core.db import reader

    sql = ("SELECT trade_date, close FROM index_daily "
           "WHERE symbol = ? AND trade_date >= ? AND trade_date <= ? "
           "ORDER BY trade_date")
    with reader() as con:
        df = con.execute(sql, [index_symbol, start, end]).pl()
    return df


# ---------------------------------------------------------------- 估值


def load_valuation(symbol: str, asof: date,
                   lookback_days: int = VALUATION_LOOKBACK_DAYS
                   ) -> tuple[pl.DataFrame, pl.DataFrame]:
    """估值数据：``(本票历史, asof 当日全市场截面)``。

    截面只取 asof 前后一小段（拿当日快照），全市场物化没有意义。
    """
    from lquant.data.store.parquet import read_daily_basic

    start = asof - timedelta(days=lookback_days)
    # read_daily_basic 恒返回 DataFrame（空湖也是），读失败才由 _safe 给 None
    own = _safe(read_daily_basic, start, asof, symbols=[symbol])
    own = own if isinstance(own, pl.DataFrame) else pl.DataFrame()

    # 截面：asof 往回 10 个自然日足够覆盖最近交易日（含长假）。
    cross = _safe(read_daily_basic, asof - timedelta(days=10), asof)
    cross = cross if isinstance(cross, pl.DataFrame) else pl.DataFrame()
    if not cross.is_empty() and "trade_date" in cross.columns:
        last_day = cross["trade_date"].max()
        cross = cross.filter(pl.col("trade_date") == last_day)
    return own, cross


# ---------------------------------------------------------------- 财务（PIT）

#: 财务指标候选名 → 统一口径。
#: 同一指标在不同数据源下命名不同（baostock 用 ``profit.roeAvg``，
#: tushare 用 ``indicator.roe``），这里做归一，谁在就用谁。
CANONICAL_FINANCIAL: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("roe", "净资产收益率", ("indicator.roe", "indicator.roe_waa", "profit.roeAvg"), True),
    ("roa", "总资产收益率", ("indicator.roa",), True),
    ("roic", "投入资本回报率", ("indicator.roic",), True),
    ("net_margin", "销售净利率", ("indicator.netprofit_margin", "profit.npMargin"), True),
    ("gross_margin", "销售毛利率",
     ("indicator.grossprofit_margin", "indicator.gross_margin", "profit.gpMargin"), True),
    ("debt_to_assets", "资产负债率",
     ("indicator.debt_to_assets", "balance.liabilityToAsset"), False),
    ("current_ratio", "流动比率", ("indicator.current_ratio", "balance.currentRatio"), True),
    ("quick_ratio", "速动比率", ("indicator.quick_ratio", "balance.quickRatio"), True),
    ("assets_turn", "总资产周转率",
     ("indicator.assets_turn", "operation.AssetTurnRatio"), True),
    ("revenue_yoy", "营收同比", ("indicator.or_yoy", "indicator.tr_yoy"), True),
    ("profit_yoy", "净利同比",
     ("indicator.netprofit_yoy", "indicator.dt_netprofit_yoy"), True),
    ("ocf_yoy", "经营现金流同比", ("indicator.ocf_yoy",), True),
)

#: 所有候选名的扁平集合（一次查询取回）。
FINANCIAL_ITEMS: tuple[str, ...] = tuple(
    name for _, _, names, _ in CANONICAL_FINANCIAL for name in names
)

#: 逆映射：item → canonical key。
ITEM_TO_KEY: dict[str, str] = {
    name: key for key, _, names, _ in CANONICAL_FINANCIAL for name in names
}

#: higher_better 映射。
HIGHER_BETTER: dict[str, bool] = {
    key: hb for key, _, _, hb in CANONICAL_FINANCIAL
}


def load_financial_own(con, symbol: str, asof: date) -> pl.DataFrame:
    """本票 PIT 财务（pub_date <= asof 的全部期数，供展示趋势）。

    同一 ``(symbol, item, stat_date, pub_date)`` 可能有多行（数据源重复入库），
    这里按值去重 —— 不去重会让「同比」之类的派生计算被重复行放大。
    """
    ph = ",".join("?" * len(FINANCIAL_ITEMS))
    sql = (f"SELECT symbol, item, stat_date, pub_date, value FROM financial_pit "
           f"WHERE symbol = ? AND item IN ({ph}) AND pub_date <= ? "
           f"ORDER BY stat_date, pub_date")
    try:
        df = con.execute(sql, [symbol, *FINANCIAL_ITEMS, asof]).pl()
    except Exception as e:  # noqa: BLE001 - 表未建 / 未同步
        log.debug("financial_pit 读取失败: %s", e)
        return pl.DataFrame()
    if df.is_empty():
        return df
    return df.unique(subset=["symbol", "item", "stat_date", "pub_date"], keep="first")


def load_financial_cross(con, symbols: list[str], asof: date) -> pl.DataFrame:
    """同行业 PIT 财务截面：每个 ``(symbol, item)`` 取 ``stat_date`` 最新的一期。

    ``symbols`` 为空时返回空帧（**不**退化成全市场扫描 —— 那是 4 秒级的查询，
    不能放在交互路径上）。
    """
    if not symbols:
        return pl.DataFrame()
    sph = ",".join("?" * len(symbols))
    ph = ",".join("?" * len(FINANCIAL_ITEMS))
    sql = f"""
        SELECT symbol, item, value FROM (
            SELECT symbol, item, value,
                   row_number() OVER (PARTITION BY symbol, item
                                      ORDER BY stat_date DESC, pub_date DESC) AS rn
            FROM financial_pit
            WHERE symbol IN ({sph}) AND item IN ({ph}) AND pub_date <= ?
        ) WHERE rn = 1
    """
    try:
        return con.execute(sql, [*symbols, *FINANCIAL_ITEMS, asof]).pl()
    except Exception as e:  # noqa: BLE001
        log.debug("financial_pit 截面读取失败: %s", e)
        return pl.DataFrame()


# ---------------------------------------------------------------- 行业 / 资金 / 新闻


def load_industry(con, symbol: str, asof: date
                  ) -> tuple[str | None, str | None, list[str]]:
    """所属行业 ``(code, name, 同行业标的列表)``，全部按 ``std_date <= asof`` 取。"""
    try:
        row = con.execute(
            "SELECT code, name FROM industry_classify "
            "WHERE symbol = ? AND std_date <= ? "
            "ORDER BY std_date DESC LIMIT 1",
            [symbol, asof],
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        log.debug("industry_classify 读取失败: %s", e)
        return None, None, []
    if not row:
        return None, None, []
    code, name = row[0], row[1]
    try:
        peers = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM industry_classify "
            "WHERE code = ? AND std_date <= ?",
            [code, asof],
        ).fetchall()]
    except Exception:  # noqa: BLE001
        peers = []
    return code, name, peers


def load_money_flow(con, symbol: str, asof: date,
                    days: int = FLOW_LOOKBACK_DAYS) -> pl.DataFrame:
    """主力资金流（近 ``days`` 自然日，截至 asof）。

    只取真实来源的行：``money_flow`` 里混着 demo 采集写进去的合成数据，
    而合成数据的代码有相当一部分能对上真实标的（200 个 demo 代码里 75 个
    是真实上市公司），拿它评分等于用伪造的净流入下结论。
    过滤条件与覆盖统计/清理共用 ``real_flow_predicate``，
    避免「分析排除了、统计没排除」这类漂移。
    """
    from lquant.market.backfill import real_flow_predicate

    try:
        real = real_flow_predicate(con)
        return con.execute(
            "SELECT trade_date, main_net_inflow, main_net_ratio, super_large_net, "
            "large_net, medium_net, small_net, change_pct FROM money_flow "
            "WHERE symbol = ? AND trade_date <= ? AND trade_date >= ? "
            f"AND {real} "
            "ORDER BY trade_date",
            [symbol, asof, asof - timedelta(days=days)],
        ).pl()
    except Exception as e:  # noqa: BLE001
        log.debug("money_flow 读取失败: %s", e)
        return pl.DataFrame()


def load_news(con, symbol: str, asof: date,
              days: int = NEWS_LOOKBACK_DAYS) -> pl.DataFrame:
    """个股相关新闻（``symbols`` 数组包含本票，截至 asof）。"""
    try:
        return con.execute(
            "SELECT news_id, title, source_name, published_at, url FROM news_item "
            "WHERE list_contains(symbols, ?) AND published_at <= ? AND published_at >= ? "
            "ORDER BY published_at DESC",
            [symbol, datetime.combine(asof, datetime.max.time()),
             datetime.combine(asof - timedelta(days=days), datetime.min.time())],
        ).pl()
    except Exception as e:  # noqa: BLE001
        log.debug("news_item 读取失败: %s", e)
        return pl.DataFrame()


def load_meta(con, symbol: str) -> dict:
    """标的基础信息（``security`` 表；表空/无该票时给空 dict）。"""
    try:
        row = con.execute(
            "SELECT name, sec_type, board, is_st FROM security WHERE symbol = ?",
            [symbol],
        ).fetchone()
    except Exception:  # noqa: BLE001
        return {}
    if not row:
        return {}
    return {"name": row[0], "sec_type": row[1], "board": row[2],
            "is_st": bool(row[3]) if row[3] is not None else False}


def load_all(symbol: str, asof: date) -> MarketData:
    """把一次分析需要的全部数据读出来（单项失败不影响其他项）。"""
    from lquant.core.db import reader

    md = MarketData(symbol=symbol, asof=asof)
    md.bars = load_bars(symbol, asof)
    md.benchmark, md.benchmark_symbol = load_benchmark(asof)
    md.valuation, md.valuation_cross = load_valuation(symbol, asof)

    with reader() as con:
        md.name, md.sec_type, md.board, md.is_st = _meta_tuple(load_meta(con, symbol))
        md.industry_code, md.industry, peers = load_industry(con, symbol, asof)
        md.financial = load_financial_own(con, symbol, asof)
        md.money_flow = load_money_flow(con, symbol, asof)
        md.news = load_news(con, symbol, asof)
        # 行业截面把本票也算进去（分位应当含自己）
        peer_syms = sorted(set(peers) | {symbol})
        md.peer_count = len(peer_syms)
        md.financial_peers = load_financial_cross(con, peer_syms, asof)

    if md.bars.is_empty():
        md.notes.append(f"{symbol} 在 {asof} 之前没有日线数据")
    if md.financial.is_empty():
        md.notes.append("没有可用的 PIT 财务数据（先 `lq data financial` 回填）")
    return md


def _meta_tuple(meta: dict) -> tuple[str | None, str | None, str | None, bool]:
    return (meta.get("name"), meta.get("sec_type"),
            meta.get("board"), bool(meta.get("is_st", False)))
