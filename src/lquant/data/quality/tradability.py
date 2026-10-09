"""可交易性打标：停牌/新股/ST，以及「上市初期无涨跌幅约束」纯规则。

这三类日子不是坏数据，而是「规则不同」：停牌日无成交价格不可信，
新股波动与涨跌停口径特殊，ST 涨跌幅限制只有 5%。直接删除会撕出
价格缺口，打标让下游按需过滤（如回测剔除或调整成本模型）。

本模块同时承载**无涨跌幅窗口**（IPO 上市初期）的纯规则：它是回测
「当日是否免涨跌停约束」判定的唯一事实源，per-instrument 的静态标记
（InstrumentRules.no_price_limit）算不出「第几个交易日」，只有逐日判定
才能给出正确答案。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.types import Board, SecType, parse_symbol
from lquant.data.quality.flags import NEW_LISTING, ST_RISK, SUSPENDED, hit, or_flags

__all__ = [
    "BSE_NO_LIMIT_CALENDAR_MARGIN",
    "BSE_NO_LIMIT_TRADING_DAYS",
    "GEM_REGISTRATION_DATE",
    "IPO_NO_LIMIT_CALENDAR_MARGIN",
    "IPO_NO_LIMIT_TRADING_DAYS",
    "MAIN_REGISTRATION_DATE",
    "NoLimitWindow",
    "STAR_REGISTRATION_DATE",
    "flag_tradability",
    "is_no_limit_day",
    "no_limit_calendar_margin_days",
    "no_limit_window",
    "no_limit_window_days",
    "parse_listing_date",
]

# 上市未满此天数视为新股（波动、流动性、涨跌停口径均特殊）
DEFAULT_MIN_LISTED_DAYS = 120

# ---------------------------------------------------------------------------
# 上市初期「无涨跌幅限制」窗口（注册制新股）
#
# 口径来源：沪深北交易所交易规则 + 全面注册制改革公告。各板块的
# **政策生效日**必须精确 —— 用「上市日」而不是「当前日」去比：
#   - 科创板：2019-07-22 开板即注册制，上市后前 5 个交易日无涨跌幅
#   - 创业板：2020-08-24 注册制首批起，前 5 个交易日无涨跌幅
#   - 沪深主板：2023-02-17 全面注册制起，前 5 个交易日无涨跌幅
#   - 北交所：2021-11-15 开市，仅上市**首日**无涨跌幅（次日起 ±30%）
# 生效日**之前**上市的老股，无「前 5 日」待遇，只按上市首日处理。
# 与 `/tmp/tick-stock-panel`（MIT）参考实现的一处口径差异：参考实现把
# 注册制前上市的老股窗口记为 0（即首日也按板块涨跌幅），本实现按任务
# 口径记为「仅首日」。差异只影响「注册制之前上市标的的上市首日本身」，
# 对全区间回测的其余日子没有影响；若后续要改成 0，只需改
# `no_limit_window_days` 里那一行 return。
# ---------------------------------------------------------------------------
STAR_REGISTRATION_DATE = date(2019, 7, 22)     # 科创板开板（注册制起点）
GEM_REGISTRATION_DATE = date(2020, 8, 24)      # 创业板注册制首批
MAIN_REGISTRATION_DATE = date(2023, 2, 17)     # 沪深主板全面注册制
BSE_OPENING_DATE = date(2021, 11, 15)          # 北交所开市

# 主板/创业板/科创板：上市后前 5 个交易日（含首日）；北交所：仅首日
IPO_NO_LIMIT_TRADING_DAYS = 5
BSE_NO_LIMIT_TRADING_DAYS = 1

# 拿不到「该标的自己的交易日序列」时的**保守日历天边际**：
# 5 个交易日最坏跨约 15 个日历天（含春节/国庆连休），1 个交易日跨 3 天。
# 这里的取舍是「宁多标勿漏标」：把窗口外多标一天，后果只是漏掉一次
# 「涨停不可买」的拒单；而把真正的无涨跌幅日漏判成涨停，会把根本买不
# 进去的收益算进回测（方向相反、且系统性高估），代价高得多。
# 因此近似路径只能比真实窗口**更宽**，绝不能更窄。
# 注意：这条边际是按**板块**给的，注册制前上市的老股（窗口只有首日）
# 在退化为日历估算时同样享受 15/3 天边际 —— 属于有意的「过标」。
# 精确路径（调用方给出该标的的交易日序号 day_rank）不受此影响，
# 回测引擎正是走精确路径。
IPO_NO_LIMIT_CALENDAR_MARGIN = 15
BSE_NO_LIMIT_CALENDAR_MARGIN = 3


@dataclass(frozen=True)
class NoLimitWindow:
    """某交易日「是否无涨跌幅约束」的判定结果（含判定质量信息）。

    单返回一个 bool 会把「确定不在窗口」和「因为缺上市日而无法判定」
    混成同一个 False，调用方无从区分。这里的 resolved/estimated 就是
    给调用方看的：
      - resolved=False → listing_date 缺失/非法，**无法判定**，按保守处理
        （即照常施加涨跌停约束，no_limit 恒为 False）；
      - estimated=True → 没有精确的交易日序号，用日历天保守边际估算，
        窗口可能被**高估**（宁多标勿漏标）。
    """

    no_limit: bool               # 该交易日是否免涨跌停约束
    window_days: int = 0         # 该板块该上市日对应的窗口交易日数
    day_rank: int | None = None  # 是上市后第几个交易日（首日=1），None=未知
    estimated: bool = False      # 是否走了日历天保守估算路径
    resolved: bool = True        # False=listing_date 缺失/非法，无法判定
    reason: str = ""             # 判定依据，供日志/排查使用

    def __bool__(self) -> bool:  # 方便 `if verdict:` 直接当布尔用
        return self.no_limit


def parse_listing_date(value: object) -> date | None:
    """上市日 → date；兼容 date / datetime / ISO 字符串 / None。

    非法值一律返回 None（由调用方按「无法判定」保守处理），绝不猜。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _board_of(symbol: str) -> Board:
    """标的 → 板块；解析失败时退化为代码段匹配（不抛异常）。

    非股票（指数/ETF/LOF/债券）一律判为 UNKNOWN —— 新股「上市前 N 个
    交易日」是股票规则，指数与基金不适用；否则 000300.SH 这类指数会被
    代码段误判成主板。
    """
    raw = str(symbol).strip().upper()
    try:
        sym = parse_symbol(raw)
    except ValueError:
        pass
    else:
        if sym.sec_type is not SecType.STOCK:
            return Board.UNKNOWN
        return sym.board
    code = raw.split(".")[0]
    if code.startswith(("688", "689")):
        return Board.STAR
    if code.startswith(("300", "301", "302")):
        return Board.GEM
    if code.startswith(("4", "8", "92")):
        return Board.BSE
    if code.startswith(("6", "0")):
        return Board.MAIN
    return Board.UNKNOWN


