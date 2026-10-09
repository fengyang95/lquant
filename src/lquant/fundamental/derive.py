"""派生指标：``financial_pit`` 里没有直接字段的基本面比率，由原始科目现算。

为什么需要这一层：Tushare ``fina_indicator`` 只提供了一部分比率
（``roe`` / ``current_ratio`` / ``assets_turn`` …），而「经营现金流/净利润」
「利息保障倍数」「存货周转天数」这些**跨表**比率在库里没有现成列。
与其把它们直接从评分目录里删掉（会让「现金质量」模块残缺、覆盖率天花板下降），
不如用原始三表现算 —— 但必须把口径写死在代码里并加测试，而不是留给调用方猜。

**PIT 正确性（本模块的核心约束）**：

1. 派生按 ``(symbol, stat_date)`` 分组 —— 分子分母必须是**同一报告期**。
   若按指标各自取「最新报告期」再相除，会拿 Q3 的现金流去除 Q2 的净利润，
   算出没有意义的数（这是最容易被忽略的一类前视/串期偏差）。
2. 每个原始科目在组内取 ``pub_date`` 最新的一次修订（更正后的报表为准）。
3. 派生的 ``pub_date = max(所用分子的 pub_date, 分母的 pub_date)`` ——
   只有全部输入都已公告，这个比率才是可知的。任一输入缺失即整条缺失，
   **绝不用 0 或行业均值兜底**（缺数据不能被当成「经营现金流为 0」）。
4. 分母为 0 / 结果非有限值时置空，而不是产出 ``inf`` —— 后者会污染
   行业分位（P25/P50/P75 被一个 inf 拉爆）。

口径与近似（每条都必须在文档里说清，避免「看起来像官方指标」）：

===========================  ==========================================  ==================
逻辑键                        公式                                        说明
===========================  ==========================================  ==================
``derived.cfo_to_np``        ``cashflow.n_cashflow_act / income.n_income_attr_p``  经营现金流/归母净利润
``derived.cfo_to_or``        ``cashflow.n_cashflow_act / income.total_revenue``    经营现金流/营业收入
``derived.cfo_to_op``        ``cashflow.n_cashflow_act / income.operate_profit``   经营现金流/营业利润
``derived.ar_turn_days``     ``360 / indicator.ar_turn``                 用 Tushare 官方周转率折算
``derived.inv_turn_days``    ``360 × balancesheet.inventories / income.oper_cost``  **近似**：用期末存货而非平均存货
``derived.ebit_to_interest`` ``indicator.ebit / income.fin_exp_int_exp``  利息保障倍数
===========================  ==========================================  ==================
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

__all__ = [
    "DERIVED_METRICS",
    "DerivedSpec",
    "derive_pit",
    "derived_labels",
    "raw_items",
]

_PANEL_COLS = ("symbol", "stat_date", "pub_date", "item", "value")


@dataclass(frozen=True)
class DerivedSpec:
    """``value = scale × numerator / denominator``。

    Attributes:
        item: 派生指标的稳定逻辑键（``derived.`` 前缀）。
        label: 中文名。
        numerator: ``financial_pit`` 物理键；``float`` 表示常数（如周转天数里的 360）。
        denominator: ``financial_pit`` 物理键。
        scale: 整体缩放系数。
        denominator_positive: 要求分母严格为正。用于「周转天数 / 利息保障倍数」
            这类分母为负就没有经济含义的比率；而 ``cfo_to_np`` 的净利润
            为负是**有意义**的（亏损公司），所以不设此约束。
    """

    item: str
    label: str
    numerator: str | float
    denominator: str
    scale: float = 1.0
    denominator_positive: bool = False
    # True：分母是「报告期累计」口径（利润表科目/周转率），按报告期年化后再除。
    # Q1 的营业成本只覆盖 3 个月，不年化会把周转天数高估 4 倍
    annualize_denominator: bool = False

    @property
    def inputs(self) -> tuple[str, ...]:
        keys = [self.denominator]
        if isinstance(self.numerator, str):
            keys.append(self.numerator)
        return tuple(dict.fromkeys(keys))


#: 逻辑键 → 派生口径。顺序即目录展示顺序。
DERIVED_METRICS: dict[str, DerivedSpec] = {
    s.item: s
    for s in (
        DerivedSpec("derived.cfo_to_np", "经营现金流/净利润",
                    "cashflow.n_cashflow_act", "income.n_income_attr_p"),
        DerivedSpec("derived.cfo_to_or", "经营现金流/营业收入",
                    "cashflow.n_cashflow_act", "income.total_revenue"),
        DerivedSpec("derived.cfo_to_op", "经营现金流/营业利润",
                    "cashflow.n_cashflow_act", "income.operate_profit"),
        DerivedSpec("derived.ar_turn_days", "应收周转天数",
                    360.0, "indicator.ar_turn", denominator_positive=True,
                    annualize_denominator=True),
        DerivedSpec("derived.inv_turn_days", "存货周转天数",
                    "balancesheet.inventories", "income.oper_cost",
                    scale=360.0, denominator_positive=True,
                    annualize_denominator=True),
        DerivedSpec("derived.ebit_to_interest", "利息保障倍数",
                    "indicator.ebit", "income.fin_exp_int_exp",
                    denominator_positive=True),
    )
}


def raw_items() -> tuple[str, ...]:
    """全部派生指标需要的 ``financial_pit`` 物理键（去重，用于取数）。"""
    keys: list[str] = []
    for spec in DERIVED_METRICS.values():
        keys.extend(spec.inputs)
    return tuple(dict.fromkeys(keys))


def derived_labels() -> dict[str, str]:
    return {s.item: s.label for s in DERIVED_METRICS.values()}


def _latest_slice(panel: pl.DataFrame, key: str) -> pl.DataFrame:
    """``(symbol, stat_date)`` → 该科目 ``pub_date`` 最新的一次修订。"""
    return (
        panel.filter(pl.col("item") == key)
        .sort(["symbol", "stat_date", "pub_date"])
        .group_by(["symbol", "stat_date"])
        .agg(pl.col("value").last().alias("value"),
             pl.col("pub_date").last().alias("pub_date"))
    )


def _annualize_factor_expr() -> pl.Expr:
    """报告期累计 → 年度口径的系数：Q1×4 / H1×2 / Q1-3×(4/3) / FY×1。

    按报告期月份推断覆盖天数占比（Q1=3/12 年、H1=6/12、Q1-3=9/12）。
    """
    m = pl.col("stat_date").dt.month()
    return (pl.when(m == 3).then(4.0)
            .when(m == 6).then(2.0)
            .when(m == 9).then(4.0 / 3.0)
            .otherwise(1.0))


def _derive_one(panel: pl.DataFrame, spec: DerivedSpec) -> pl.DataFrame:
    """单个派生指标 → 长表行（``item`` 为逻辑键）。"""
    den = _latest_slice(panel, spec.denominator).rename(
        {"value": "den", "pub_date": "den_pub"})
    if not den.height:
        return panel.head(0).select(_PANEL_COLS)

    if isinstance(spec.numerator, str):
        num = _latest_slice(panel, spec.numerator).rename(
            {"value": "num", "pub_date": "num_pub"})
        if not num.height:
            return panel.head(0).select(_PANEL_COLS)
        joined = den.join(num, on=["symbol", "stat_date"], how="inner")
    else:
        joined = den.with_columns(
            pl.lit(float(spec.numerator)).alias("num"),
            pl.col("den_pub").alias("num_pub"),
        )

    # 任一输入缺失 / 分母为 0（或要求正但非正）→ 整条置空，不兜底
    if spec.annualize_denominator:
        # 分母年化到年度口径再做比率（tushare 财务口径是报告期累计）
        joined = joined.with_columns(
            (pl.col("den") * _annualize_factor_expr()).alias("den"))
    ok = (pl.col("den") > 0) if spec.denominator_positive else (pl.col("den") != 0)
    ok = ok & pl.col("den").is_not_null() & pl.col("num").is_not_null()

    value = (pl.col("num") * float(spec.scale) / pl.col("den"))
    value = pl.when(ok & value.is_finite() & value.is_not_null()).then(value).otherwise(None)

    return (
        joined.with_columns(
            value.alias("value"),
            # 派生值只有在**全部**输入都已公告时才可知
            pl.max_horizontal("den_pub", "num_pub").alias("pub_date"),
            pl.lit(spec.item).alias("item"),
        )
        .select(_PANEL_COLS)
        .filter(pl.col("value").is_not_null())
    )


def derive_pit(panel: pl.DataFrame) -> pl.DataFrame:
    """把原始长表扩展出全部派生指标行。

    Args:
        panel: ``financial_pit`` 长表，含 ``symbol / stat_date / pub_date /
            item / value``；只需包含 :func:`raw_items` 里的键。

    Returns:
        仅含派生指标的长表（同 schema）；输入为空或所需科目全缺时返回空表。
    """
    empty = pl.DataFrame(schema={
        "symbol": pl.String, "stat_date": pl.Date, "pub_date": pl.Date,
        "item": pl.String, "value": pl.Float64,
    })
    if panel.is_empty():
        return empty
    missing = set(_PANEL_COLS) - set(panel.columns)
    if missing:
        raise KeyError(f"financial_pit 面板缺列: {sorted(missing)}")

    frames = [_derive_one(panel, spec) for spec in DERIVED_METRICS.values()]
    frames = [f for f in frames if f.height]
    if not frames:
        return empty
    return pl.concat(frames, how="vertical_relaxed")
