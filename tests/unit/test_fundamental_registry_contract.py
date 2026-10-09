"""指标目录的**物理键契约** —— 本文件就是那个曾经缺失的回归测试。

背景（真实事故）：``lquant/fundamental/metrics.py`` 里的 17 个 ``item`` 全部
沿用 FinancialTool 时代的 BaoStock 键名（``profit.roeAvg`` /
``balance.currentRatio`` / ``dupont.dupontNitogr`` / ``valuation.pe_ttm``），
而 ``financial_pit`` 里的真实键是 Tushare 的 ``indicator.*``。

后果不是报错，而是**静默全空**：``_load_financial`` 查不到任何行，接口返回
``available: false`` +「财务数据为空，请先执行回填」。用户于是反复重跑数据
回填，而问题在代码里 —— 库里当时有 7700 万行财务数据。

当时的测试发现不了：``test_fundamental_percentile.py`` 用同一批遗留键名造
合成数据，``test_api_port_endpoints.py`` 也照抄了一份键名清单，两边都自洽。

所以这里做两件事：
1. **固定**期望的键集合（改动必须是有意识的，并同步改这里）；
2. 若本机存在真实库，**直接对着真实库校验**每个键都存在 —— 这才是
   「键名对不对」唯一可靠的判据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lquant.fundamental import (
    METRICS,
    MODULE_WEIGHTS,
    VALUATION_ITEMS,
    metrics_by_module,
    pit_items,
    raw_items,
    total_weight,
    validate_modules,
)
from lquant.fundamental.derive import DERIVED_METRICS

#: 直接从 financial_pit 读的物理键（Tushare fina_indicator / 三表口径）
EXPECTED_PIT_ITEMS = {
    "indicator.roe",
    "indicator.netprofit_margin",
    "indicator.grossprofit_margin",
    "indicator.profit_to_gr",
    "indicator.assets_turn",
    "indicator.current_ratio",
    "indicator.quick_ratio",
    "indicator.debt_to_assets",
}

#: 逻辑键 → 需要的原始科目
EXPECTED_DERIVED_INPUTS = {
    "derived.cfo_to_np": {"cashflow.n_cashflow_act", "income.n_income_attr_p"},
    "derived.cfo_to_or": {"cashflow.n_cashflow_act", "income.total_revenue"},
    "derived.cfo_to_op": {"cashflow.n_cashflow_act", "income.operate_profit"},
    "derived.ar_turn_days": {"indicator.ar_turn"},
    "derived.inv_turn_days": {"balancesheet.inventories", "income.oper_cost"},
    "derived.ebit_to_interest": {"indicator.ebit", "income.fin_exp_int_exp"},
}

#: 估值逻辑键 → (湖分区, 列名)
EXPECTED_VALUATION = {
    "valuation.pe_ttm": ("daily", "pe_ttm"),
    "valuation.pb": ("daily", "pb_mrq"),
    "valuation.dividend_yield": ("daily_basic", "dv_ttm"),
}

#: financial_pit 里真实存在的前缀（DISTINCT 勘察：indicator/income/cashflow/balancesheet）
KNOWN_PREFIXES = {"indicator", "income", "cashflow", "balancesheet"}


def test_pit_items_match_expected_contract():
    assert set(pit_items()) == EXPECTED_PIT_ITEMS


def test_derived_inputs_match_expected_contract():
    got = {k: set(v.inputs) for k, v in DERIVED_METRICS.items()}
    assert got == EXPECTED_DERIVED_INPUTS


def test_valuation_items_match_expected_contract():
    assert {k: tuple(v) for k, v in VALUATION_ITEMS.items()} == EXPECTED_VALUATION


def test_every_metric_item_is_registered_exactly_once():
    """目录里的每个 item 都必须能落到某个取数来源上。"""
    assert len({m.item for m in METRICS}) == len(METRICS)
    derived = set(DERIVED_METRICS)
    valuation = set(VALUATION_ITEMS)
    for m in METRICS:
        if m.source == "pit":
            assert m.item in EXPECTED_PIT_ITEMS
        elif m.source == "derived":
            assert m.item in derived, f"{m.item} 未在 DERIVED_METRICS 里实现"
        else:
            assert m.item in valuation, f"{m.item} 未在 VALUATION_ITEMS 里实现"
    # 反向：实现了但没进目录 = 白算
    assert derived | valuation | EXPECTED_PIT_ITEMS == {m.item for m in METRICS}


def test_all_pit_keys_use_known_prefixes():
    for key in (*EXPECTED_PIT_ITEMS, *raw_items()):
        prefix = key.split(".", 1)[0]
        assert prefix in KNOWN_PREFIXES, f"{key} 的前缀 {prefix!r} 不在真实库前缀集合里"


def test_module_weights_and_directions():
    validate_modules()
    assert total_weight() == pytest.approx(100.0)
    for mod, items in metrics_by_module().items():
        assert sum(m.max_score for m in items) == pytest.approx(MODULE_WEIGHTS[mod])
    # 反向指标必须显式声明，否则「越低越好」的指标会被当成越高越好
    reverse = {m.item for m in METRICS if not m.higher_better}
    assert reverse == {
        "derived.ar_turn_days", "derived.inv_turn_days",
        "indicator.debt_to_assets", "valuation.pe_ttm", "valuation.pb",
    }


# --------------------------------------------------------------------------
# 对真实库校验（本机有库才跑；CI 无数据时跳过）
# --------------------------------------------------------------------------

def _real_db_items() -> set[str] | None:
    """真实 ``financial_pit`` 里的 item 集合；库不可用时返回 ``None``。"""
    import time

    import duckdb

    from lquant.core.config import get_settings

    path = Path(get_settings().duckdb_path)
    if not path.exists():
        return None
    for _ in range(6):
        try:
            con = duckdb.connect(str(path), read_only=True)
        except Exception:  # noqa: BLE001 - 服务进程持锁：跳过而不是误报失败
            time.sleep(0.5)
            continue
        try:
            rows = con.execute(
                "SELECT DISTINCT item, source FROM financial_pit").fetchall()
        except Exception:  # noqa: BLE001 - 表未建
            return None
        finally:
            con.close()
        # **空表必须当成「无法校验」而不是「校验失败」**：CI 会建出空的
        # ``financial_pit``（scripts/init_db.py），此时键集合为空，若照常断言就会
        # 把「没有数据可查」误报成「目录里的键全都不存在」—— 那正是这个文件
        # 最该避免的假警报。
        #
        # 演示数据（``source='demo'``）同样不算「可校验的真实数据」：
        # ``generate_demo`` 只合成行业/趋势分析需要的少数几个键（roe / or_yoy /
        # netprofit_yoy …），拿它校验「目录里的每个键都存在」必然失败，而失败
        # 的原因不是键名错了，是演示环境本来就没打算覆盖全部键。所以这里只挑
        # 非演示行；一条都没有就回到「无法校验」。
        items = {r[0] for r in rows if (r[1] or "") != "demo"}
        return items or None
    return None


@pytest.fixture(scope="module")
def real_items() -> set[str]:
    items = _real_db_items()
    if items is None:
        pytest.skip(
            "本机没有**有内容的** financial_pit（CI 建的是空表 / 全新 checkout）："
            "键名契约无法对真实数据校验。有数据的机器上这条会真正执行。"
        )
    return items


def test_real_db_contains_every_pit_key(real_items):
    """**这条就是真正能挡住原始事故的断言。**"""
    missing = sorted(EXPECTED_PIT_ITEMS - real_items)
    assert not missing, (
        f"目录声明了库里不存在的物理键：{missing}。\n"
        f"这会让这些指标永远取不到值，而且只在页面显示为「财务数据为空」。\n"
        f"库里可用的前缀：{sorted({i.split('.')[0] for i in real_items})}"
    )


def test_real_db_contains_every_derived_input(real_items):
    inputs = set(raw_items())
    missing = sorted(inputs - real_items)
    assert not missing, f"派生指标需要的原始科目在库里不存在：{missing}"


def test_real_db_has_no_legacy_fundamental_keys(real_items):
    """遗留键一旦被真实数据接受，说明有人把旧口径写回了库里。"""
    legacy_prefixes = ("profit.", "dupont.", "operation.", "balance.", "valuation.")
    overlap = sorted(
        k for k in real_items
        if k.startswith(legacy_prefixes) and k in {m.item for m in METRICS}
    )
    assert not overlap, f"真实库里出现了遗留口径的 financial_pit 键：{overlap}"


def test_valuation_lake_is_queried_lazily_not_from_financial_pit(real_items):
    """估值必须来自日线湖；若这些键出现在 financial_pit 里，说明有人写错了地方。"""
    wrong = sorted(k for k in EXPECTED_VALUATION if k in real_items)
    assert not wrong, f"估值键不该出现在 financial_pit：{wrong}"


# ---------- 目录自检的失败分支 ----------
# 这些分支的意义是「目录写错时导入即失败」。不测它们，
# 就等于把这类保护当成装饰 —— 真写错时没人知道它到底会不会抛。

def test_ratio_metric_rejects_unknown_source():
    from lquant.fundamental.metrics import RatioMetric

    with pytest.raises(ValueError, match="未知取数来源"):
        RatioMetric("indicator.x", "x", "profitability", 1.0, source="magic")


def test_validate_rejects_duplicate_items(monkeypatch):
    """重复 item 会让评分明细串行（同一行被算两次），必须导入即失败。"""
    from lquant.fundamental.metrics import RatioMetric

    bad = (RatioMetric("indicator.roe", "a", "profitability", 25.0),
           RatioMetric("indicator.roe", "b", "cashflow", 20.0),
           RatioMetric("indicator.x", "c", "efficiency", 15.0),
           RatioMetric("indicator.y", "d", "solvency", 20.0),
           RatioMetric("valuation.z", "e", "valuation", 20.0, source="valuation"))
    monkeypatch.setattr("lquant.fundamental.metrics.METRICS", bad)
    with pytest.raises(ValueError, match="重复"):
        validate_modules()


def test_validate_rejects_total_weight_not_100(monkeypatch):
    from lquant.fundamental import metrics as M
    from lquant.fundamental.metrics import RatioMetric

    monkeypatch.setattr(M, "MODULE_WEIGHTS", {"profitability": 50.0})
    monkeypatch.setattr(
        M, "METRICS",
        (RatioMetric("indicator.roe", "a", "profitability", 50.0),))
    with pytest.raises(ValueError, match="合计应为 100"):
        validate_modules()


def test_validate_rejects_pit_key_without_prefix(monkeypatch):
    """pit 源的键必须带 ``<前缀>.``；裸名说明键名口径写错了。

    权重校验排在前面，所以这里只在**真实目录**上把某一个键改成裸名 ——
    否则会先撞上「子项满分之和 ≠ 模块权重」，测不到这条。
    """
    from lquant.fundamental.metrics import METRICS, RatioMetric

    bad = tuple(
        RatioMetric("roe", m.label, m.module, m.max_score, m.higher_better, m.source)
        if m.item == "indicator.roe" else m
        for m in METRICS
    )
    monkeypatch.setattr("lquant.fundamental.metrics.METRICS", bad)
    with pytest.raises(ValueError, match="前缀"):
        validate_modules()


def test_validate_rejects_unknown_module(monkeypatch):
    from lquant.fundamental.metrics import RatioMetric

    bad = (RatioMetric("indicator.roe", "a", "nope", 1.0),)
    monkeypatch.setattr("lquant.fundamental.metrics.METRICS", bad)
    with pytest.raises(ValueError, match="未声明的模块"):
        validate_modules()


def test_metrics_by_module_of_keeps_undeclared_modules():
    """目录未声明的模块也要出现在分组里，否则明细会被静默漏计。"""
    from lquant.fundamental.metrics import RatioMetric, metrics_by_module_of

    got = metrics_by_module_of((RatioMetric("indicator.roe", "a", "weird", 1.0),))
    assert set(got) == {"weird"}

    # 而对外暴露的 metrics_by_module() 仍只返回已声明模块
    assert set(metrics_by_module()) == set(MODULE_WEIGHTS)