def no_limit_window_days(symbol: str, listing_date: date | None) -> int:
    """上市初期无涨跌幅窗口长度（交易日数）；0 = 该标的/该上市日不适用。

    判定同时用到**板块**与**上市日**：同样是主板，2023-02-17 前上市的
    只有首日窗口，之后上市的前 5 个交易日都无涨跌幅。只看板块（或只看
    当前日期）都会把老股误判成有 5 日窗口。
    """
    if listing_date is None:
        return 0
    board = _board_of(symbol)
    if board is Board.BSE:
        # 北交所开市即注册制，无论何时上市都只有首日窗口
        return BSE_NO_LIMIT_TRADING_DAYS
    onset = {
        Board.STAR: STAR_REGISTRATION_DATE,
        Board.GEM: GEM_REGISTRATION_DATE,
        Board.MAIN: MAIN_REGISTRATION_DATE,
    }.get(board)
    if onset is None:
        # 未知板块（ETF/指数/债券等）不享受新股无涨跌幅窗口：
        # 它们本就不按「上市前 N 日」这套股票规则走
        return 0
    return IPO_NO_LIMIT_TRADING_DAYS if listing_date >= onset else 1


def no_limit_calendar_margin_days(symbol: str) -> int:
    """窗口的日历天保守边际（北交所 3 天，其余 15 天）。"""
    return (BSE_NO_LIMIT_CALENDAR_MARGIN if _board_of(symbol) is Board.BSE
            else IPO_NO_LIMIT_CALENDAR_MARGIN)


def no_limit_window(symbol: str, listing_date: object, trade_date: date, *,
                    day_rank: int | None = None,
                    resume_first_day: bool = False,
                    st_change_day: bool = False) -> NoLimitWindow:
    """判定 `trade_date` 是否无涨跌幅约束（IPO 窗口为主，另两类留接口）。

    参数
    ----
    symbol        : `600000.SH` / `830799` 等（板块由代码段推断）
    listing_date  : 上市日；缺失/非法 → resolved=False、no_limit=False
    trade_date    : 待判定的交易日
    day_rank      : trade_date 在**该标的自己的交易日序列**里的序号
                    （上市首日=1）。给了它就走精确路径；None 则退化为
                    日历天保守估算（estimated=True，窗口宁宽勿窄）。
    resume_first_day / st_change_day
                  : 复牌首日 / ST 变更日。**接口先留好**：日线湖目前没有
                    这两类逐日标记的数据来源（security 表无对应日期维度），
                    等数据层提供后由调用方传入即可，本函数无需再改。
    """
    if resume_first_day or st_change_day:
        # 这两类同样是「某一天」的 per-day 事实，语义与 IPO 首日一致：
        # 当日不受涨跌停约束。当前调用方拿不到数据，故恒为 False。
        return NoLimitWindow(
            no_limit=True, window_days=1, resolved=True,
            reason="resume_first_day" if resume_first_day else "st_change_day")

    listing = parse_listing_date(listing_date)
    if listing is None:
        # 不猜：缺上市日就只能照常施加涨跌停约束，并显式告诉调用方「无法判定」
        return NoLimitWindow(no_limit=False, resolved=False,
                             reason="listing_date_missing")
    window_days = no_limit_window_days(symbol, listing)
    if window_days <= 0:
        return NoLimitWindow(no_limit=False, resolved=True,
                             reason="board_not_applicable")
    if trade_date < listing:
        # 早于上市日的 bar 属数据异常，不享受任何窗口
        return NoLimitWindow(no_limit=False, window_days=window_days,
                             resolved=True, reason="before_listing")
    if day_rank is not None and day_rank >= 1:
        inside = day_rank <= window_days
        return NoLimitWindow(no_limit=inside, window_days=window_days,
                             day_rank=day_rank, resolved=True,
                             reason="in_window" if inside else "after_window")
    # 退化路径：没有该标的的交易日序列，用日历天保守边际估算窗口
    boundary = listing + timedelta(days=no_limit_calendar_margin_days(symbol))
    inside = trade_date <= boundary
    return NoLimitWindow(no_limit=inside, window_days=window_days,
                         estimated=True, resolved=True,
                         reason="calendar_margin" if inside else "after_margin")


