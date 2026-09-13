"""因子评级回归：Strong / Moderate / Weak 的边界，以及多重检验校正的降级作用。

评级是给 Agent 的唯一「够不够格」结论，边界错一档就会把噪声当 alpha 推上去，
所以这些用例全部用手工构造的指标字典，把每条判定线单独钉住。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from lquant.factors.evaluate.ic import ic_summary
from lquant.factors.evaluate.quantile import quantile_summary
from lquant.factors.evaluate.rating import (
    RatingThresholds,
    factor_rating,
    load_thresholds,
)


def _ic(mean: float = 0.04, ir: float = 0.6, t_nw: float = 4.0) -> dict:
    sub = {"mean": mean, "ir": ir, "t_stat_nw": t_nw, "t_stat": t_nw}
    return {"ic": dict(sub), "rank_ic": dict(sub)}


def _q(mono: float = 0.9, sharpe: float = 1.5) -> dict:
    return {"monotonicity": mono, "long_short": {"sharpe": sharpe}}


# ────────────────────────── 三档边界 ──────────────────────────

def test_rating_strong_requires_all_four():
    r = factor_rating(_ic(), _q())
    assert r["rating"] == "strong"
    assert r["score"] == 2
    assert r["blockers"] == []


def test_rating_moderate_when_icir_short_of_strong():
    """ICIR 0.4：过 moderate 线但不到 strong，其余项全优也不给 strong。"""
    r = factor_rating(_ic(ir=0.4), _q())
    assert r["rating"] == "moderate"
    assert any("ICIR" in b for b in r["blockers"])


def test_rating_moderate_when_only_ls_sharpe_qualifies():
    r = factor_rating(_ic(ir=0.1), _q(sharpe=0.6))
    assert r["rating"] == "moderate"
    assert any("多空夏普" in s for s in r["reasons"])


def test_rating_weak_when_ic_is_noise():
    """|IC| 掉到有效线以下 → 直接 weak，别管 ICIR 多漂亮。"""
    r = factor_rating(_ic(mean=0.005, ir=0.9), _q())
    assert r["rating"] == "weak"
    assert any("噪声" in s for s in r["reasons"])


def test_rating_weak_when_nothing_qualifies():
    r = factor_rating(_ic(mean=0.025, ir=0.1), _q(mono=0.2, sharpe=0.1))
    assert r["rating"] == "weak"


def test_rating_skips_quantile_checks_when_absent():
    """没跑分层时只按 IC 评级 —— 不能因为「没有单调性」就把强因子打成 weak。"""
    r = factor_rating(_ic(), None)
    assert r["rating"] == "strong"
    assert r["monotonicity"] is None or np.isnan(r["monotonicity"])


# ────────────────────────── 多重检验校正 ──────────────────────────

def test_rating_multiple_testing_downgrades_weak_t():
    """t=2.5 在单次检验里显著，但搜了 2000 次之后门槛是 3.90 —— 不能给 strong。"""
    ok = factor_rating(_ic(t_nw=2.5), _q())
    assert ok["rating"] == "strong"          # 未声明 n_trials → 不启用校正
    searched = factor_rating(_ic(t_nw=2.5), _q(), n_trials=2000)
    assert searched["rating"] == "moderate"
    assert searched["significant"] is False
    assert searched["t_threshold"] > 3.0


def test_rating_threshold_config_can_add_fixed_t_line():
    th = RatingThresholds(t_stat_strong=5.0)
    r = factor_rating(_ic(t_nw=4.0), _q(), thresholds=th)
    assert r["significant"] is False
    assert r["rating"] == "moderate"


# ────────────────────────── 取值口径 ──────────────────────────

def test_rating_prefers_rank_ic_over_pearson():
    """RankIC 说好、Pearson IC 说差 → 按 RankIC 判（A 股极端值多）。"""
    summ = {"ic": {"mean": 0.004, "ir": 0.05, "t_stat_nw": 0.4},
            "rank_ic": {"mean": 0.04, "ir": 0.6, "t_stat_nw": 4.0}}
    r = factor_rating(summ, _q())
    assert r["source"] == "rank_ic"
    assert r["rating"] == "strong"


def test_rating_handles_missing_and_nan_gracefully():
    r = factor_rating({"ic": {"mean": float("nan"), "ir": float("nan")}}, None)
    assert r["rating"] == "weak"
    r2 = factor_rating({}, {})
    assert r2["rating"] == "weak"
    assert r2["source"] == "ic"


# ────────────────────────── 阈值配置 ──────────────────────────

def test_load_thresholds_defaults_when_file_missing():
    th = load_thresholds("/nonexistent/rating.yaml")
    assert th.icir_strong == 0.5
    assert th.ic_moderate == 0.02


def test_load_thresholds_reads_partial_custom_file(tmp_path):
    p = tmp_path / "rating.yaml"
    p.write_text("rating:\n  icir_strong: 0.9\n  ic_moderate: 0.05\n", encoding="utf-8")
    th = load_thresholds(str(p))
    assert th.icir_strong == 0.9
    assert th.ic_moderate == 0.05
    assert th.ls_sharpe_strong == 1.0          # 未覆盖的字段回退默认


def test_rating_thresholds_from_dict_ignores_unknown_keys():
    th = RatingThresholds.from_dict({"icir_strong": 0.7, "unknown_key": 1})
    assert th.icir_strong == 0.7


def test_repo_rating_config_is_valid():
    """仓库自带的 config/factors/rating.yaml 必须能被解析成阈值。

    键名写错不会报错（只回退默认值），所以这条用例专门盯住「配置是否真的生效」：
    它跑的是默认路径 load_thresholds()，读的就是仓库里那份文件。
    """
    from lquant.core.config import load_yaml

    th = load_thresholds()
    raw = load_yaml("factors/rating.yaml")["rating"]
    assert th.icir_strong == float(raw["icir_strong"])
    assert th.ic_moderate == float(raw["ic_moderate"])
    assert 0 < th.ic_moderate <= th.ic_strong <= 1


# ────────────────────────── 端到端接线 ──────────────────────────

def test_rating_end_to_end_on_synthetic_strong_factor():
    """从真实 ic_summary/quantile_summary 的输出来 → 必须评为 strong。"""
    rng = np.random.default_rng(5)
    rows = []
    for d in range(60):
        for i in range(10):
            f = float(i)
            rows.append({"trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=d),
                         "symbol": f"S{i:02d}", "f": f,
                         "fwd_ret_1": f * 0.001 + rng.normal(0, 0.0002)})
    df = pl.DataFrame(rows)
    r = factor_rating(ic_summary(df, "f", "fwd_ret_1"),
                      quantile_summary(df, "f", "fwd_ret_1", 5))
    assert r["rating"] == "strong"
    assert r["icir"] > 1.0
    assert r["monotonicity"] > 0.8
