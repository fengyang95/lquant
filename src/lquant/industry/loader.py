"""行业分析取数层。

三条硬约束与 :mod:`lquant.security.loader` 完全一致（同一个平台不该有两套口径）：

1. **PIT（无未来函数）**：行业归属按 ``industry_classify.std_date <= asof`` 解析；
   财务按 ``pub_date <= asof``；行情/估值/资金流按 ``trade_date <= asof``。
2. **空表是常态不是错误**：表没建 / 没同步 → 空帧 + ``notes``，绝不抛异常把
   整个分析打挂。数据湖可以只有行情没有财务。
3. **asof 缺省取湖内最新交易日**，而不是自然日。

**行业指数是自己合成的，不是外部指数。** 成分股按 PIT 归属选出后按**等权**
合成日收益 —— 口径透明、可复现、不依赖新数据源（申万官方行业指数日线需要
额外数据源）。代价是它与行情软件的行业指数涨跌幅不会完全一致（等权 ≠ 市值
加权），报告里明确标注口径，不含糊过去。

**为什么用 as-of join 而不是「取 asof 当天的成员回溯整段」**：后者等于用今天的
分类去解释历史涨跌 —— 一只 2025 年才调入该行业的票会被算进 2023 年的「行业
收益」，既是前视偏差，也让同一个行业的「历史」每次分类调整都被重写。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from lquant.core.logging import get_logger
from lquant.core.types import today_cn

log = get_logger(__name__)

#: 行情回看窗口（自然日）。需覆盖 120 日动量 / MA60 / 250 日波动，留足预热。
#: 行情回看窗口（自然日）。需覆盖 120 日动量 / MA60 / 250 日波动，**还要覆盖
#: RRG 相对旋转图**：RS-Ratio 需要 ``shift(220) + rolling(20)``、RS-Momentum 再叠
#: 一层 ``shift(60) + rolling(20)``，共 318 个交易日预热（约 1.3 年）。
#: 720 自然日 ≈ 490 个交易日，留足余量。
BAR_LOOKBACK_DAYS = 720

#: 估值历史回看窗口（自然日）——行业估值纵向分位需要足够长的历史。
VALUATION_LOOKBACK_DAYS = 1250

#: 资金流 / 涨停 / 新闻回看窗口（自然日）。
FLOW_LOOKBACK_DAYS = 60
LIMIT_LOOKBACK_DAYS = 60

#: 优先使用的行业分类标准（``industry_classify.std``）。取不到时回退到
#: 行数最多的那个 std，并在 ``notes`` 里写明实际用的是哪个。
PREFERRED_STD = ("SW", "sw1", "SW1", "CICS", "cics", "EM", "em")

#: 一个行业至少要有这么多成员才认为「行业代表性足够」。
MIN_MEMBERS = 5

#: 默认基准指数（相对强度的分母）。与个股分析同一条链，取第一个有数据的。
BENCHMARK_CANDIDATES: tuple[str, ...] = ("000300.SH", "000001.SH", "399006.SZ")

#: 缓存：全市场行业面板的构建代价几乎全在「读全市场日线」上，而这些结果在
#: 同一天内稳定。TTL 兜住盘中增量，条目上限防止按 asof 遍历吃光内存。
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 8
_PANEL_CACHE: dict[tuple, tuple[float, IndustryUniverse]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> IndustryUniverse | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _PANEL_CACHE.get(key)
        if hit is None:
            return None
        stamp, value = hit
        if now - stamp > _CACHE_TTL_SECONDS:
            _PANEL_CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple, value: IndustryUniverse) -> None:
    with _CACHE_LOCK:
        _PANEL_CACHE[key] = (time.monotonic(), value)
        if len(_PANEL_CACHE) > _CACHE_MAX_ENTRIES:
            oldest = min(_PANEL_CACHE, key=lambda k: _PANEL_CACHE[k][0])
            _PANEL_CACHE.pop(oldest, None)


def clear_industry_cache() -> None:
    """清空行业面板缓存（数据同步完成后调用，避免继续用 TTL 内的旧面板）。"""
    with _CACHE_LOCK:
        _PANEL_CACHE.clear()


@dataclass
class IndustryUniverse:
    """一次行业分析所需的**全市场**行业面板（两个端点共用一份）。"""

    asof: date
    std: str | None = None

    #: 全市场成员日线（已按 PIT 归属打上行业标签）
    #: ``trade_date, symbol, industry_code, industry_name, close, amount,
    #:   turnover_rate, ret``
    bars: pl.DataFrame = field(default_factory=pl.DataFrame)
    #: 行业日度聚合：``trade_date, industry_code, industry_name, ret, n,
    #:   amount, up_ratio, turnover_rate``
    daily: pl.DataFrame = field(default_factory=pl.DataFrame)
    #: asof 时点的成员清单：``symbol, industry_code, industry_name, std_date``
    membership: pl.DataFrame = field(default_factory=pl.DataFrame)
    #: 行业清单：``industry_code, industry_name, n_members``
    industries: pl.DataFrame = field(default_factory=pl.DataFrame)

    benchmark: pl.DataFrame = field(default_factory=pl.DataFrame)
    benchmark_symbol: str | None = None
    #: asof 当日全市场估值截面：``symbol, pe_ttm, pb_mrq, ps_ttm, dv_ttm, total_mv``
    valuation_cross: pl.DataFrame = field(default_factory=pl.DataFrame)

    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 基础设施


def _safe(fn, *args, **kwargs):
    """读底层数据的统一降级包装：任何异常 → None（由调用方转成空帧 + note）。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - 表未建 / 湖缺失 / 源不可用
        log.debug("industry loader 降级: %s", e)
        return None


