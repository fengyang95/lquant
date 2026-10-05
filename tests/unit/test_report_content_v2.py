"""报告 v2 内容契约 + 容量/样本过滤新模块。

覆盖上一轮两份评审（`docs/因子报告设计评审.md` R1–R26、
`docs/因子报告内容完备性评估.md` A–G）里「已修复」的那些点：
身份与口径、结论层、静默可见、格式化统一、表格不静默截断、研报三件套、容量、样本过滤。
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.factors.evaluate import report as rep
from lquant.factors.evaluate.capacity import capacity_summary
from lquant.factors.evaluate.returns import forward_return
from lquant.factors.evaluate.sample import (
    apply_sample_filters,
    describe_sample_filters,
)

# 18 只 × 3 行业 × 60 天；amount 与行业都齐，便于算容量与归因
_SPEC = [  # (symbol, drift, amount, industry, is_st)
    ("A1", 0.05, 1e7, "银行", False), ("A2", 0.04, 2e7, "银行", False),
    ("A3", 0.03, 3e7, "银行", True), ("A4", 0.02, 4e7, "银行", False),
    ("A5", 0.018, 4.5e7, "银行", False), ("A6", 0.016, 5e7, "银行", False),
    ("B1", 0.02, 5e8, "医药", False), ("B2", 0.015, 6e8, "医药", False),
    ("B3", 0.01, 7e8, "医药", True), ("B4", 0.005, 8e8, "医药", False),
    ("B5", 0.004, 8.5e8, "医药", False), ("B6", 0.003, 9e8, "医药", False),
    ("C1", 0.004, 9e8, "钢铁", False), ("C2", 0.003, 1e9, "钢铁", False),
    ("C3", 0.002, 1.1e9, "钢铁", False), ("C4", 0.001, 1.2e9, "钢铁", False),
    ("C5", 0.0008, 1.3e9, "钢铁", False), ("C6", 0.0005, 1.4e9, "钢铁", False),
]


def _df(n: int = 60, *, with_flags: bool = True) -> pl.DataFrame:
    """确定性面板。

    因子值带一个按 (日, 标的) 变化的成分 —— 否则分位组会员天天一样，
    换手率恒为 0，容量/换手相关断言全部失效。
    """
    d0 = date(2024, 1, 2)
    rows = []
    for j, (s, drift, amt, ind, st) in enumerate(_SPEC):
        for i in range(n):
            r = {
                "symbol": s,
                "trade_date": d0 + timedelta(days=i),
                "close": 10.0 * (1.0 + drift) ** i,
                "amount": amt,
                "market_cap": amt,          # 归因的「分组特征」需要一个数值协变量
                "industry_sw1": ind,
                "mom": 0.02 * j + 0.3 * math.sin(i * 0.7 + j * 1.3),
            }
            if with_flags:
                r["is_st"] = st
                r["is_suspended"] = (i % 17 == 0 and s == "A1")
            rows.append(r)
    return forward_return(pl.DataFrame(rows), periods=[1])


def _section(html: str, name: str) -> str:
    """取某个 <h2> 小节的内容（到下一个 <h2> 或 footer 为止）。"""
    m = re.search(r"<h2>" + re.escape(name) + r"[^<]*</h2>(.*?)(?=<h2>|<footer>)",
                  html, re.S)
    return m.group(0) if m else ""


# --------------------------------------------------------------------------- #
# 容量 / 流动性（D4）
# --------------------------------------------------------------------------- #
def test_capacity_closed_form_and_monotonicity():
    df = _df()
    cap = capacity_summary(df, "mom", "fwd_ret_1", n_groups=3)
    assert cap["portfolio_adv"] > 0
    assert cap["turnover_avg"] > 0
    # 容量上限 = 参与率上限 × ADV / 日均换手（文档里写的闭式解）
    expected = cap["max_participation"] * cap["portfolio_adv"] / cap["turnover_avg"]
    assert cap["capacity_aum"] == pytest.approx(expected, rel=1e-9)
    # 参与率 = AUM × 日均换手 / ADV
    row = cap["rows"][0]
    assert row["participation"] == pytest.approx(
        row["aum"] * cap["turnover_avg"] / cap["portfolio_adv"], rel=1e-9)
    # 参与率随 AUM 单调上升；净收益随 AUM 单调下降
    parts = [r["participation"] for r in cap["rows"]]
    nets = [r["net_annual"] for r in cap["rows"]]
    assert parts == sorted(parts)
    assert nets == sorted(nets, reverse=True)


def test_capacity_requires_amount_column():
    df = _df().drop("amount")
    with pytest.raises(KeyError, match="成交额列"):
        capacity_summary(df, "mom", "fwd_ret_1", n_groups=3)


# --------------------------------------------------------------------------- #
# 样本过滤（A6）
# --------------------------------------------------------------------------- #
def test_sample_filter_applies_and_reports():
    df = _df()
    out = apply_sample_filters(df, exclude_st=True)
    assert len(out) < len(df)
    assert out["is_st"].sum() == 0

    desc = {d["key"]: d for d in describe_sample_filters(df, exclude_st=True)}
    assert desc["exclude_st"]["status"] == "applied"
    assert desc["exclude_suspended"]["status"] == "available_not_applied"


def test_sample_filter_unavailable_column_is_disclosed():
    """想剔但数据里没这一列 → 必须显式标成 unavailable，不能假装剔过了。"""
    df = _df().drop("is_st")
    desc = {d["key"]: d for d in describe_sample_filters(df, exclude_st=True)}
    assert desc["exclude_st"]["status"] == "unavailable"
    # 列不存在时不报错、也不改变行数
    assert len(apply_sample_filters(df, exclude_st=True)) == len(df)


# --------------------------------------------------------------------------- #
# 身份与口径（R1–R3 / A 类）
# --------------------------------------------------------------------------- #
def test_report_identifies_factor():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1",
                             display_name="MA20", expr="Ts_Mean($close,20)")
    assert "因子研究报告 · MA20" in html
    assert "Ts_Mean($close,20)" in html
    # 内部列名不该出现在标题里
    assert "因子研究报告 · mom" not in html


def test_report_discloses_recipe_and_sample_filters():
    html = rep.factor_report(
        _df(), "mom", "fwd_ret_1", display_name="MA20",
        steps=[{"op": "winsorize", "method": "mad"},
               {"op": "standardize", "method": "zscore"}],
        covariates={"industry_sw1": 0.98},
        sample_filters={"exclude_st": True, "exclude_suspended": False},
        universe="all", data_start="2024-01-02", data_end="2024-03-31",
        n_samples=1080)
    prov = _section(html, "样本与口径")
    assert prov, "缺少「样本与口径」节"
    assert "去极值" in prov and "标准化" in prov
    assert "98.00%" in prov                     # 协变量覆盖率
    assert "全市场" in prov                      # universe_label 生效，不是裸 "all"
    assert "2024-01-02 ~ 2024-03-31" in prov
    assert "1080" in prov
    assert "已剔除" in prov                       # ST 过滤状态


def test_report_default_recipe_disclosed_as_raw():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1")
    assert "原始因子直接评价" in html


# --------------------------------------------------------------------------- #
# 结论层（R4–R6 / F 类）
# --------------------------------------------------------------------------- #
def test_report_renders_rating_and_robustness():
    rating = {
        "rating": "strong", "source": "rank_ic", "ic_mean": 0.03, "icir": 0.6,
        "t_stat_nw": 3.2, "t_threshold": 2.5, "significant": True, "n_trials": 7,
        "monotonicity": 0.9, "ls_sharpe": 1.2,
        "reasons": ["|ICIR|=0.6 ≥ 0.5"], "blockers": [],
    }
    robustness = {
        "verdict": "robust", "n_passed": 4, "n_judged": 5,
        "checks": [
            {"name": "param_sensitivity", "status": "passed", "value": 0.9,
             "threshold": 0.8, "hint": "扰动下 ICIR 保持"},
            {"name": "oos_decay", "status": "failed", "value": 0.4,
             "threshold": 0.3, "hint": "样本外衰减偏大"},
            {"name": "time_stability", "status": "skipped", "hint": "样本不足"},
        ],
    }
    html = rep.factor_report(_df(), "mom", "fwd_ret_1",
                             display_name="MA20", rating=rating,
                             robustness=robustness)
    concl = _section(html, "结论")
    assert concl, "缺少「结论」节"
    assert "强" in concl                     # 评级徽章
    assert "稳健" in concl                    # 稳健性判定
    assert "参数扰动" in concl and "样本外衰减" in concl
    assert "通过" in concl and "未过" in concl and "跳过" in concl
    assert "|ICIR|=0.6 ≥ 0.5" in concl
    # 结论节必须排在最前面（在「样本与口径」之前）
    assert html.index("结论") < html.index("样本与口径")


def test_report_without_rating_has_no_conclusion_section():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1")
    assert "<h2>结论</h2>" not in html


# --------------------------------------------------------------------------- #
# 静默与截断（R7 / R8）
# --------------------------------------------------------------------------- #
def test_failure_is_visible_not_silent(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("源数据异常")

    monkeypatch.setattr(rep, "attribution_summary", boom)
    html = rep.factor_report(_df(), "mom", "fwd_ret_1", cat_col="industry_sw1")
    assert "归因分解" not in html
    assert "本节生成失败" in html
    assert "attribution" in html
    assert "源数据异常" in html


def test_table_truncation_is_disclosed():
    df = pl.DataFrame({"a": list(range(31))})
    html = rep._table(df, limit=20)
    assert "仅显示前 20 行（共 31 行）" in html
    # 不截断时不出现提示
    assert "仅显示" not in rep._table(df, limit=None)


def test_table_does_not_drop_nav_implicitly():
    html = rep._table(pl.DataFrame({"nav": [1.0], "a": [2.0]}))
    assert "<th>nav</th>" in html


# --------------------------------------------------------------------------- #
# 格式化统一（R9–R11）
# --------------------------------------------------------------------------- #
def test_fmt_integers_and_ratios():
    assert rep._fmt(2026.0) == "2026"          # 不再 2026.0000
    assert rep._fmt(171.0) == "171"
    assert rep._fmt_int(5.0) == "5"
    assert rep._fmt(41.8791, ratio=True) == "41.9x"
    assert rep._fmt(True, boolean=True) == "是"
    assert rep._fmt(False, boolean=True) == "否"
    # 旧行为保持
    assert rep._fmt(0.1234) == "0.1234"
    assert rep._fmt(True) == "True"
    assert rep._fmt(0.1234, pct=True) == "12.34%"


def test_cost_table_is_formatted():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1", n_groups=3,
                             bps_list=[0.0, 5.0, 30.0])
    cost = _section(html, "成本敏感性")
    assert cost, "缺少成本敏感性节"
    assert "%" in cost                        # 收益按百分比
    assert "x" in cost                        # 年化换手按倍数
    assert "是" in cost or "否" in cost        # viable 用中文
    assert "True" not in cost and "False" not in cost
    assert "0.0000" not in cost               # bps 不再 4 位小数


def test_ic_metrics_use_one_convention():
    """IC 族按原值、比率按 % —— 同一份报告里不再两种口径混用。"""
    html = rep.factor_report(_df(), "mom", "fwd_ret_1")
    decay = _section(html, "IC 衰减")
    yearly = _section(html, "分年度 IC")
    # positive_rate 两处都按 %
    assert "positive_rate" in decay and "positive_rate" in yearly
    # IC 本身不按 %（避免 0.03 → 3.00% 这种混用）
    assert "3.00%" not in decay


# --------------------------------------------------------------------------- #
# 研报三件套 + 中性化（R19 / C/E 类）
# --------------------------------------------------------------------------- #
def _extras() -> dict:
    return {
        "excess": {
            "benchmark": "股票池等权",
            "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "curves": {"Q3": [1.0, 1.01, 1.02]},
            "metrics": {"annual_excess": 0.08, "excess_sharpe": 1.1,
                        "excess_mdd": -0.05},
        },
        "top_n": [{"n": 3, "annual_return": 0.2, "annual_excess": 0.05,
                   "excess_sharpe": 1.0, "max_drawdown": -0.1,
                   "annual_turnover": 12.5}],
        "style_corr": {"max_abs": 0.11, "passed": True, "threshold": 0.14,
                       "styles": [{"style": "市值", "corr": 0.11}]},
        "neutral_ladder": [{"step": "raw", "ic_mean": 0.03, "n_days": 60},
                           {"step": "+market_cap", "ic_mean": 0.01, "n_days": 60}],
        "neutral_views": {"view": "factor_neutral",
                          "industry_group_quantile": [
                              {"group": "银行", "spread": 0.01}]},
        "group_ic_size": {"size_col": "cov_market_cap",
                          "rows": [{"group": "size_q1", "ic_mean": 0.05,
                                    "rank_ic_mean": 0.04, "ir": 0.3, "n_days": 60}]},
    }


def test_report_renders_extra_blocks():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1", n_groups=3,
                             extras=_extras())
    assert "超额收益" in html and "8.00%" in html
    assert "Top-N 持仓收缩" in html
    assert "风格相关性体检" in html
    assert "IC 归因阶梯" in html and "+market_cap" in html
    assert "中性化视图" in html
    assert "分组 IC · 市值分组" in html


def test_report_renders_capacity_block():
    cap = capacity_summary(_df(), "mom", "fwd_ret_1", n_groups=3)
    html = rep.factor_report(_df(), "mom", "fwd_ret_1", n_groups=3,
                             extras={"capacity": cap})
    sec = _section(html, "容量与流动性")
    assert sec
    assert "容量上限（估算）" in sec
    assert "是" in sec or "否" in sec


# --------------------------------------------------------------------------- #
# 归因不再白算（E2/E3）
# --------------------------------------------------------------------------- #
def test_attribution_renders_contribution_and_profile():
    html = rep.factor_report(_df(), "mom", "fwd_ret_1", n_groups=3,
                             cat_col="industry_sw1")
    assert "归因分解" in html
    assert "收益贡献" in html          # 此前被丢弃
    assert "分组特征" in html          # 此前被丢弃


# --------------------------------------------------------------------------- #
# 滚动多序列（R26） + 事件日按索引（R21）
# --------------------------------------------------------------------------- #
def test_rolling_plots_three_series():
    html = rep.factor_report(_df(n=90), "mom", "fwd_ret_1", window=20)
    sec = _section(html, "滚动窗口")
    assert "滚动 IC" in sec and "滚动 RankIC" in sec and "滚动 IR" in sec
    assert "20 交易日" in sec          # window 参数真的生效


def test_svg_multi_mark_index_uses_layout():
    """mark_index 由函数自己换算像素，调用方不再复刻 pad_l/pad_r。"""
    svg = rep._svg_multi([0, 1, 2], {"a": [1.0, 2.0, 3.0]}, mark_index=1)
    assert "stroke-dasharray" in svg
    # index=1 落在 w/2 附近（640 宽、左右各 46/10）→ 中点约 333
    m = re.search(r'x1="([\d.]+)"[^>]*stroke-dasharray', svg)
    assert m and 300 < float(m.group(1)) < 360


def test_rolling_window_param_reaches_report():
    a = rep.factor_report(_df(n=90), "mom", "fwd_ret_1", window=20)
    b = rep.factor_report(_df(n=90), "mom", "fwd_ret_1", window=40)
    assert "20 交易日" in a and "40 交易日" in b
