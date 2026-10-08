"""看板另类数据 → 因子层（EP-9 落地）：资金流 / 龙虎榜 / 涨停池。

这三张表在 ``market/collectors`` 里采集已久（``money_flow`` / ``dragon_tiger``
/ ``limit_up_pool``），因子层却零消费 —— 数据在库里睡觉。本模块把它们做成
``CovariateProvider``（``build_covariates`` 自动 join + 覆盖率上报），使
G0-G3 门禁能评价「情绪/资金面因子」。

防未来函数（本模块的生命线，所有特征统一执行）：

- 龙虎榜由交易所 T 日收盘后公布、晚间定型 → T+1 才可用；
- 资金流 / 涨停池当日盘中值会漂、日终才定型，且采集任务可能在盘中跑 →
  同样只承诺 T+1 可用；
- 实现：特征先按 T 日口径算好，再组内 ``shift(1)``（T 日值出现在 T+1 行），
  于是 T+1 行的因子值只能看到 ≤T 日的看板数据。
- 宁可晚一天，不可用未来 —— 日线因子本来就是 T 收盘算、T+1 调仓，晚一天
  只损失一点时效，方向绝不错。

缺失语义与 covariates 三条硬约束一致：看板表没同步 → ``CovariateUnavailable``
（coverage=0 显式上报，绝不填 0 冒充）；未上榜 / 当日无流数据按业务语义取 0
（「没有资金异动」是事实，不是缺失），shift 产生的窗口首行保留 null。
"""

from __future__ import annotations

import polars as pl

from lquant.factors.covariates import CovariateUnavailable, provider

__all__ = ["board_cov_names", "BOARD_COVARIATES"]


def _norm_sym(code) -> str:
    """看板表与日线面板的 symbol 口径对齐（parse_symbol 标准格式）。

    采集端落库已 _norm 过，这里再对齐一次是防御：格式漂移的症状是
    join 全空 + coverage=0，比静默错位好查得多。
    """
    from lquant.core.types import parse_symbol

    try:
        return str(parse_symbol(str(code)))
    except ValueError:
        return str(code)


def _board_table(
    table: str, panel: pl.DataFrame, need: tuple[str, ...] = ()
) -> pl.DataFrame:
    """读看板表（覆盖 panel 的日期范围）。表未建/缺列/为空 → CovariateUnavailable。

    ``need`` 是这张表上 provider 真正消费的字段。缺列必须在这里统一收口成
    ``CovariateUnavailable``：``build_covariates`` 只认这一个异常，而 provider 里
    裸的 ``pl.col(...)`` 会抛 polars ``ColumnNotFoundError`` 穿透整个协变量构建
    （CLI 裸 traceback / API 500），而不是 README 承诺的 coverage=0 显式上报。
    老库 / 只跑过部分采集的库缺字段是真实运维场景；这里显式报缺列，也绝不靠
    「让 build_covariates 吞掉所有 Exception」来掩盖真错误。

    先查列再查空：空结果一样带 schema，缺列时先报缺列（更可操作）。
    """
    from duckdb import CatalogException

    from lquant.core.db import reader

    lo, hi = panel["trade_date"].min(), panel["trade_date"].max()
    try:
        with reader() as con:
            rows = con.execute(
                f"SELECT * FROM {table} WHERE trade_date BETWEEN ? AND ?",
                [lo, hi],
            ).pl()
    except CatalogException as e:
        raise CovariateUnavailable(f"{table} 未建表（看板采集从未跑过）") from e
    missing = [c for c in need if c not in rows.columns]
    if missing:
        raise CovariateUnavailable(
            f"{table} 缺列 {missing}（老库或只跑过部分采集）——该 covariate 不可计算；"
            "重跑对应看板采集补齐字段")
    if not len(rows):
        raise CovariateUnavailable(f"{table} 在评价窗口内无数据（先在数据页采集）")
    return (
        rows.with_columns(pl.col("symbol").map_elements(_norm_sym, return_dtype=pl.Utf8))
        # 归一后按主键收敛：表的主键是原始字符串，脏格式（sz.000001）与标准
        # 格式（000001.SZ）可能同票同日共存两行 —— 不去重的话 left join 会
        # 膨胀面板，IC 分母悄悄变大。keep="last" 取最新一条（采集幂等语义）。
        .unique(subset=["trade_date", "symbol"], keep="last")
    )