def resolve_asof(asof: date | str | None) -> date:
    """确定观察日：显式给定优先，否则用湖内最新交易日。"""
    if asof is not None:
        if isinstance(asof, str):
            return date.fromisoformat(asof)
        return asof
    from lquant.data.store.parquet import latest_trade_date

    latest = latest_trade_date()
    return latest if latest is not None else today_cn()


_EMPTY_MEMBERSHIP = pl.DataFrame(schema={
    "symbol": pl.String, "std": pl.String, "industry_code": pl.String,
    "industry_name": pl.String, "std_date": pl.Date})


def _read_classify(con, asof: date) -> pl.DataFrame:
    """``std_date <= asof`` 的行业分类全表（原始形态，含 std 列）。"""
    try:
        return con.execute(
            "SELECT symbol, std, code, name, std_date FROM industry_classify "
            "WHERE std_date <= ?", [asof],
        ).pl()
    except Exception as e:  # noqa: BLE001 - 表未建 / 迁移中
        log.debug("industry_classify 读取失败: %s", e)
        return pl.DataFrame()


def available_stds(con, asof: date) -> list[str]:
    """湖里可用的行业分类标准（按行数降序）。空表返回 []。"""
    ic = _read_classify(con, asof)
    if ic.is_empty() or "std" not in ic.columns:
        return []
    counts = (ic.filter(pl.col("std").is_not_null())
                .group_by("std").len().sort("len", descending=True))
    return [r[0] for r in counts.iter_rows()]


def resolve_std(con, asof: date,
                requested: str | None = None) -> tuple[str | None, bool]:
    """确定用哪个行业分类标准。

    Returns:
        ``(实际使用的标准, 是否发生了回退)``。湖里一个标准都没有时返回
        ``(None, False)``。

    **显式请求的标准不存在时会回退到首选标准，但必须把「回退了」这件事报出去**
    —— 申万与中信的行业划分不同，静默换标准等于让同一份报告的口径在用户
    不知情的情况下变了（与 :func:`_load_benchmark` 不静默换基准同一条纪律）。
    """
    stds = available_stds(con, asof)
    if not stds:
        return None, False
    if requested and requested in stds:
        return requested, False
    for cand in PREFERRED_STD:
        if cand in stds:
            return cand, bool(requested)
    return stds[0], bool(requested)


