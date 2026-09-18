"""`lq factor` 其余子命令覆盖补齐（add/eval 配额/submit/mine/series/corr/audit/robust/report）。

demo 合成环境离线跑；挖掘会话的 run_session 打桩 —— CLI 层只测参数流与记账分支。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from types import SimpleNamespace

import pytest
import polars as pl
import yaml
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("cli_env")


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("cli_factor_more")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["factor", *args])


def test_add_registers_expr():
    r = _invoke("add", "Ts_Mean($close,5)", "--name", "my_fac")
    assert r.exit_code == 0, r.output
    assert "OK my_fac" in r.output
    r2 = _invoke("add", "Ts_Foo($close,5)")
    assert r2.exit_code != 0


def test_eval_with_agent_quota():
    r = _invoke("eval", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_trials"] >= 1
    assert body["quota_remaining"] is not None
    assert any("剩余配额" in h for h in body["hints"])


def test_eval_agent_unregistered():
    r = _invoke("eval", "Ts_Mean($close,5)", "--agent", "no-such-agent")
    assert r.exit_code != 0
    assert "Agent 未注册" in r.output


def test_eval_quota_exhausted(monkeypatch):
    import lquant.factors.agents as agents_mod

    def _boom(agent, n):
        raise ValueError("配额不足: 需要 1 次，剩余 0 次")

    monkeypatch.setattr(agents_mod, "ensure_quota", _boom)
    r = _invoke("eval", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code != 0
    assert "配额不足" in r.output


def _write_spec(tmp_path, payload):
    fp = tmp_path / "spec.yaml"
    fp.write_text(yaml.safe_dump(payload, allow_unicode=True))
    return str(fp)


def test_submit_spec_ok_and_invalid(tmp_path):
    good = _write_spec(tmp_path, {"expr": "Ts_Mean($close,5)",
                                  "rationale": "测试提交"})
    r = _invoke("submit", good)
    assert r.exit_code == 0, r.output

    bad = _write_spec(tmp_path, {"no_expr": 1})
    r2 = _invoke("submit", bad)
    assert r2.exit_code != 0
    assert "spec 结构非法" in r2.output

    (tmp_path / "broken.yaml").write_text("- a\n- b\n")
    r3 = _invoke("submit", str(tmp_path / "broken.yaml"))
    assert r3.exit_code != 0


def _fake_run_session_result():
    return SimpleNamespace(
        n_evaluated=5, n_static_fail=1, n_low_ic=2, n_redundant=0,
        n_size_proxy=1, n_survivors=1, corrections=["fix: x"],
    ), ["Ts_Mean($close,5)"]


@pytest.fixture()
def stub_mining(monkeypatch):
    import lquant.factors.mining.runner as runner_mod

    seen = {}

    def fake_run_session(eng, df, gen, agent=None, n_candidates=0, covs=None):
        seen["n_candidates"] = n_candidates
        seen["agent"] = agent
        return _fake_run_session_result()

    monkeypatch.setattr(runner_mod, "run_session", fake_run_session)
    return seen


def test_mine_gp_and_random(stub_mining):
    r = _invoke("mine", "--generator", "gp", "--n", "3")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_evaluated"] == 5 and body["n_survivors"] == 1
    r2 = _invoke("mine", "--generator", "random", "--n", "3")
    assert r2.exit_code == 0, r2.output


def test_mine_proposals(stub_mining, tmp_path):
    import lquant.factors.mining.llm as llm_mod

    monkeypatch_load = llm_mod
    jsonl = tmp_path / "p.jsonl"
    jsonl.write_text('{"expr": "Ts_Mean($close,5)", "note": "n"}\n')
    orig = llm_mod.load_proposals
    monkeypatch_load.load_proposals = orig  # 真读文件
    r = _invoke("mine", "--generator", "proposals", "--proposals", str(jsonl))
    assert r.exit_code == 0, r.output

    r2 = _invoke("mine", "--generator", "proposals")
    assert r2.exit_code != 0
    assert "--proposals" in r2.output


def test_mine_agent_guards(monkeypatch):
    import lquant.factors.agents as agents_mod

    r = _invoke("mine", "--agent", "no-such-agent")
    assert r.exit_code != 0
    assert "Agent 未注册" in r.output

    frozen = SimpleNamespace(name="fz", enabled=False, quota_eval=200)
    monkeypatch.setattr(agents_mod, "find_agent", lambda n: frozen)
    r2 = _invoke("mine", "--agent", "fz")
    assert r2.exit_code != 0
    assert "已冻结" in r2.output

    ok = SimpleNamespace(name="ok", enabled=True, quota_eval=10)
    monkeypatch.setattr(agents_mod, "find_agent", lambda n: ok)
    monkeypatch.setattr(agents_mod, "quota_remaining", lambda n: 2)
    r3 = _invoke("mine", "--agent", "ok", "--n", "5")
    assert r3.exit_code != 0
    assert "超出剩余配额" in r3.output

    monkeypatch.setattr(agents_mod, "quota_remaining", lambda n: 100)
    r4 = _invoke("mine", "--agent", "ok", "--n", "50")
    assert r4.exit_code != 0
    assert "超出配额" in r4.output


def test_mine_db_write_failure_warns(monkeypatch, stub_mining):
    import lquant.core.db as db_mod

    class Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db_mod, "writer", lambda: Boom())
    r = _invoke("mine", "--generator", "gp", "--n", "3")
    assert r.exit_code == 0, r.output
    assert "记账落库失败" in r.output


def test_series_cmd():
    r = _invoke("series", "Ts_Mean($close,5)")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_days"] > 0


def test_corr_cmd():
    r = _invoke("corr", "Ts_Mean($close,5)", "Rank($close)", "--threshold", "0.9")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert "pairs" in body or "redundant" in body or isinstance(body, dict)


def test_audit_full():
    r = _invoke("audit", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["rating"] is not None
    assert body["n_days"]["train_days"] > 0
    assert body["decay"]["profile"]


def test_robust_with_agent():
    r = _invoke("robust", "Ts_Mean($close,5)", "--agent", "gp-internal",
                "--deltas", "0.1,0.2")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["expr"] == "Ts_Mean($close,5)"


def test_report_writes_html(tmp_path):
    out = tmp_path / "rep.html"
    r = _invoke("report", "Ts_Mean($close,5)", "--out", str(out))
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["bytes"] > 0
    assert out.exists()


def test_clean_and_parse_floats():
    from lquant.cli.commands.factor import _clean, _icir, _parse_floats

    assert _clean(float("nan")) is None
    assert _clean(float("inf")) is None
    assert _clean({"a": [float("nan"), 1.0]}) == {"a": [None, 1.0]}
    assert _parse_floats("0.1, 0.2,,") == (0.1, 0.2)
    assert math.isnan(_icir({}))
    assert _icir({"rank_ic": {"ir": 0.5}}) == 0.5
    assert math.isnan(_icir({"rank_ic": {"ir": None}}))


def test_run_cmd():
    r = _invoke("run", "--name", "whatever")
    assert r.exit_code == 0, r.output
    assert "compute whatever" in r.output


def test_eval_low_t_hint(monkeypatch):
    """|t| 低于校正门槛时应给提示 —— 用高 n_trials 抬高门槛触发。"""
    import lquant.factors.agents as agents_mod

    monkeypatch.setattr(agents_mod, "record_eval", lambda agent, n=1: 200)
    monkeypatch.setattr(agents_mod, "quota_remaining", lambda agent: 5)
    r = _invoke("eval", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert any("低于校正门槛" in h for h in body["hints"])


def _empty_panel(monkeypatch):
    import lquant.factors.mining.submit as submit_mod

    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda start=None: (pl.DataFrame(), []))


def test_eval_empty_panel(monkeypatch):
    _empty_panel(monkeypatch)
    r = _invoke("eval", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_eval_train_empty(monkeypatch):
    import lquant.factors.mining.submit as submit_mod

    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda start=None: (pl.DataFrame({"x": [1]}), []))
    monkeypatch.setattr(submit_mod, "_split_eval",
                        lambda df, covs, expr: {"train": pl.DataFrame()})
    r = _invoke("eval", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "train 段 IC 序列为空" in r.output


def test_series_empty(monkeypatch):
    import lquant.factors.mining.submit as submit_mod

    _empty_panel(monkeypatch)
    monkeypatch.setattr(submit_mod, "_split_eval",
                        lambda df, covs, expr: {"train": pl.DataFrame()})
    r = _invoke("series", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "train 段 IC 序列为空" in r.output


def test_corr_empty(monkeypatch):
    import lquant.data.store.parquet as parquet_mod

    monkeypatch.setattr(parquet_mod, "read_daily",
                        lambda start=None: pl.DataFrame().lazy())
    r = _invoke("corr", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def _audit_robust_report_empty(monkeypatch):
    import lquant.factors.mining.submit as submit_mod

    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda start=None: (pl.DataFrame(), []))


def test_audit_empty(monkeypatch):
    _audit_robust_report_empty(monkeypatch)
    r = _invoke("audit", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_robust_empty(monkeypatch):
    _audit_robust_report_empty(monkeypatch)
    r = _invoke("robust", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_report_empty(monkeypatch):
    _audit_robust_report_empty(monkeypatch)
    r = _invoke("report", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_audit_no_agent_and_unregistered(monkeypatch):
    r = _invoke("audit", "Ts_Mean($close,5)")   # 不带 --agent → _quota 空分支
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_trials"] is None

    import lquant.factors.agents as agents_mod

    monkeypatch.setattr(agents_mod, "find_agent", lambda n: None)
    r2 = _invoke("audit", "Ts_Mean($close,5)", "--agent", "ghost")
    assert r2.exit_code != 0
    assert "Agent 未注册" in r2.output


def test_audit_quota_exhausted(monkeypatch):
    import lquant.factors.agents as agents_mod

    def _boom(agent, n):
        raise ValueError("配额不足")

    monkeypatch.setattr(agents_mod, "ensure_quota", _boom)
    r = _invoke("audit", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code != 0
    assert "配额不足" in r.output


def test_mine_empty_panel(monkeypatch, stub_mining):
    import lquant.factors.mining.submit as submit_mod

    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda start=None: (pl.DataFrame(), []))
    r = _invoke("mine", "--generator", "gp", "--n", "3")
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_audit_low_t_hint(monkeypatch):
    """audit --agent 路径的「低于校正门槛」提示。"""
    import lquant.factors.agents as agents_mod

    monkeypatch.setattr(agents_mod, "record_eval", lambda agent, n=1: 200)
    monkeypatch.setattr(agents_mod, "quota_remaining", lambda agent: 5)
    r = _invoke("audit", "Ts_Mean($close,5)", "--agent", "gp-internal")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert any("低于校正门槛" in h for h in body["hints"])


def _seg_returns_empty(monkeypatch):
    """面板非空但 prepare_segment 切出空段 → audit/robust/report 的 train 空分支。"""
    import lquant.factors.mining.submit as submit_mod

    dates = [dt.date(2026, 1, d) for d in (5, 6, 7)]
    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda start=None: (pl.DataFrame({"trade_date": dates}), []))
    monkeypatch.setattr(submit_mod, "prepare_segment",
                        lambda df, covs, expr, dates, *, horizons=(1, 5): pl.DataFrame())


def test_audit_train_segment_empty(monkeypatch):
    _seg_returns_empty(monkeypatch)
    r = _invoke("audit", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "train 段为空" in r.output


def test_robust_train_segment_empty(monkeypatch):
    _seg_returns_empty(monkeypatch)
    r = _invoke("robust", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "train 段为空" in r.output


def test_report_train_segment_empty(monkeypatch):
    _seg_returns_empty(monkeypatch)
    r = _invoke("report", "Ts_Mean($close,5)")
    assert r.exit_code != 0
    assert "train 段为空" in r.output


def test_audit_attribution_with_industry(monkeypatch):
    """train 段补一列 cov_industry_sw1 → 走归因成功/失败两个分支。"""
    import polars as pl

    import lquant.factors.mining.submit as submit_mod

    orig = submit_mod.prepare_segment

    def seg_with_industry(df, cov_cols, expr, dates, *, horizons=(1, 5)):
        d = orig(df, cov_cols, expr, dates, horizons=list(horizons))
        return d.with_columns(pl.lit("银行", dtype=pl.String).alias("cov_industry_sw1"))

    monkeypatch.setattr(submit_mod, "prepare_segment", seg_with_industry)
    r = _invoke("audit", "Ts_Mean($close,5)")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["attribution"]["by"] == "cov_industry_sw1"
    assert "gross_exposure" in body["attribution"]


def test_audit_attribution_error_visible(monkeypatch):
    """归因失败必须可见：JSON 里带 error 字段而不是静默 null。"""
    import polars as pl

    import lquant.factors.evaluate as ev_mod
    import lquant.factors.mining.submit as submit_mod

    orig = submit_mod.prepare_segment

    def seg_with_industry(df, cov_cols, expr, dates, *, horizons=(1, 5)):
        d = orig(df, cov_cols, expr, dates, horizons=list(horizons))
        return d.with_columns(pl.lit("银行", dtype=pl.String).alias("cov_industry_sw1"))

    monkeypatch.setattr(submit_mod, "prepare_segment", seg_with_industry)
    monkeypatch.setattr(ev_mod, "attribution_summary",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _invoke("audit", "Ts_Mean($close,5)")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert "error" in body["attribution"]