def is_no_limit_day(symbol: str, listing_date: object, trade_date: date, *,
                    day_rank: int | None = None,
                    resume_first_day: bool = False,
                    st_change_day: bool = False) -> bool:
    """`no_limit_window` 的布尔简写（细节请用 no_limit_window 取回）。"""
    return no_limit_window(
        symbol, listing_date, trade_date, day_rank=day_rank,
        resume_first_day=resume_first_day, st_change_day=st_change_day).no_limit


def _to_date(v: date | str) -> date:
    """str / date 统一转 python date（可安全参与 polars 日期运算）。"""
    return v if isinstance(v, date) else date.fromisoformat(v)


def _listing_expr(df: pl.DataFrame, listing_dates: dict | None,
                  min_listed_days: int) -> pl.Expr | None:
    """NEW_LISTING 判定：listing_date 列优先，缺列用 listing_dates（symbol → 上市日）。"""
    if "listing_date" in df.columns and "trade_date" in df.columns:
        listed = pl.col("listing_date").cast(pl.Date)
    elif listing_dates and "trade_date" in df.columns:
        # 未出现在 dict 里的 symbol 视为 null（跳过判定）
        days = [
            pl.when(pl.col("symbol") == sym)
            .then((pl.col("trade_date").cast(pl.Date) - _to_date(d))
                  .dt.total_days())
            for sym, d in listing_dates.items()
        ]
        listed_days = pl.coalesce(days)
        return hit(NEW_LISTING, listed_days.is_not_null()
                   & (listed_days < min_listed_days))
    else:
        return None
    listed_days = (pl.col("trade_date").cast(pl.Date) - listed).dt.total_days()
    return hit(NEW_LISTING, listed_days.is_not_null()
               & (listed_days < min_listed_days))


def _st_expr(df: pl.DataFrame, st_ranges: dict | None) -> pl.Expr | None:
    """ST_RISK 判定：is_st 列优先，缺列用 st_ranges（symbol → [(start, end), ...]）。"""
    if "is_st" in df.columns:
        return hit(ST_RISK, pl.col("is_st").fill_null(False))
    if not st_ranges or "trade_date" not in df.columns:
        return None
    # 逐 symbol 展开区间，行级判定：落在任一区间内即打标
    conds: list[pl.Expr] = []
    for sym, ranges in st_ranges.items():
        in_ranges = pl.lit(False)
        for start, end in ranges:
            in_ranges = in_ranges | pl.col("trade_date").cast(pl.Date) \
                .is_between(pl.lit(_to_date(start)), pl.lit(_to_date(end)))
        conds.append((pl.col("symbol") == sym) & in_ranges)
    if not conds:
        return None
    return hit(ST_RISK, pl.any_horizontal(conds))


def flag_tradability(df: pl.DataFrame, *, listing_dates: dict | None = None,
                     st_ranges: dict | None = None,
                     min_listed_days: int = DEFAULT_MIN_LISTED_DAYS) -> pl.DataFrame:
    """打标 SUSPENDED / NEW_LISTING / ST_RISK（缺输入时跳过对应检查）。

    - SUSPENDED：volume == 0（当日零成交）
    - NEW_LISTING：trade_date - listing_date < min_listed_days，
      优先用 df 的 listing_date 列，缺列用 listing_dates（symbol → 上市日）dict
    - ST_RISK：优先用 df 的 is_st 列（bool），缺列用 st_ranges
      （symbol → [(start, end), ...]）dict
    """
    flags: list[pl.Expr] = []
    if "volume" in df.columns:
        flags.append(hit(SUSPENDED, pl.col("volume") == 0))
    for expr in (_listing_expr(df, listing_dates, min_listed_days),
                 _st_expr(df, st_ranges)):
        if expr is not None:
            flags.append(expr)
    if not flags:
        # 已有 quality_flags（先前检查打的位）必须保留，不能覆盖成 0
        if "quality_flags" in df.columns:
            return df
        return df.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
    return or_flags(df, *flags)