def _membership_frame(ic: pl.DataFrame, std: str | None) -> pl.DataFrame:
    """原始分类表 → 标准化的成员表（PIT 用，保留全部历史行）。"""
    if ic.is_empty():
        return _EMPTY_MEMBERSHIP.clone()
    sub = ic
    if std is not None and "std" in sub.columns:
        sub = sub.filter(pl.col("std") == std)
    if sub.is_empty():
        return _EMPTY_MEMBERSHIP.clone()
    name = (pl.col("name").fill_null(pl.col("code"))
            if {"name", "code"} <= set(sub.columns) else pl.col("code"))
    return (sub.select([
        pl.col("symbol"),
        (pl.col("std") if "std" in sub.columns else pl.lit(std)).alias("std"),
        pl.col("code").alias("industry_code"),
        name.alias("industry_name"),
        pl.col("std_date"),
    ]).drop_nulls(["industry_code", "std_date"])
      .sort(["symbol", "std_date"]))


def load_membership(con, asof: date, std: str | None = None) -> pl.DataFrame:
    """asof 时点的行业归属（每个 symbol 取生效日最晚的一条）。"""
    ic = _membership_frame(_read_classify(con, asof), std)
    if ic.is_empty():
        return _EMPTY_MEMBERSHIP.clone()
    return (ic.group_by("symbol")
              .agg([
                  pl.col("std").sort_by("std_date").last(),
                  pl.col("industry_code").sort_by("std_date").last(),
                  pl.col("industry_name").sort_by("std_date").last(),
                  pl.col("std_date").max(),
              ])
              .sort("symbol"))


def list_industries(con, asof: date, std: str | None = None) -> pl.DataFrame:
    """行业清单 + 成员数（按成员数降序）。"""
    m = load_membership(con, asof, std)
    if m.is_empty():
        return pl.DataFrame(schema={"industry_code": pl.String,
                                    "industry_name": pl.String,
                                    "n_members": pl.Int64})
    return (m.group_by(["industry_code", "industry_name"])
             .agg(pl.len().alias("n_members"))
             .sort(["n_members", "industry_code"], descending=[True, False]))


def resolve_industry(con, identifier: str, asof: date,
                     std: str | None = None) -> tuple[str, str] | None:
    """按代码或名称定位行业。

    匹配优先级：代码精确 → 名称精确 → 名称包含（唯一命中才算）。
    匹配不到返回 ``None``（由上层给出可用名称清单的 hint）。
    """
    key = (identifier or "").strip()
    if not key:
        return None
    table = list_industries(con, asof, std)
    if table.is_empty():
        return None
    rows = table.to_dicts()
    for r in rows:
        if r["industry_code"] == key:
            return r["industry_code"], r["industry_name"]
    for r in rows:
        if r["industry_name"] == key:
            return r["industry_code"], r["industry_name"]
    hits = [r for r in rows if key in (r["industry_name"] or "")]
    if len(hits) == 1:
        return hits[0]["industry_code"], hits[0]["industry_name"]
    return None


# ---------------------------------------------------------------- 行情与行业指数


def _load_benchmark(asof: date, lookback_days: int = BAR_LOOKBACK_DAYS
                    ) -> tuple[pl.DataFrame, str | None]:
    """基准指数日线。与个股分析同一条降级纪律：**只在「没有数据」时换候选**，
    读取异常直接判不可用（静默换基准会让同一观察日的结论漂移）。"""
    from lquant.core.db import reader

    start = asof - timedelta(days=lookback_days)
    for code in BENCHMARK_CANDIDATES:
        try:
            with reader() as con:
                df = con.execute(
                    "SELECT trade_date, close FROM index_daily "
                    "WHERE symbol = ? AND trade_date >= ? AND trade_date <= ? "
                    "ORDER BY trade_date", [code, start, asof],
                ).pl()
        except Exception as e:  # noqa: BLE001
            log.warning("基准指数 %s 读取失败，不降级到其他基准: %s", code, e)
            return pl.DataFrame(), None
        if not df.is_empty():
            return df, code
    return pl.DataFrame(), None


