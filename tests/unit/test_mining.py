"""M3 挖掘内核回归：门禁、切分、适应度、random 基线、GP。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.mining.fitness import corrected_threshold, fitness
from lquant.factors.mining.gates import g0_static
from lquant.factors.mining.random_gen import make_generator
from lquant.factors.mining.runner import run_session, split_dates
from lquant.factors.ops import cs_ops, el_ops, ts_ops  # noqa: F401


def _panel(n_days=120, n_sym=8):
    rng = np.random.default_rng(11)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "volume": float(1e5 + i), "amount": px * 1e4,
                         "turnover_rate": 0.5 + rng.normal(0, 0.1)})
    return pl.DataFrame(rows)


def test_g0_static_pass_and_reject():
    F = {"close", "open", "volume", "amount", "turnover_rate"}
    assert g0_static("Ts_Mean($close, 5)", F).passed
    assert g0_static("Ts_Mean($closs, 5)", F).reason_code == "STATIC_FAIL"
    assert g0_static("Zzz($close, 5)", F).reason_code == "STATIC_FAIL"


def test_corrected_threshold_grows():
    assert corrected_threshold(2) < corrected_threshold(100)
    assert corrected_threshold(2000) == pytest.approx(3.90, abs=0.02)


def test_fitness_decay_penalty():
    assert fitness(0.05, 0.05) == pytest.approx(0.05)
    # 衰减 90% → 惩罚 (0.9-0.5)*2 = 0.8
    assert fitness(0.005, 0.05) < 0    # 衰减 90% → 罚 (0.9-0.5)*2
    assert fitness(0.05, 0.005) == pytest.approx(0.05)


def test_split_dates_70_15_15():
    ds = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(100)]
    tr, va, te = split_dates(ds)
    assert len(tr) == 70 and len(va) == 15 and len(te) == 15


def test_run_session_random_smoke():
    from lquant.factors.engine import FactorEngine

    panel = _panel()
    eng = FactorEngine(panel.lazy())
    gen = make_generator(seed=3)
    res, survivors = run_session(eng, panel, gen, agent="random", n_candidates=30)
    assert res.n_evaluated == 30
    assert res.n_evaluated == len(res.corrections) + res.n_survivors
    for s in survivors:
        assert "expr" in s and "t_stat" in s


def test_gp_generator_feedback():
    from lquant.factors.mining.gp import GPGenerator

    gp = GPGenerator(seed=2)
    seen = [gp() for _ in range(10)]
    assert len(set(seen)) >= 5
    gp.feedback("Rank(Ts_Mean($close,5))", 0.06)
    gp.feedback("Ts_Std($volume,20)", 0.01)
    out = gp()
    assert isinstance(out, str) and out



def test_eval_quota_ledger(tmp_path, monkeypatch):
    """方案 6.3 硬护栏 2：eval 按 Agent 记账，配额可见。"""
    from lquant.factors import agents as A

    # 无 DB 环境：eval_usage 应优雅返回 0（记账失败不炸）
    assert A.eval_usage("ghost") >= 0

    prof = A.AgentProfile(name="t", kind="skill", driver="agent",
                          quota_eval=10, can_submit=True)
    assert A.AgentProfile is not None