def _grid(panel: pl.DataFrame) -> pl.DataFrame:
    """(symbol, trade_date) 去重网格 + 组内日期升序（shift/rolling 的前提）。"""
    return panel.select("symbol", "trade_date").unique().sort(["symbol", "trade_date"])


def _shift1(df: pl.DataFrame, value_col: str) -> pl.DataFrame:
    """T 日特征 → T+1 行（防前视的唯一出口，所有 provider 必经）。"""
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col(value_col).shift(1).over("symbol").alias(value_col))
        .select(["trade_date", "symbol", value_col])
    )


def board_cov_names() -> list[str]:
    """本模块注册的全部看板 covariate 名（挖掘/评价侧枚举用）。"""
    return list(BOARD_COVARIATES)


BOARD_COVARIATES: tuple[str, ...] = (
    "mf_main_ratio",  # T 日主力净流入占比（money_flow.main_net_ratio）
    "mf_main_ratio_3d",  # 近 3 日主力净占比均值
    "lhb_on_board",  # T 日是否上龙虎榜（0/1）
    "lhb_net_buy_5d",  # 近 5 日上榜净买合计（未上榜日计 0）
    "zt_streak",  # T 日连续涨停天数
    "zt_open_count_20d",  # 近 20 日炸板次数合计（open_count，未涨停日计 0）
)


@provider("mf_main_ratio", label="T日主力净流入占比（T+1 可用）")
def _p_mf_main_ratio(panel, industry_df=None):
    raw = _board_table("money_flow", panel, need=("main_net_ratio",))
    d = _grid(panel).join(
        raw.select("trade_date", "symbol", "main_net_ratio"),
        on=["trade_date", "symbol"],
        how="left",
    )
    # 未上榜 / 当日无流数据按模块 docstring 与 README 的业务语义取 0（「没有资金
    # 异动」是事实，不是缺失）；shift 的窗口首行仍留 null（防前视的代价，已声明）。
    # 这里绝不能留 null：``money_flow`` 采集按主力净流入降序只取前 200 只，留 null
    # 会让 OLS 中性化的 valid_mask 把这些行整行剔除，评价样本被「协变量本身」截断
    # 成当日前 200 只，而 submit/CLI 的 JSON 里没有任何样本量字段能看到这次截断。
    d = d.with_columns(pl.col("main_net_ratio").fill_null(0.0))
    return _shift1(d, "main_net_ratio")


@provider("mf_main_ratio_3d", label="近3日主力净占比均值（T+1 可用）")
def _p_mf_main_ratio_3d(panel, industry_df=None):
    raw = _board_table("money_flow", panel, need=("main_net_ratio",))
    d = _grid(panel).join(
        raw.select("trade_date", "symbol", "main_net_ratio"),
        on=["trade_date", "symbol"],
        how="left",
    )
    # 与 mf_main_ratio 同口径：无流日先填 0 再滚动，否则 rolling 跳过 null 观测
    # 会让「近 3 日均值」在缺流日变成非 0 的隔日值，且同样触发中性化截断。
    d = d.with_columns(pl.col("main_net_ratio").fill_null(0.0))
    d = d.with_columns(
        pl.col("main_net_ratio").rolling_mean(3, min_samples=1).over("symbol").alias("_v")
    )
    return _shift1(d, "_v")


@provider("lhb_on_board", label="T日是否上龙虎榜 0/1（T+1 可用）")
def _p_lhb_on_board(panel, industry_df=None):
    raw = _board_table("dragon_tiger", panel).with_columns(pl.lit(1).alias("_hit"))
    d = _grid(panel).join(
        raw.select("trade_date", "symbol", "_hit"), on=["trade_date", "symbol"], how="left"
    )
    d = d.with_columns(pl.col("_hit").fill_null(0).cast(pl.Int8).alias("_v"))
    return _shift1(d, "_v")