def _read_market_bars(asof: date, lookback_days: int) -> pl.DataFrame:
    """全市场日线（仅分析需要的列）。"""
    from lquant.data.store.parquet import read_daily

    start = asof - timedelta(days=lookback_days)
    lf = _safe(read_daily, None, start=start, end=asof)
    if lf is None:
        return pl.DataFrame()
    cols = [c for c in ("trade_date", "symbol", "close", "high", "low",
                        "amount", "turnover_rate")
            if c in lf.collect_schema().names()]
    df = _safe(lambda: lf.select(cols).collect())
    if df is None or df.is_empty():
        return pl.DataFrame()
    return df.sort(["symbol", "trade_date"])


def _attach_industry(bars: pl.DataFrame, ic: pl.DataFrame) -> pl.DataFrame:
    """把 PIT 行业归属 as-of join 到日线上。

    ``join_asof(strategy="backward")``：每行取 ``std_date <= trade_date`` 中
    生效日最晚的一条 —— 与 :func:`lquant.fundamental.panel.resolve_industry`
    逐日等价，但一次向量化完成，且天然处理「窗口内发生行业变更」。
    """
    if bars.is_empty() or ic.is_empty():
        return pl.DataFrame()
    right = ic.select(["symbol", "std_date", "industry_code", "industry_name"])
    out = bars.join_asof(
        right, left_on="trade_date", right_on="std_date", by="symbol",
        strategy="backward", check_sortedness=False,
    )
    return out.drop_nulls(["industry_code"])


def _industry_daily(bars: pl.DataFrame) -> pl.DataFrame:
    """行业日度聚合：等权收益 / 中位收益 / 成交额 / 上涨占比 / 换手率。

    等权收益即「行业合成指数」的日收益 —— 一只票一份权重，不受个别巨头
    市值碾压。中位收益作为对照：两者背离说明行业内部分化严重（少数大票
    拉动），这个差异在报告里会点出来。
    """
    if bars.is_empty() or "ret" not in bars.columns:
        return pl.DataFrame()
    aggs = [
        pl.col("ret").mean().alias("ret"),
        pl.col("ret").median().alias("median_ret"),
        pl.len().alias("n"),
        (pl.col("ret") > 0).mean().alias("up_ratio"),
    ]
    for col in ("amount", "turnover_rate"):
        if col in bars.columns:
            aggs.append(
                (pl.col(col).sum() if col == "amount" else pl.col(col).mean())
                .alias(col))
        else:
            aggs.append(pl.lit(None, dtype=pl.Float64).alias(col))
    return (bars.drop_nulls(["ret"])
            .group_by(["trade_date", "industry_code", "industry_name"])
            .agg(aggs)
            .sort(["industry_code", "trade_date"]))


def build_universe(asof: date, std: str | None = None,
                   lookback_days: int = BAR_LOOKBACK_DAYS) -> IndustryUniverse:
    """构建（并缓存）一次行业分析所需的全部截面数据。"""
    key = (asof, std, lookback_days)
    hit = _cache_get(key)
    if hit is not None:
        return hit

    uni = _build_universe(asof, std, lookback_days)
    _cache_put(key, uni)
    return uni


