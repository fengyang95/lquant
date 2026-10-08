"""五层验证（细化方案 §3.8）：记录级之上的一切。

- L2 序列级：缺口、跳变、僵尸、复权错
- L3 截面级：覆盖度突降、整日未更新、日历对齐
- L4 语义级：涨跌停约束（一行断言抓 80% 的价格错误）、前后复权收益率恒等

每个 check 返回 list[Issue]，不直接抛异常 —— 由调用方按 severity 决定
阻断（fatal）还是落库告警（warn）。这是「标记而非删除」原则的上层入口。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import polars as pl

from lquant.data.quality.issues import Issue

__all__ = [
    "check_limit_breach", "check_ret_identity", "check_calendar_alignment",
    "check_coverage", "check_zombie", "check_adj_factor",
]

# 涨跌停约束容差：涨停价四舍五入到分带来的浮动
_LIMIT_TOL = 0.005
# 最小变动价位的一半（元）：涨跌停价要按 0.01 元取整，所以
# |close/pre_close - 1| 的合法上界是 limit + 0.005/pre_close。
# 低价股上这个量级远超 _LIMIT_TOL —— 实测 688496.SH 2026-09-10：
# pre_close 0.73、跌停价 0.584→取整 0.58 成交，收益率 0.2055 > 0.20+0.005，
# 被判成价格错误，其实是完全合法的跌停成交。
_TICK_HALF = 0.005

# 板性涨跌幅限制（非 ST）
_LIMIT_BY_BOARD = {"main": 0.10, "gem": 0.20, "star": 0.20, "bse": 0.30}
_ST_LIMIT = 0.05
# 上市初期无涨跌幅限制的**交易日**数。全湖经验证据（湖起点=上市日的标的，
# rank=上市第 N 个交易日，超档行数 / 总行数）：
#   main  r1 161/166  r2 16/166  r3 1  r4 2  r5 3  r6..r8 全 0
#   gem   r1 89/89    r2 13/89   r3 7  r4 5  r5 3  r6..r8 全 0
#   star  r1 52/53    r2 4/53    r3 1  r4 1  r5 2  r6..r8 全 0
#   bse   r1 3/3      r2 0（max 0.2999 ≈ 30% 档内）其余 0
# → 注册制后主板/双创新股上市后**前 5 个交易日**不设涨跌幅限制（主板 2023-02
# 前是老口径，湖内 2024 起的样本已全是新口径）；北交所只首日不设限、次日起 30%。
_FREE_DAYS = {"main": 5, "gem": 5, "star": 5, "bse": 1}
# 「窗口内首条 ≠ 上市首日」的判定容差（自然日）：窗口首个交易日与上市日
# 的间隔超过这个值，就认为窗口是从历史中途切进来的，rank 不再代表上市第
# 几个交易日。10 天覆盖周末 + 春节/国庆长假。
_LIST_GAP_DAYS = 10
# 基金（ETF/LOF）用最宽合法档：跟踪双创指数的 ETF 本身就是 ±20%，
# 而「跟踪哪个指数」不在 security 表里 —— 取宽档宁可少报也不误报
_FUND_LIMIT = 0.20
_FUND_TYPES = ("etf", "lof")


def board_from_code(symbol: str) -> str:
    """从代码段推板性（与 Symbol.board 同一口径，失败返回 'unknown'）。

    security.board 实测对**全部股票**都是 NULL（ETF/LOF/指数是 'unknown'），
    fill_null('main') 于是把创业板/科创板/北交所全按 10% 判 —— 2026-08 起
    一个月窗口的 1191 行 breach 里 1152 行是这么来的。代码段是权威且确定的
    信息源，不该依赖可能为空的快照列。
    """
    try:
        from lquant.core.types import Board, parse_symbol

        b = parse_symbol(symbol).board
        return b.value if b != Board.UNKNOWN else "unknown"
    except Exception:  # noqa: BLE001 - 解析失败按未知，不猜
        return "unknown"


def _derive_board(df: pl.DataFrame, security: pl.DataFrame) -> pl.DataFrame:
    """给每行标出实际生效的板性：代码段优先，代码段未知时用快照里的已知值。"""
    code_board = pl.col("symbol").map_elements(board_from_code,
                                               return_dtype=pl.String)
    expr = (
        pl.when(code_board != "unknown").then(code_board)
        .when(pl.col("board").is_not_null()
              & (pl.col("board") != "unknown")).then(pl.col("board"))
        .otherwise(pl.lit("unknown"))
        .alias("_board")
    )
    return df.with_columns(expr)


def check_limit_breach(df: pl.DataFrame, security: pl.DataFrame,
                       *, dataset: str = "daily_bar") -> list[Issue]:
    """涨跌停约束（L4）：|close/pre_close - 1| 不应超过板性限制 + 容差。

    除权日、新股上市初期会有合法越界 —— 按标的占比判定，而非逐行 fatal：
    占比 > 0.5% 判 error（复权/单位/串码类错误），少量命中打标告警。

    板性口径（2026-09-18 修正）：
    - 板性从**代码段**推（security.board 对股票全为 NULL，见 board_from_code）
    - 基金走最宽合法档；指数/债券不参与（点位不是价格）
    - 容差按最小变动价位修正：limit + _LIMIT_TOL + 0.005/pre_close
    - 上市初期（双创前 5 个交易日、主板/北交所首日）无涨跌幅限制 → 豁免。
      豁免只在「窗口从该标的上市起就完整可见」时生效（窗口首条与上市日
      间隔 ≤ _LIST_GAP_DAYS），否则 rank 只是「窗口内第几条」，无法确认
      它到底上市几天了 —— 宁可不豁免。

    实测（2026-08-01~09-17，229376 行）：修前 1191 行命中，修后 0 行；
    pipeline 实际窗口（近 400 个自然日，1645610 行）同样 0 行。
    已知局限（PIT，两条）：
    - security 表只有当前 is_st 快照，历史区间会用今天的口径回看 ——
      调用方应只在近期窗口上跑本检查（见 pipeline._recent_window）
    - 主板免限天数统一取 5（注册制口径）。2023-02 前上市的主板新股首日
      只有 ±44%、次日起 10%，对这批老样本会少报；但本检查只跑近期窗口，
      老样本不在范围内，为此引入时间闸门只会把 2021/2022 的合法首日行
      重新变成噪声（实测 ~100 行/年）。
    """
    need = {"symbol", "close", "pre_close"}
    if not need <= set(df.columns) or not len(df) or not len(security):
        return []
    keep = [c for c in ("symbol", "sec_type", "board", "is_st", "list_date")
            if c in security.columns]
    meta = security.select(keep).unique(subset=["symbol"])
    if "sec_type" not in meta.columns:
        meta = meta.with_columns(pl.lit("stock").alias("sec_type"))
    if "list_date" not in meta.columns:
        meta = meta.with_columns(pl.lit(None, dtype=pl.Date).alias("list_date"))
    if "board" not in meta.columns:
        meta = meta.with_columns(pl.lit(None, dtype=pl.String).alias("board"))

    joined = (
        df.join(meta, on="symbol", how="left")
        .with_columns(
            pl.col("close").cast(pl.Float64, strict=False).alias("_close"),
            pl.col("pre_close").cast(pl.Float64, strict=False).alias("_pre"),
        )
        .pipe(_derive_board, security=meta)
        .with_columns(
            # 该行是该标的序列里的第几个交易日（1 基）；窗口是滑动后缀，
            # 「窗口内第 1 条」不等于「上市第 1 个交易日」，所以还要下面的
            # 上市日闸门。
            pl.col("trade_date").rank("ordinal").over("symbol").alias("_rank"),
            pl.col("trade_date").min().over("symbol").alias("_first_seen"),
            pl.col("sec_type").fill_null("stock").alias("_sec"),
            pl.col("is_st").fill_null(False).alias("_st"),
        )
        .with_columns(
            # 板性限制向量化：ST 5% / 双创 20% / 北交所 30% / 主板 10%
            pl.when(pl.col("_sec").is_in(["index", "bond"]))
            .then(pl.lit(None, dtype=pl.Float64))
            .when(pl.col("_sec").is_in(_FUND_TYPES)).then(pl.lit(_FUND_LIMIT))
            .when(pl.col("_st")).then(pl.lit(_ST_LIMIT))
            .otherwise(pl.col("_board").replace_strict(_LIMIT_BY_BOARD,
                                                       default=None))
            .alias("_limit")
        )
        .with_columns(
            pl.when(pl.col("_sec").is_in(["index", "bond"]))
            .then(pl.lit(None, dtype=pl.Int64))
            .otherwise(pl.col("_board").replace_strict(_FREE_DAYS, default=0))
            .fill_null(0)
            .alias("_free_days")
        )
        .with_columns(
            # 上市初期豁免：窗口从该标的上市起就基本完整可见时才成立。
            # 判据是「窗口内首个交易日与上市日的间隔在假期容差内」——
            # 比「list_date >= 窗口起点」精确：窗口是滑动后缀，IPO 正好落在
            # 窗口边界前一天时（实测 301632.SZ，上市 2025-08-12、窗口起
            # 08-13），旧判据会把上市第 2 个交易日（双创前 5 日不设限，当日
            # +28.2% 完全合法）误报成超档。
            (pl.col("list_date").is_not_null()
             & ((pl.col("_first_seen") - pl.col("list_date")).dt.total_days()
                <= _LIST_GAP_DAYS)
             & (pl.col("_rank") <= pl.col("_free_days"))).alias("_new_listing"),
            # 容差随价格走：涨跌停价取整到分
            (pl.col("_limit") + _LIMIT_TOL + _TICK_HALF / pl.col("_pre"))
            .alias("_thr"),
        )
        .with_columns(
            ((pl.col("_close") / pl.col("_pre") - 1).abs() > pl.col("_thr"))
            .fill_null(False).alias("_over"),
            pl.col("_new_listing").fill_null(False),
        )
        .with_columns(
            # 上市初期豁免必须真的扣掉：只算不扣会让「豁免逻辑」形同虚设
            (pl.col("_over") & ~pl.col("_new_listing")).alias("_breach")
        )
    )
    # _limit 为 null（指数/债券）时 _thr 也是 null，比较得 null →
    # fill_null(False) 保证它们不会进 bad
    bad = joined.filter(pl.col("_breach"))
    if not len(bad):
        return []
    n = len(df)
    issues = [
        Issue(rule="LIMIT_BREACH",
              severity="error" if len(bad) / n > 0.005 else "warn",
              dataset=dataset,
              detail=f"{len(bad)}/{n} 行日收益超涨跌停限制（代码段板性 + ST + "
                     f"基金宽档 + 新股豁免 + 取整容差），"
                     f"疑似复权、单位或串码错误",
              count=len(bad),
              extra={"symbols": bad["symbol"].unique().to_list()[:20]}),
    ]
    return issues


def check_ret_identity(qfq_close: pl.Series, hfq_close: pl.Series,
                       *, symbol: str = "", dataset: str = "daily_bar",
                       atol: float = 1e-9) -> list[Issue]:
    """前后复权收益率恒等（L4，杀手锏）：前复权与后复权只差常数因子，
    日收益率序列在数学上必须完全相同。不通过必然是复权因子错。"""
    q = qfq_close.cast(pl.Float64, strict=False)
    h = hfq_close.cast(pl.Float64, strict=False)
    if len(q) != len(h) or len(q) < 2:
        return []
    # 两列拼成 frame 后整行丢弃空值 —— 各自独立 drop_nulls 会让序列错位，
    # 把合法数据错判成复权错误（停牌缺口不在同一行时）
    both = pl.DataFrame({"q": q, "h": h}).drop_nulls()
    if len(both) < 2:
        return []
    ret_q = both["q"] / both["q"].shift(1) - 1
    ret_h = both["h"] / both["h"].shift(1) - 1
    pair = pl.DataFrame({"rq": ret_q, "rh": ret_h}).drop_nulls()
    if not len(pair):
        return []
    diff = (pair["rq"] - pair["rh"]).abs().max()
    if diff is not None and diff > atol:
        return [Issue(rule="ADJ_RET_IDENTITY", severity="error", dataset=dataset,
                      symbol=symbol or None,
                      detail=f"前后复权收益率不一致 max|Δret|={diff:.3e} > {atol}，"
                             f"复权因子必有错误",
                      count=1)]
    return []


def check_calendar_alignment(dates: list[date], calendar: list[date],
                             *, dataset: str = "daily_bar") -> list[Issue]:
    """交易日历对齐（L3）：数据日期必须是官方交易日子集；完整年度交易日 240–245。

    年度天数只检查「内部年」—— 窗口首尾年份天然不满一年
    （跨年切片 / 进行中的当年），对它们断言必然产生永久性假 fatal。
    """
    issues: list[Issue] = []
    cal = set(calendar)
    if cal:
        stray = sorted(set(dates) - cal)
        if stray:
            issues.append(Issue(
                rule="CALENDAR_STRAY", severity="fatal", dataset=dataset,
                detail=f"{len(stray)} 个数据日期不在官方交易日历内"
                       f"（首例 {stray[0]}）—— 日历错或日期错位",
                count=len(stray), extra={"dates": [str(d) for d in stray[:10]]}))
    years = sorted({d.year for d in calendar})
    interior = set(years[1:-1]) if len(years) > 2 else set()
    by_year: dict[int, int] = defaultdict(int)
    for d in cal:
        by_year[d.year] += 1
    bad_years = {y: n for y, n in by_year.items()
                 if y in interior and not 240 <= n <= 245}
    if bad_years:
        issues.append(Issue(
            rule="CALENDAR_YEAR_LEN", severity="fatal", dataset=dataset,
            detail=f"交易日历年度天数异常（完整年应为 240–245）: {bad_years}",
            count=len(bad_years), extra={"years": bad_years}))
    return issues


def check_coverage(df: pl.DataFrame, *, min_ratio: float = 0.90,
                   dataset: str = "daily_bar") -> list[Issue]:
    """覆盖度（L3，fatal）：单日标的数相对全窗口中位数的占比。

    分母用「本湖自身的每日标的数中位数」而不是当前 active_symbols ——
    历史日期的合理标的数本来就少（未上市 / 已退市），拿今天的活跃
    清单当分母会把每一段历史都判成批量缺数（假 fatal 淹没真信号）。
    中位数口径只抓「突降」：某天比常态少 10% 以上 = 疑似批量缺数。
    """
    if not len(df):
        return []
    per_day = (
        df.group_by("trade_date")
        .agg(pl.col("symbol").n_unique().alias("n"))
        .sort("trade_date")
    )
    counts = per_day["n"]
    median = counts.median()
    if median is None or median <= 0:
        return []
    issues: list[Issue] = []
    for d, n in zip(per_day["trade_date"], counts, strict=False):
        ratio = n / median
        if ratio < min_ratio:
            issues.append(Issue(
                rule="COVERAGE", severity="fatal", dataset=dataset, trade_date=d,
                detail=f"{d} 覆盖率 {ratio:.1%} < {min_ratio:.0%}（{n} 只，"
                       f"窗口中位 {median:.0f} 只），疑似批量缺数",
                count=int(median - n)))
    return issues


def check_zombie(df: pl.DataFrame, *, warn_ratio: float = 0.08,
                 error_ratio: float = 0.30, dataset: str = "daily_bar") -> list[Issue]:
    """僵尸检测（L3）：全市场某日 |ret| == 0 占比。正常 < 8%，
    > 30% 说明源站没更新（整日复制的旧数据）。"""
    if not {"symbol", "close"} <= set(df.columns) or not len(df):
        return []
    zero_ret = (
        df.sort(["symbol", "trade_date"])
        .with_columns((pl.col("close") == pl.col("close").shift(1).over("symbol"))
                      .alias("_zero"))
        .filter(pl.col("_zero").fill_null(False))
        .group_by("trade_date").agg(pl.len().alias("n_zero"))
    )
    per_day = df.group_by("trade_date").agg(pl.len().alias("n")).join(
        zero_ret, on="trade_date", how="inner").with_columns(
        (pl.col("n_zero") / pl.col("n")).alias("ratio"))
    issues: list[Issue] = []
    for d, ratio in zip(per_day["trade_date"], per_day["ratio"], strict=False):
        if ratio >= error_ratio:
            issues.append(Issue(
                rule="ZOMBIE_DAY", severity="error", dataset=dataset, trade_date=d,
                detail=f"{d} 零收益占比 {ratio:.0%} ≥ {error_ratio:.0%}，"
                       f"源站疑似未更新（僵尸日）", count=1))
        elif ratio >= warn_ratio:
            issues.append(Issue(
                rule="ZOMBIE_DAY", severity="warn", dataset=dataset, trade_date=d,
                detail=f"{d} 零收益占比 {ratio:.0%} ≥ {warn_ratio:.0%}", count=1))
    return issues


def check_adj_factor(df: pl.DataFrame, *, dataset: str = "daily_bar") -> list[Issue]:
    """复权因子（L2）：后复权因子应随时间单调不减；跳变 >50% 需与除权事件比对。"""
    if not {"symbol", "trade_date", "adj_factor"} <= set(df.columns) or not len(df):
        return []
    s = df.sort(["symbol", "trade_date"]).with_columns(
        pl.col("adj_factor").shift(1).over("symbol").alias("_prev"))
    decreasing = s.filter(
        pl.col("_prev").is_not_null() & (pl.col("adj_factor") < pl.col("_prev") - 1e-12))
    jump = s.filter(
        pl.col("_prev").is_not_null()
        & (((pl.col("adj_factor") / pl.col("_prev")) - 1).abs() > 0.5))
    issues: list[Issue] = []
    if len(decreasing):
        issues.append(Issue(
            rule="ADJ_DECREASE", severity="error", dataset=dataset,
            detail=f"{len(decreasing)} 行后复权因子不单调（分红送转只会让它变大）",
            count=len(decreasing),
            extra={"symbols": decreasing["symbol"].unique().to_list()[:20]}))
    if len(jump):
        issues.append(Issue(
            rule="ADJ_JUMP", severity="warn", dataset=dataset,
            detail=f"{len(jump)} 行复权因子跳变 >50%，疑似除权，需与除权事件比对",
            count=len(jump),
            extra={"symbols": jump["symbol"].unique().to_list()[:20]}))

    # 覆盖度：缺失/非正因子是**静默**的口径错误来源 —— 复权序列里混入原始价，
    # 收益率凭空跳变。这里按标的报，而不是只报行数（要知道该去补哪只）。
    missing = df.filter(
        pl.col("adj_factor").is_null()
        | (pl.col("adj_factor").cast(pl.Float64) <= 0))
    if len(missing):
        bad_syms = missing["symbol"].unique().to_list()
        rate = len(missing) / len(df)
        issues.append(Issue(
            rule="ADJ_MISSING", severity="warn" if rate < 0.05 else "error",
            dataset=dataset,
            detail=(f"{len(missing)}/{len(df)} 行（{rate:.1%}）无有效复权因子，"
                    f"涉及 {len(bad_syms)} 只标的；这些标的的 fq 复权会退回原始价"),
            count=len(missing),
            extra={"symbols": bad_syms[:20], "n_symbols": len(bad_syms)}))
    return issues