@provider("lhb_net_buy_5d", label="近5日龙虎榜净买合计，未上榜日计0（T+1 可用）")
def _p_lhb_net_buy_5d(panel, industry_df=None):
    raw = _board_table("dragon_tiger", panel, need=("net_buy",))
    d = _grid(panel).join(
        raw.select("trade_date", "symbol", "net_buy"), on=["trade_date", "symbol"], how="left"
    )
    d = d.with_columns(
        # 龙虎榜同一票同日可能多上榜原因（多行）→ 先按日合计再滚动
        pl.col("net_buy").sum().over(["symbol", "trade_date"]).fill_null(0.0).alias("_nb")
    )
    d = d.with_columns(pl.col("_nb").rolling_sum(5, min_samples=1).over("symbol").alias("_v"))
    return _shift1(d, "_v")


def _limit_up_only(panel: pl.DataFrame) -> pl.DataFrame:
    """只要**真涨停**的行：``limit_up_pool`` 同时装「涨停池」与「炸板池」两份采集。

    ``market/collectors/__init__.py`` 把 ``broken_pool`` 也落到 ``limit_up_pool``
    （同一个交易日、同一张表），而炸板池的行没有 ``limit_up_type``（``fetch_broken_pool``
    不产出该列 → 入库为 NULL，``fetch_limit_up_pool`` 才有「一字板/T字板/换手板」）。
    按「在池里」计数会把「盘中涨停但收盘没封住」算成涨停日，连板数因此虚高。

    炸板行只能在写库时区分，所以这里按 ``limit_up_type`` 过滤；列整个缺失
    （老库只跑过炸板池采集）由 ``_board_table(need=...)`` 统一报
    CovariateUnavailable，绝不假装知道 —— 与模块「缺失语义显式上报，绝不填 0
    冒充」的约定一致。
    """
    raw = _board_table("limit_up_pool", panel, need=("limit_up_type",))
    real = raw.filter(pl.col("limit_up_type").is_not_null())
    if not len(real):
        raise CovariateUnavailable(
            "limit_up_pool 窗口内只有炸板池行（无 limit_up_type）——涨停池未采集")
    return real


@provider("zt_streak", label="T日连续涨停天数（T+1 可用）")
def _p_zt_streak(panel, industry_df=None):
    raw = _limit_up_only(panel).with_columns(pl.lit(1).alias("_in_pool"))
    d = _grid(panel).join(
        raw.select("trade_date", "symbol", "_in_pool"), on=["trade_date", "symbol"], how="left"
    )
    d = d.with_columns(pl.col("_in_pool").fill_null(0).cast(pl.Int8))
    d = d.with_columns(pl.col("_in_pool").rle_id().over("symbol").alias("_rid"))
    d = d.with_columns(
        pl.when(pl.col("_in_pool") == 1)
        .then(pl.col("_in_pool").cum_sum().over(["symbol", "_rid"]))
        .otherwise(0)
        .alias("_v")
    )
    return _shift1(d, "_v")


@provider("zt_open_count_20d", label="近20日炸板次数合计，未涨停日计0（T+1 可用）")
def _p_zt_open_count_20d(panel, industry_df=None):
    raw = _board_table("limit_up_pool", panel, need=("open_count",))
    raw = raw.group_by(["trade_date", "symbol"]).agg(
        pl.col("open_count").cast(pl.Int64).sum().alias("_oc")
    )
    d = _grid(panel).join(raw, on=["trade_date", "symbol"], how="left")
    d = d.with_columns(pl.col("_oc").fill_null(0).alias("_v"))
    d = d.with_columns(pl.col("_v").rolling_sum(20, min_samples=1).over("symbol").alias("_v"))
    return _shift1(d, "_v")