def _build_universe(asof: date, std: str | None,
                    lookback_days: int) -> IndustryUniverse:
    from lquant.core.db import reader

    uni = IndustryUniverse(asof=asof)
    with reader() as con:
        uni.std, fell_back = resolve_std(con, asof, std)
        if fell_back:
            # 申万与中信的划分不同 —— 回退必须报出来，不能静默换口径
            uni.notes.append(
                f"请求的行业标准 {std!r} 在湖里不存在，已回退到 {uni.std!r}"
                "（两者行业划分不同，口径可能不一致）")
        ic = _membership_frame(_read_classify(con, asof), uni.std)
        uni.membership = load_membership(con, asof, uni.std)
        uni.industries = list_industries(con, asof, uni.std)

    if uni.std is None:
        uni.notes.append(
            "没有行业分类数据（industry_classify 为空）：先执行 "
            "`lq data industry` 或全量同步补 PIT 行业归属")

    bars = _read_market_bars(asof, lookback_days)
    if bars.is_empty():
        uni.notes.append("日线湖为空：行业指数与趋势角度都算不出来")
        return uni

    joined = _attach_industry(bars, ic)
    if joined.is_empty():
        uni.notes.append("行业归属为空，无法把个股日线聚合到行业")
        return uni
    if "close" not in joined.columns:
        uni.notes.append("日线湖缺 close 列，无法合成行业指数")
        return uni

    joined = joined.with_columns(
        pl.col("close").pct_change().over("symbol").alias("ret"))
    # 列裁剪要按实际存在的列走：不同 provider 的日线列集不完全一致
    # （amount / turnover_rate / high / low 都可能缺），缺列在这里补 NULL
    # 而不是让整条取数链抛 KeyError。
    wanted = ["trade_date", "symbol", "industry_code", "industry_name",
              "close", "high", "low", "amount", "turnover_rate", "ret"]
    joined = joined.select([
        pl.col(c) if c in joined.columns else pl.lit(None, dtype=pl.Float64).alias(c)
        for c in wanted
    ])
    uni.bars = joined
    uni.daily = _industry_daily(joined)

    uni.benchmark, uni.benchmark_symbol = _load_benchmark(asof, lookback_days)
    uni.valuation_cross = _load_valuation_cross(asof)
    return uni


def _load_valuation_cross(asof: date) -> pl.DataFrame:
    """asof 最近交易日的全市场估值截面（PE/PB/PS/股息率）。"""
    from lquant.data.store.parquet import read_daily_basic

    df = _safe(read_daily_basic, asof - timedelta(days=10), asof)
    if not isinstance(df, pl.DataFrame) or df.is_empty():
        return pl.DataFrame()
    if "trade_date" in df.columns:
        df = df.filter(pl.col("trade_date") == df["trade_date"].max())
    keep = [c for c in ("symbol", "pe_ttm", "pb_mrq", "ps_ttm", "dv_ttm",
                        "total_mv", "float_mv") if c in df.columns]
    return df.select(keep)


# ---------------------------------------------------------------- 单行业按需取数


def industry_bars(uni: IndustryUniverse, code: str) -> pl.DataFrame:
    """某行业的成员日线（PIT 归属后）。"""
    if uni.bars.is_empty():
        return pl.DataFrame()
    return uni.bars.filter(pl.col("industry_code") == code)


def industry_daily(uni: IndustryUniverse, code: str) -> pl.DataFrame:
    """某行业的日度聚合序列。"""
    if uni.daily.is_empty():
        return pl.DataFrame()
    return uni.daily.filter(pl.col("industry_code") == code).sort("trade_date")


def index_level(daily: pl.DataFrame) -> list[float]:
    """行业日收益序列 → 合成指数净值（起点 1.0）。"""
    level, cur = [], 1.0
    for r in daily["ret"].to_list():
        if r is None:
            level.append(cur)
            continue
        cur *= (1.0 + float(r))
        level.append(cur)
    return level


def member_symbols(uni: IndustryUniverse, code: str) -> list[str]:
    """某行业在 asof 时点的成员代码。"""
    if uni.membership.is_empty():
        return []
    return (uni.membership.filter(pl.col("industry_code") == code)["symbol"]
            .to_list())


