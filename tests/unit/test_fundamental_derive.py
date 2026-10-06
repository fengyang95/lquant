"""派生指标的口径与 PIT 契约：同报告期、公告日取 max、缺项不兜底。

这一层是整个基本面模块里最容易悄悄出错的地方 —— 分母取自另一个报告期
（Q3 现金流 ÷ Q2 净利润）算出来的数「看起来正常」，但没有任何经济含义，
而且不会抛异常。所以每条口径都单独固定。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.fundamental.derive import (
    DERIVED_METRICS,
    DerivedSpec,
    derive_pit,
    derived_labels,
    raw_items,
)

Q1 = date(2025, 3, 31)
Q2 = date(2025, 6, 30)
PUB_Q1 = date(2025, 4, 25)
PUB_Q2 = date(2025, 8, 20)
REV_Q1 = date(2025, 6, 10)          # Q1 的更正公告


def _panel(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema=["symbol", "stat_date", "pub_date", "item", "value"],
        orient="row")


def _value(out: pl.DataFrame, item: str, symbol: str = "600000.SH"):
    """取该指标在 ``resolve_pit`` 口径下会用的那一条（最新报告期、最新公告）。

    ``derive_pit`` 按 (symbol, stat_date) 逐期产出，**不是**每票一行 ——
    每期都要保留，才能让下游 ``resolve_pit`` 自己挑最新的一期。
    """
    hit = out.filter((pl.col("item") == item) & (pl.col("symbol") == symbol))
    if hit.is_empty():
        return None
    return hit.sort(["stat_date", "pub_date"]).row(-1, named=True)


# ---------- 注册表本身 ----------

def test_raw_items_is_union_of_all_inputs():
    expected: list[str] = []
    for spec in DERIVED_METRICS.values():
        expected.extend(spec.inputs)
    assert set(raw_items()) == set(expected)


def test_spec_inputs_include_constant_numerator_case():
    """常数分子（周转天数里的 360）不该被当成物理键塞进取数清单。"""
    spec = DERIVED_METRICS["derived.ar_turn_days"]
    assert spec.inputs == ("indicator.ar_turn",)
    assert "360" not in raw_items()
    assert not any(k.replace(".", "").isdigit() for k in raw_items())


def test_spec_rejects_unknown_field_names():
    """构造期就挡住写错的字段名，而不是等运行到一半 AttributeError。"""
    with pytest.raises(TypeError):
        DerivedSpec("x", "x", "a.b", "c.d", nonexistent=True)  # type: ignore[call-arg]


# ---------- 同报告期 ----------

def test_derives_from_same_report_period_not_latest_per_item():
    """分子分母必须来自同一报告期。

    构造：Q1 两个科目都有值，Q2 只有分母（净利润）有值。
    若实现按「每个科目各自取最新」再相除，会得到 Q1现金流 ÷ Q2净利润；
    正确答案应当是 Q1 的比（Q2 算不出来）。
    """
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 50.0),
        ("600000.SH", Q2, PUB_Q2, "income.n_income_attr_p", 25.0),
    ]))
    row = _value(out, "derived.cfo_to_np")
    assert row is not None
    assert row["value"] == pytest.approx(2.0)          # 100 / 50，不是 100 / 25
    assert row["stat_date"] == Q1


def test_all_periods_are_emitted_so_resolve_pit_can_pick():
    """每期都产出：只留最新一期会让「最新期缺某个输入」直接丢票，
    而正确答案是回退到上一个输入齐全的报告期。"""
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 50.0),
        ("600000.SH", Q2, PUB_Q2, "cashflow.n_cashflow_act", 90.0),
        ("600000.SH", Q2, PUB_Q2, "income.n_income_attr_p", 30.0),
    ]))
    got = out.filter(pl.col("item") == "derived.cfo_to_np")
    assert set(got["stat_date"].to_list()) == {Q1, Q2}
    assert _value(out, "derived.cfo_to_np")["stat_date"] == Q2


# ---------- PIT：公告日 ----------

def test_pub_date_is_max_of_inputs():
    """派生值只有在**全部**输入都已公告时才可知。"""
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q2, "income.n_income_attr_p", 50.0),   # 更晚公告
    ]))
    assert _value(out, "derived.cfo_to_np")["pub_date"] == PUB_Q2


def test_latest_revision_wins_and_pub_date_follows_revision():
    """同报告期的更正必须覆盖原值，且派生公告日跟着后一次公告走。"""
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 50.0),
        ("600000.SH", Q1, REV_Q1, "income.n_income_attr_p", 40.0),   # 更正
    ]))
    row = _value(out, "derived.cfo_to_np")
    assert row["value"] == pytest.approx(2.5)          # 100 / 40
    assert row["pub_date"] == REV_Q1


def test_missing_input_produces_no_row_rather_than_zero():
    """缺一个输入 → 整条缺失。绝不用 0 兜底（0 会被当成「现金流为 0」）。"""
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
    ]))
    assert out.filter(pl.col("item") == "derived.cfo_to_np").is_empty()


# ---------- 分母守卫 ----------

def test_zero_denominator_is_dropped():
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 0.0),
    ]))
    assert out.filter(pl.col("item") == "derived.cfo_to_np").is_empty()


def test_negative_denominator_allowed_for_cfo_but_not_for_interest():
    """净利润为负是**有意义**的（亏损公司）；利息费用为负则没有含义。"""
    rows = [
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 100.0),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", -50.0),
        ("600000.SH", Q1, PUB_Q1, "indicator.ebit", 30.0),
        ("600000.SH", Q1, PUB_Q1, "income.fin_exp_int_exp", -10.0),
    ]
    out = derive_pit(_panel(rows))
    cfo = _value(out, "derived.cfo_to_np")
    assert cfo is not None and cfo["value"] == pytest.approx(-2.0)
    assert out.filter(pl.col("item") == "derived.ebit_to_interest").is_empty()


def test_non_finite_result_is_dropped():
    """inf / nan 绝不能进分位 —— 一个 inf 会把 P75 直接拉爆。"""
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "cashflow.n_cashflow_act", 1e308),
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 1e-308),
    ]))
    import math

    vals = out.filter(pl.col("item") == "derived.cfo_to_np")["value"].to_list()
    assert all(math.isfinite(v) for v in vals)


# ---------- 逐条口径 ----------

def test_turnover_days_formulas():
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "indicator.ar_turn", 4.0),
        ("600000.SH", Q1, PUB_Q1, "balancesheet.inventories", 50.0),
        ("600000.SH", Q1, PUB_Q1, "income.oper_cost", 100.0),
    ]))
    assert _value(out, "derived.ar_turn_days")["value"] == pytest.approx(90.0)
    assert _value(out, "derived.inv_turn_days")["value"] == pytest.approx(180.0)


def test_empty_and_bad_input():
    assert derive_pit(pl.DataFrame()).is_empty()
    with pytest.raises(KeyError, match="缺列"):
        derive_pit(pl.DataFrame({"symbol": ["x"]}))


def test_derived_labels_covers_every_spec():
    labels = derived_labels()
    assert set(labels) == set(DERIVED_METRICS)
    assert all(v for v in labels.values())


def test_denominator_present_but_numerator_absent():
    """分母有值、分子整个缺失 → 该派生指标不应产出任何行。

    与 test_missing_input_produces_no_row_rather_than_zero 是**两个不同的分支**：
    那条走的是「分母缺失」提前返回，这条走的是「分子切片为空」。
    """
    out = derive_pit(_panel([
        ("600000.SH", Q1, PUB_Q1, "income.n_income_attr_p", 50.0),
    ]))
    assert out.filter(pl.col("item") == "derived.cfo_to_np").is_empty()
    # 其它分母存在的指标照常
    assert out.filter(pl.col("item") == "derived.cfo_to_or").is_empty()
