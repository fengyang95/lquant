"""演示数据生成器：无网络环境下的开箱即用。

为什么需要它：
- BaoStock 走原始 TCP（常被公司网络/代理 TUN 阻断），东财 HTTP 也可能被拦。
- 平台的价值链路（看板/因子/回测）不该被数据源卡死。
- 合成数据格式与真实数据完全一致（同一 schema、同一质量断言），
  换真数据时零改动 —— `lq data sync` 直接覆盖。

方法：几何布朗运动 + 跳空，代码全部用真实存在的格式（600xxx.SH / 000xxx.SZ /
510xxx.SH ETF），但数据是合成的。**绝不用于真实回测结论。**

**自洽是硬要求**：演示环境要能跑通仿真里的**全部**下游，否则「缺数据」会被
误读成「功能坏了」。两道曾经缺失的地基（2026-10-06 CI 全红的原因）：
``float_mv``（协变量 market_cap 的来源，DAILY_BAR schema 里有、演示数据里没有）
与 ``industry_classify``（申万行业，协变量 industry_sw1 的来源，演示建库后是空表）。
缺了它们，因子报告的「归因分解 / 风格相关性体检」不会报错，只会安静地整块消失。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from lquant.core.types import now_cn, today_cn
from lquant.data.store.catalog import (
    EtfMetaRepo,
    FinancialRepo,
    IndustryClassifyRepo,
    SecurityRepo,
    TradeCalendarRepo,
)
from lquant.data.store.parquet import write_daily, write_daily_basic

STOCK_CODES = [
    ("600000.SH", "浦发银行"), ("600036.SH", "招商银行"), ("600519.SH", "贵州茅台"),
    ("600887.SH", "伊利股份"), ("601318.SH", "中国平安"), ("601899.SH", "紫金矿业"),
    ("000001.SZ", "平安银行"), ("000002.SZ", "万科A"), ("000333.SZ", "美的集团"),
    ("000651.SZ", "格力电器"), ("002594.SZ", "比亚迪"), ("300750.SZ", "宁德时代"),
    ("688111.SH", "金山办公"), ("603259.SH", "药明康德"), ("600276.SH", "恒瑞医药"),
    ("000858.SZ", "五粮液"), ("002415.SZ", "海康威视"), ("600900.SH", "长江电力"),
    ("601088.SH", "中国神华"), ("600030.SH", "中信证券"),
]

# (代码, 名称, sellable_after_days)：T+0/T+1 用真实规则标注
ETF_CODES = [
    ("510300.SH", "沪深300ETF", 1), ("510050.SH", "上证50ETF", 1),
    ("588000.SH", "科创50ETF", 1), ("159915.SZ", "创业板ETF", 1),
    ("513100.SH", "纳指ETF", 0), ("513050.SH", "中概互联ETF", 0),
    ("512880.SH", "证券ETF", 1), ("512690.SH", "酒ETF", 1),
    ("511260.SH", "十年国债ETF", 0), ("518880.SH", "黄金ETF", 0),
]

# 申万一级行业（演示用，代码/名称为真实格式）。分类是 PIT 协变量栈的必需输入：
# 没有它，报告的归因分解、行业中性化、风格体检都会**安静地**整块消失 ——
# 而这是代码问题还是数据问题，看报告是分不出来的（见 generate_demo 注释）。
DEMO_INDUSTRIES = [
    ("801780.SI", "银行"), ("801080.SI", "电子"), ("801120.SI", "食品饮料"),
    ("801180.SI", "房地产"), ("801730.SI", "电力设备"), ("801150.SI", "医药生物"),
]


def _calendar(start: str, end: str) -> pl.DataFrame:
    import pandas as pd

    bdays = pd.bdate_range(start, end)   # 工作日近似交易日（节假日不影响链路验证）
    return pl.DataFrame({
        # pandas DatetimeIndex.date 是 object 数组，必须显式 cast 成 Date
        "trade_date": pl.Series(list(bdays.date), dtype=pl.Date),
        "is_open": True,
        "exchange": "SSE",
    })


def _synth_bars(symbol: str, dates: list, seed: int, base_price: float,
                sec_type: str) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(dates)
    ret = rng.normal(0.0003, 0.25 / np.sqrt(244), n)   # 年化波动 25%
    close = base_price * np.exp(np.cumsum(ret))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    pre_close = np.concatenate([[base_price], close[:-1]])
    volume = rng.lognormal(15, 0.6, n)
    # float_mv（流通市值，元）与真实 provider 同一恒等式推导：
    # 换手率(%) = 成交量 / 流通股本 × 100 → 流通市值 = close × volume / (turn / 100)。
    # 必须**在 round 之前**由未取整的 close/volume/turn 算出，否则三处各自的
    # 取整误差会叠进市值；份额下限 0.2% 保证除数恒正、不产 inf。
    turnover = rng.uniform(0.2, 5, n)
    float_mv = close * volume / (turnover / 100)
    return pl.DataFrame({
        "trade_date": dates,
        "symbol": [symbol] * n,
        "open": open_.round(2), "high": high.round(2),
        "low": low.round(2), "close": close.round(2),
        "pre_close": pre_close.round(2),
        "volume": volume.round(0),
        "amount": (volume * close).round(0),
        "turnover_rate": turnover.round(2),
        "float_mv": float_mv.round(0),
        "adj_factor": np.ones(n),
        "sec_type": [sec_type] * n,
    })


def _refuse_if_lake_has_real_data() -> None:
    """湖里已有真实行 → 在写任何东西之前拒绝生成演示数据。

    演示数据的落点是 CWD 相对的 ./data/parquet，而隔离 worktree 的标准姿势正是
    把 data/parquet 软链到主仓真实湖 —— 一次误跑就会把 20 只真代码股票 + 10 只
    真代码 ETF 的真实行覆盖成合成值（2026-09-18 事故，实测 21,300 行 /
    2024-01-01~2026-09-18，schema 与质量断言全都看不出异常）。

    这里先做一次粗筛（湖里有没有非 demo 行）快速失败，避免连日历/标的/ETF 元
    数据一起被演示值覆盖；逐键的精确护栏在 write_daily（写入层兜底）。
    """
    from lquant.core.errors import DataQualityError
    from lquant.data.store.parquet import read_daily

    try:
        lake = read_daily().select(["symbol", "trade_date", "source"]).collect()
    except Exception:  # noqa: BLE001 - 读不动就交给写入层兜底，不在读路径上炸
        return
    if not len(lake) or "source" not in lake.columns:
        return
    real = lake.filter(pl.col("source") != "demo")
    if not len(real):
        return
    from lquant.core.config import get_settings

    raise DataQualityError(
        "DEMO_OVERWRITE_REAL",
        f"目标湖已有 {len(real):,} 行真实日线（{real['symbol'].n_unique()} 只标的），"
        f"拒绝写入演示数据。演示数据只用于全新/空湖；"
        f"若要重置请先归档或删除 ({get_settings().parquet_dir})/daily。"
    )


def generate_demo(start: str = "2024-01-01", end: str | None = None) -> dict:
    """生成合成地基 + 日线。幂等：同 key 直接覆盖。

    拒绝在已有真实数据的湖上运行（见 _refuse_if_lake_has_real_data）——
    合成数据覆盖真实观测在任何场景下都不是想要的结果。
    """
    from datetime import date

    from loguru import logger

    _refuse_if_lake_has_real_data()

    end = end or today_cn().isoformat()
    dates = _calendar(start, end)["trade_date"].to_list()
    logger.info(f"生成演示数据 {len(dates)} 个交易日 {start}~{end}")

    n_cal = TradeCalendarRepo().upsert(_calendar(start, end))

    secs = pl.DataFrame({
        "symbol": [c for c, _ in STOCK_CODES] + [c for c, _, _ in ETF_CODES],
        "name": [n for _, n in STOCK_CODES] + [n for _, n, _ in ETF_CODES],
        "sec_type": ["stock"] * len(STOCK_CODES) + ["etf"] * len(ETF_CODES),
        "board": ["main"] * len(STOCK_CODES) + ["unknown"] * len(ETF_CODES),
        "list_date": date(2010, 1, 1),
        "delist_date": None,
        "is_st": False,
        "source": "demo",
        "updated_at": now_cn().replace(tzinfo=None),
    })
    n_sec = SecurityRepo().upsert(secs)

    etfs = pl.DataFrame({
        "symbol": [c for c, _, _ in ETF_CODES],
        "name": [n for _, n, _ in ETF_CODES],
        "track_index": [n for _, n, _ in ETF_CODES],
        "sellable_after_days": [t for _, _, t in ETF_CODES],
        "as_of": today_cn(),
        "source": "demo",
    })
    EtfMetaRepo().upsert(etfs)

    # 行业分类：演示环境必须**自洽**。缺失的后果不是报错，而是报告的归因分解 /
    # 行业中性化 / 风格体检静默消失（协变量 coverage=0 → cat_col=None → 那几段
    # 直接不渲染），从报告上看不出是「没数据」还是「代码坏了」——
    # tests/unit/test_report_extras_parity.py 正是这么红的。
    # std_date 取 2010-01-01（早于演示区间起点）：PIT as-of join 需要「当日已生效」。
    industries = pl.DataFrame({
        "symbol": [c for c, _ in STOCK_CODES],
        "std": ["SW"] * len(STOCK_CODES),
        "code": [DEMO_INDUSTRIES[i % len(DEMO_INDUSTRIES)][0]
                 for i in range(len(STOCK_CODES))],
        "name": [DEMO_INDUSTRIES[i % len(DEMO_INDUSTRIES)][1]
                 for i in range(len(STOCK_CODES))],
        "std_date": [date(2010, 1, 1)] * len(STOCK_CODES),
        "source": ["demo"] * len(STOCK_CODES),
    })
    n_ind = IndustryClassifyRepo().upsert(industries)

    frames = []
    for i, (sym, _) in enumerate(STOCK_CODES):
        frames.append(_synth_bars(sym, dates, seed=100 + i,
                                  base_price=5.0 + (i * 13.7) % 250, sec_type="stock"))
    for i, (sym, _, _) in enumerate(ETF_CODES):
        frames.append(_synth_bars(sym, dates, seed=500 + i,
                                  base_price=1.0 + (i * 0.83) % 6, sec_type="etf"))
    daily = pl.concat(frames).with_columns(
        source=pl.lit("demo"),
        quality_flags=pl.lit(0, dtype=pl.Int32),
        ingested_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit("demo"),
    )
    write_daily(daily)

    # 估值列（daily_basic）与 PIT 财务（financial_pit）
    #
    # 为什么演示环境必须有这两张表：个股分析的「估值」角度、行业分析的
    # 「估值 / 景气度」角度、以及基本面排名页，全都以它们为输入。缺失的表现
    # **不是报错**，而是那几个角度安静地变成 ``available=false`` —— 从页面上
    # 分不出「演示环境本来就没有这个数据」还是「代码写坏了」。这与 float_mv /
    # industry_classify 当初被补进演示数据是同一个理由。
    basic = _synth_daily_basic(daily)
    write_daily_basic(basic)
    fin_rows = _synth_financial(start, dates[-1] if dates else None)
    n_fin = FinancialRepo().upsert(fin_rows) if len(fin_rows) else 0

    # 指数日线（基准）。个股的「行业与相对强度」、行业的「相对基准超额 / RRG
    # 相对旋转图」都以它为分母；缺了会让这些角度整体不可用。演示环境用同一段
    # 交易日合成，保证 asof 当天确实查得到基准。
    n_idx = _seed_index_daily(start, (dates[-1] if dates else today_cn()).isoformat())

    logger.info(
        f"演示数据就绪: 日历 {n_cal} 天 / 标的 {n_sec} 只 / 行业 {n_ind} 条 / "
        f"日线 {len(daily)} 行 / 估值 {len(basic)} 行 / 财务 {n_fin} 行 / "
        f"指数 {n_idx} 行"
    )
    return {"calendar": n_cal, "securities": n_sec, "industries": n_ind,
            "daily_rows": len(daily), "daily_basic_rows": len(basic),
            "financial_rows": n_fin, "index_rows": n_idx}


def _seed_index_daily(start: str, end: str) -> int:
    """把演示指数日线写进 ``index_daily``（失败不阻断演示环境搭建）。"""
    try:
        from lquant.market.collectors.index_daily import fetch_index_daily
        from lquant.market.scheduler import persist

        df = fetch_index_daily(start=start, end=end, demo=True)
        counts = persist({"index_daily": df})
        return int(counts.get("index_daily", 0))
    except Exception as e:  # noqa: BLE001 - 指数只是基准，缺了不该挡住演示环境
        from loguru import logger

        logger.warning(f"演示指数日线写入失败（基准相关角度将不可用）: {e}")
        return 0


#: 每股收益 / 每股净资产 的演示生成参数。用固定基准价换算，保证 PE / PB 落在
#: A 股常见区间（PE 8~45、PB 0.8~8），而不是随机数 —— 随机 PE 会让「行业估值
#: 分位」在演示环境里毫无参考性。
_PE_BASE_RANGE = (8.0, 45.0)
_PB_BASE_RANGE = (0.8, 8.0)
_FREE_FLOAT_RANGE = (0.3, 0.8)


def _synth_daily_basic(daily: pl.DataFrame) -> pl.DataFrame:
    """由日线派生演示估值列（``daily_basic``）。

    恒等式与真实 provider 一致：``float_share = float_mv / close``、
    ``total_share = float_share / 自由流通比例``、``total_mv = close × total_share``。
    PE/PB 由「每股收益 / 每股净资产」反推，估值只会因为价格波动而变化 ——
    这正是「估值分位」要度量的东西。
    """
    if daily is None or daily.is_empty() or "float_mv" not in daily.columns:
        return pl.DataFrame()
    syms = sorted(set(daily["symbol"].to_list()))
    eps, bps, ff = {}, {}, {}
    for i, s in enumerate(syms):
        rng = np.random.default_rng(9000 + i)
        pe = float(rng.uniform(*_PE_BASE_RANGE))
        pb = float(rng.uniform(*_PB_BASE_RANGE))
        base = float(daily.filter(pl.col("symbol") == s)["close"][0] or 10.0)
        eps[s], bps[s] = base / pe, base / pb
        ff[s] = float(rng.uniform(*_FREE_FLOAT_RANGE))

    out = (daily.select(["trade_date", "symbol", "close", "float_mv",
                         "turnover_rate"])
           .with_columns(
               pl.col("symbol").replace_strict(eps, default=None).alias("_eps"),
               pl.col("symbol").replace_strict(bps, default=None).alias("_bps"),
               pl.col("symbol").replace_strict(ff, default=None).alias("_ff"),
           )
           .with_columns(
               (pl.col("close") / pl.col("_eps")).round(2).alias("pe_ttm"),
               (pl.col("close") / pl.col("_bps")).round(3).alias("pb_mrq"),
               (pl.col("close") / pl.col("_eps") * 0.6).round(2).alias("ps_ttm"),
               (pl.col("close") / pl.col("_bps") * 0.5).round(2).alias("dv_ttm"),
               (pl.col("float_mv") / pl.col("close")).round(0).alias("float_share"),
           )
           .with_columns(
               (pl.col("float_share") / pl.col("_ff")).round(0).alias("total_share"),
           )
           .with_columns(
               (pl.col("close") * pl.col("total_share")).round(0).alias("total_mv"),
               pl.lit("demo").alias("source"),
           ))
    return out.select(["symbol", "trade_date", "close", "turnover_rate",
                       "pe_ttm", "pb_mrq", "ps_ttm", "total_mv", "float_mv",
                       "dv_ttm", "total_share", "float_share", "source"])


#: 演示财务指标（物理键 → (基准值, 波动, 每期趋势)）。键名与真实 provider
#: 的命名一致，否则演示环境跑通、真实环境静默取不到数。
_FIN_ITEMS: tuple[tuple[str, float, float, float], ...] = (
    ("indicator.roe", 12.0, 4.0, 0.15),
    ("indicator.grossprofit_margin", 32.0, 8.0, 0.10),
    ("indicator.netprofit_margin", 12.0, 5.0, 0.10),
    ("indicator.debt_to_assets", 45.0, 12.0, -0.20),
    ("indicator.or_yoy", 8.0, 12.0, 0.60),
    ("indicator.netprofit_yoy", 10.0, 18.0, 0.80),
)


def _quarter_ends(start: str, end: date | None) -> list[date]:
    """演示区间内（含前推一年预热）的季度报告期。

    末尾**多带一个季度**：区间常常止于季度中（如 3 月 29 日），严格按
    ``q <= end`` 过滤会让短区间一条财务都生不出来 —— 演示环境随即「静默」
    少了景气度角度。多出的那个报告期公告日晚于区间末尾，会被 PIT 门禁
    正确挡在 ``asof`` 之外，正好顺带验证门禁有效。
    """
    d0 = date.fromisoformat(start)
    d1 = end or today_cn()
    limit = d1 + timedelta(days=100)
    out: list[date] = []
    for y in range(d0.year - 1, d1.year + 2):
        for m, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, day)
            if d0 <= q <= limit:
                out.append(q)
    return out


def _synth_financial(start: str, end: date | None) -> pl.DataFrame:
    """演示 PIT 财务长表。

    ``pub_date = stat_date + 45 天`` 是**演示用的近似**：真实披露日随报告期
    不同（年报可晚至次年 4 月底）。这里保证的是 PIT 语义正确（公告日之前查
    不到），而不是披露日历精确 —— 精确日历由真实数据源提供。
    """
    quarters = _quarter_ends(start, end)
    if not quarters:
        return pl.DataFrame()
    rows = []
    for i, (sym, _) in enumerate(STOCK_CODES):
        rng = np.random.default_rng(7000 + i)
        for j, q in enumerate(quarters):
            for item, base, vol, trend in _FIN_ITEMS:
                value = base + vol * math.sin(i + j / 2.0) + trend * j
                value += float(rng.normal(0, vol * 0.25))
                rows.append({
                    "symbol": sym,
                    "stat_date": q,
                    "pub_date": q + timedelta(days=45),
                    "report_type": "Q",
                    "item": item,
                    "value": round(value, 4),
                    "unit": "%",
                    "source": "demo",
                })
    return pl.DataFrame(rows)