def load_financials(symbols: list[str], asof: date) -> pl.DataFrame:
    """行业成员的 PIT 财务截面（每 symbol × item 取公告日最新的一条）。

    与 ``lquant.security.loader.load_financial_cross`` 同一口径；行业聚合用
    **中位数**而不是均值 —— 一个亏损巨头的净利同比能把均值拉到毫无意义的
    位置，中位数才是「这个行业典型公司过得怎么样」。

    返回**最近两个报告期**（``rn`` = 1 为最新、2 为上一期）：景气度不只看
    「现在好不好」，更看「在变好还是变坏」—— 环比动能需要上一期才有的比。
    """
    if not symbols:
        return pl.DataFrame()
    from lquant.core.db import reader
    from lquant.security.loader import FINANCIAL_ITEMS

    sph = ",".join("?" * len(symbols))
    ph = ",".join("?" * len(FINANCIAL_ITEMS))
    sql = f"""
        SELECT symbol, item, value, stat_date, pub_date, rn FROM (
            SELECT symbol, item, value, stat_date, pub_date,
                   row_number() OVER (PARTITION BY symbol, item
                                      ORDER BY stat_date DESC, pub_date DESC) AS rn
            FROM financial_pit
            WHERE symbol IN ({sph}) AND item IN ({ph}) AND pub_date <= ?
        ) WHERE rn <= 2
    """
    try:
        with reader() as con:
            return con.execute(sql, [*symbols, *FINANCIAL_ITEMS, asof]).pl()
    except Exception as e:  # noqa: BLE001
        log.debug("industry financials 读取失败: %s", e)
        return pl.DataFrame()


def load_valuation_history(symbols: list[str], asof: date,
                           lookback_days: int = VALUATION_LOOKBACK_DAYS
                           ) -> pl.DataFrame:
    """行业成员的估值历史（算行业估值纵向分位用）。"""
    if not symbols:
        return pl.DataFrame()
    from lquant.data.store.parquet import read_daily_basic

    df = _safe(read_daily_basic, asof - timedelta(days=lookback_days), asof,
               symbols=symbols)
    if not isinstance(df, pl.DataFrame) or df.is_empty():
        return pl.DataFrame()
    keep = [c for c in ("symbol", "trade_date", "pe_ttm", "pb_mrq") if c in df.columns]
    return df.select(keep).sort("trade_date")


def load_money_flow(symbols: list[str], asof: date,
                    lookback_days: int = FLOW_LOOKBACK_DAYS) -> pl.DataFrame:
    """行业成员的资金流（近 ``lookback_days`` 自然日，截至 asof）。"""
    if not symbols:
        return pl.DataFrame()
    from lquant.core.db import reader

    sph = ",".join("?" * len(symbols))
    try:
        with reader() as con:
            return con.execute(
                f"SELECT trade_date, symbol, main_net_inflow, main_net_ratio "
                f"FROM money_flow WHERE symbol IN ({sph}) "
                f"AND trade_date <= ? AND trade_date >= ?",
                [*symbols, asof, asof - timedelta(days=lookback_days)],
            ).pl()
    except Exception as e:  # noqa: BLE001 - 表未建 / 未同步
        log.debug("industry money_flow 读取失败: %s", e)
        return pl.DataFrame()


def load_limit_up(symbols: list[str], asof: date,
                  lookback_days: int = LIMIT_LOOKBACK_DAYS) -> pl.DataFrame:
    """行业成员的涨停记录（近 ``lookback_days`` 自然日）。"""
    if not symbols:
        return pl.DataFrame()
    from lquant.core.db import reader

    sph = ",".join("?" * len(symbols))
    try:
        with reader() as con:
            return con.execute(
                f"SELECT trade_date, symbol, name FROM limit_up_pool "
                f"WHERE symbol IN ({sph}) AND trade_date <= ? AND trade_date >= ?",
                [*symbols, asof, asof - timedelta(days=lookback_days)],
            ).pl()
    except Exception as e:  # noqa: BLE001
        log.debug("industry limit_up 读取失败: %s", e)
        return pl.DataFrame()


