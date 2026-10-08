"""M3 挖掘内核回归：门禁、切分、适应度、random 基线、GP。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.mining.fitness import corrected_threshold, fitness
from lquant.factors.mining.gates import g0_static, g1_fast_screen
from lquant.factors.mining.random_gen import make_generator
from lquant.factors.mining.runner import (
    horizon_from_ret_col,
    run_session,
    split_dates,
)
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


def test_split_dates_zero_purge_embargo_is_identical():
    """回归保护：显式传 0 必须与默认逐元素一致。"""
    ds = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(100)]
    assert split_dates(ds) == split_dates(ds, purge_bars=0, embargo_bars=0)


def test_split_dates_purge_embargo_has_no_time_overlap():
    """purge/embargo 生效后，用索引证明三段之间留了隔离带、且互不重叠。"""
    ds = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(100)]
    tr, va, te = split_dates(ds, purge_bars=1, embargo_bars=1)
    # 尾部各剪 1 根，头部再各推 1 根
    assert len(tr) == 69 and len(va) == 13 and len(te) == 14
    idx = {d: i for i, d in enumerate(ds)}
    # 训练末端与验证起点之间：1(purge) + 1(embargo) 根谁都不属于
    assert idx[va[0]] - idx[tr[-1]] - 1 == 2
    assert idx[te[0]] - idx[va[-1]] - 1 == 2
    # 训练集与测试集在时间上完全不重叠
    assert set(tr).isdisjoint(te) and set(tr).isdisjoint(va)
    assert max(tr) < min(va) and max(va) < min(te)


def test_split_dates_negative_purge_embargo_raises():
    ds = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(100)]
    with pytest.raises(ValueError, match="不能为负"):
        split_dates(ds, purge_bars=-1)
    with pytest.raises(ValueError, match="不能为负"):
        split_dates(ds, embargo_bars=-1)


def test_horizon_from_ret_col():
    """purge 必须从标签列名解析出前瞻期数，不允许拍魔法常数。"""
    assert horizon_from_ret_col("fwd_ret_1") == 1
    assert horizon_from_ret_col("fwd_ret_5") == 5
    with pytest.raises(ValueError, match="解析前瞻期数"):
        horizon_from_ret_col("close")


def test_run_session_rejects_unparsable_label_col():
    """标签列名解析不了 → 直接报错。静默退回 purge=0 等于没做防泄漏。"""
    from lquant.factors.engine import FactorEngine

    panel = _panel()
    eng = FactorEngine(panel.lazy())
    with pytest.raises(ValueError, match="解析前瞻期数"):
        run_session(eng, panel, make_generator(seed=1), n_candidates=1,
                    ret_col="close")



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


def test_run_session_tolerates_exhausted_proposals():
    """有限提案生成器提前耗尽 = 提案用完了，不是错误 —— 不能把 n 估大了就崩。"""
    from lquant.factors.engine import FactorEngine
    from lquant.factors.mining.llm import make_generator as proposals_gen

    panel = _panel()
    eng = FactorEngine(panel.lazy())
    gen = proposals_gen([{"expr": "Ts_Mean($close,5)"},
                         {"expr": "-Ts_Return($close,10)"}])
    res, _ = run_session(eng, panel, gen, agent="llm", n_candidates=10)
    assert res.n_evaluated == 2


def test_mine_proposals_requires_file(tmp_path):
    """漏传 --proposals 必须给出可操作的报错，而不是 TypeError。"""
    from click.testing import CliRunner

    from lquant.cli.commands.factor import factor

    r = CliRunner().invoke(factor, ["mine", "--generator", "proposals", "--n", "1"])
    assert r.exit_code != 0
    assert "--proposals" in r.output


def test_mine_proposals_end_to_end(tmp_path, monkeypatch):
    """提案 JSONL → mine 全链路：Agent 只出字符串，门禁与记账都由平台跑。"""
    import json

    from click.testing import CliRunner

    from lquant.cli.commands.factor import factor
    from lquant.data.store import parquet as pq
    from lquant.factors.mining import submit

    panel = _panel()
    monkeypatch.setattr(pq, "read_daily", lambda *a, **k: panel.lazy())
    monkeypatch.setattr(submit, "_panel_with_covs", lambda start=None: (panel, []))

    prop = tmp_path / "props.jsonl"
    prop.write_text('{"expr": "Ts_Mean($close,5)", "note": "短均线"}\n'
                    '{"expr": "-Ts_Return($close,10)", "note": "10 日反转"}\n',
                    encoding="utf-8")
    r = CliRunner().invoke(factor, ["mine", "--generator", "proposals",
                                    "--proposals", str(prop), "--n", "2"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["n_evaluated"] == 2
    assert payload["agent"] == "gp-internal"
    assert payload["run_id"]



def test_eval_quota_ledger(tmp_path, monkeypatch):
    """方案 6.3 硬护栏 2：eval 按 Agent 记账，配额可见。"""
    from lquant.factors import agents as A

    # 无 DB 环境：eval_usage 应优雅返回 0（记账失败不炸）
    assert A.eval_usage("ghost") >= 0

    prof = A.AgentProfile(name="t", kind="skill", driver="agent",
                          quota_eval=10, can_submit=True)
    assert prof.quota_eval == 10



def test_g1_uses_train_subset_only():
    """G1 数据范围硬约束：IC 必须算在传入的 train 子集上，不许碰全量 panel。"""
    import polars as pl

    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.ic import ic_series

    panel = _panel()
    train_dates = sorted(panel["trade_date"].unique().to_list())[:70]
    train = panel.filter(pl.col("trade_date").is_in(train_dates))
    train = forward_return(train.sort(["symbol", "trade_date"]), "close", periods=[1])
    train = train.drop_nulls(["fwd_ret_1"])
    expr = "Ts_Return($close, 5)"
    r = g1_fast_screen(train, expr, "fwd_ret_1", engine=None)
    assert r.passed and r.ic is not None
    # 手工在 train 上重算同口径 IC
    d = compute_factor_col(train, expr, "f").drop_nulls(["f", "fwd_ret_1"])
    expect = float(ic_series(d, "f", "fwd_ret_1")["ic"].mean())
    assert abs(r.ic - expect) < 1e-9