def load_market_financial_medians(asof: date,
                                  items: Sequence[str]) -> dict[str, float]:
    """全市场 PIT 财务中位数（``pub_date <= asof`` 的最新一期，按 item 分组）。

    行业景气度必须**相对全市场**看：全行业营收下滑的年份，同比 -5% 可能
    已经跑赢市场。这里只取需要的少数几个 item，SQL 里直接聚合，不在 Python
    侧拉全市场明细。
    """
    if not items:
        return {}
    from lquant.core.db import reader

    ph = ",".join("?" * len(items))
    sql = f"""
        SELECT item, median(value) AS med, count(*) AS n FROM (
            SELECT symbol, item, value,
                   row_number() OVER (PARTITION BY symbol, item
                                      ORDER BY stat_date DESC, pub_date DESC) AS rn
            FROM financial_pit
            WHERE item IN ({ph}) AND pub_date <= ?
        ) WHERE rn = 1 GROUP BY item
    """
    try:
        with reader() as con:
            rows = con.execute(sql, [*items, asof]).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("全市场财务中位数读取失败: %s", e)
        return {}
    return {r[0]: float(r[1]) for r in rows if r[0] is not None and r[1] is not None}


def load_security_names(symbols: Sequence[str]) -> dict[str, str]:
    """标的代码 → 简称（``security`` 表；表缺失/无该票时缺键不报错）。

    仅用于把「区间涨幅最大的成员」渲染成人能认的名字 —— 缺名字时前端
    退回展示代码，不影响任何计算。
    """
    if not symbols:
        return {}
    from lquant.core.db import reader

    sph = ",".join("?" * len(symbols))
    try:
        with reader() as con:
            rows = con.execute(
                f"SELECT symbol, name FROM security WHERE symbol IN ({sph})",
                list(symbols),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("security 名称读取失败: %s", e)
        return {}
    return {r[0]: r[1] for r in rows if r[0] and r[1]}


def market_amount_daily(uni: IndustryUniverse) -> pl.DataFrame:
    """全市场日成交额（各行业成交额之和）—— 拥挤度的分母。"""
    if uni.daily is None or uni.daily.is_empty():
        return pl.DataFrame()
    return (uni.daily.group_by("trade_date")
            .agg(pl.col("amount").sum().alias("amount"))
            .sort("trade_date"))


def load_flow_market(asof: date,
                     lookback_days: int = FLOW_LOOKBACK_DAYS) -> pl.DataFrame:
    """全市场资金流对照：同日全市场主力净流入合计。

    离开全市场口径就没法判断「这行业的资金流入算多还是少」—— 同一个数字
    在不同的市场环境下含义完全不同。返回 ``trade_date, main_net_inflow``。
    """
    from lquant.core.db import reader

    try:
        with reader() as con:
            return con.execute(
                "SELECT trade_date, sum(main_net_inflow) AS main_net_inflow "
                "FROM money_flow WHERE trade_date <= ? AND trade_date >= ? "
                "GROUP BY trade_date",
                [asof, asof - timedelta(days=lookback_days)],
            ).pl().sort("trade_date")
    except Exception as e:  # noqa: BLE001
        log.debug("全市场资金流读取失败: %s", e)
        return pl.DataFrame()


__all__ = [
    "BAR_LOOKBACK_DAYS",
    "BENCHMARK_CANDIDATES",
    "FLOW_LOOKBACK_DAYS",
    "IndustryUniverse",
    "LIMIT_LOOKBACK_DAYS",
    "MIN_MEMBERS",
    "PREFERRED_STD",
    "VALUATION_LOOKBACK_DAYS",
    "available_stds",
    "build_universe",
    "clear_industry_cache",
    "index_level",
    "industry_bars",
    "industry_daily",
    "list_industries",
    "load_financials",
    "load_flow_market",
    "load_limit_up",
    "load_market_financial_medians",
    "load_membership",
    "load_money_flow",
    "load_security_names",
    "load_valuation_history",
    "market_amount_daily",
    "member_symbols",
    "resolve_asof",
    "resolve_industry",
    "resolve_std",
]
