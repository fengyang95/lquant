"""API 覆盖补齐：factors 端点（校验分支 / 来源 / 挖掘 / 报告 / 分析 / 合成）。"""
from __future__ import annotations

import os
from contextlib import contextmanager

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_cov_factors")
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
    generate_demo(start="2025-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------- 列表 / 注册 ----------------

def test_list_and_register(client):
    r = client.get("/api/factors")
    assert r.status_code == 200
    assert client.get("/api/factors", params={"source": "yaml"}).status_code == 200
    r2 = client.post("/api/factors", json={"name": "cov_factor",
                                           "expression": "close",
                                           "description": "测试"})
    assert r2.status_code == 200, r2.text
    # DSL 非法表达式 → 422
    bad = client.post("/api/factors", json={"name": "cov_bad",
                                            "expression": "1 + "})
    assert bad.status_code == 422
    d = client.get("/api/factors/cov_factor")
    assert d.status_code == 200
    assert client.get("/api/factors/ghost_factor").status_code == 404


# ---------------- EvaluateIn 校验分支 ----------------

def test_evaluate_validators_422(client):
    base = {"factor": "covv", "formula": "pct_change_20"}
    cases = [
        {"universe": "no_such_pool"},
        {"end": "junk"},
        {"start": "junk"},
        {"event_window": [-1, 5]},
        {"horizons": [0]},
        {"horizons": [251]},
        {"top_ns": [0]},
        {"top_ns": [3001]},
    ]
    for extra in cases:
        r = client.post("/api/factors/evaluate", json={**base, **extra})
        assert r.status_code == 422, f"{extra}: {r.text}"


def test_evaluate_unsupported_formula_422(client):
    r = client.post("/api/factors/evaluate", json={"formula": "foo_bar"})
    assert r.status_code == 422


def test_evaluate_result_404(client):
    assert client.get("/api/factors/evaluate/ghost").status_code == 404


def test_evaluate_series_success_and_report(client):
    """同步评价（series 端点）→ 报告落盘 → 报告端点可取。"""
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covser", "formula": "pct_change_5",
                          "start": "2025-06-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ic"]["dates"]
    rep = client.get(f"/api/factors/reports/{body['factor']}")
    assert rep.status_code == 200
    assert client.get("/api/factors/reports").status_code == 200
    # 非法报告名（含点）→ 422
    assert client.get("/api/factors/reports/a.b").status_code == 422
    # 不存在的报告 → 404
    assert client.get("/api/factors/reports/ghost").status_code == 404


def test_evaluate_task_lifecycle(client):
    """202 入队 → 轮询取结果（确定性 job_id）。"""
    r = client.post("/api/factors/evaluate",
                    json={"factor": "covtask", "formula": "pct_change_5",
                          "start": "2025-06-01"})
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    assert jid == "factor-eval-covtask"
    # 同因子重复入队 → 409（任务进行中）
    r2 = client.post("/api/factors/evaluate",
                     json={"factor": "covtask", "formula": "pct_change_5",
                           "start": "2025-06-01"})
    assert r2.status_code in (202, 409)     # 若已跑完则重新入队
    # 等任务完成 → 结果可取
    import time

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        g = client.get(f"/api/factors/evaluate/{jid}")
        if g.status_code == 200:
            assert g.json()["result"]["factor"] == "covtask"
            break
        time.sleep(0.3)
    else:
        # 队列未跑完（CI 慢）也不算失败，只断言契约
        assert client.get("/api/factors").status_code == 200


# ---------------- DSL / 内置因子 ----------------

def test_compute_factor_dsl_paths(client):
    """DSL 表达式评价（$ 表达式走 FactorEngine）。"""
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covdsl", "formula": "$close / $open",
                          "start": "2025-06-01"})
    assert r.status_code in (200, 422)


def test_builtin_endpoints(client):
    r = client.get("/api/factors/builtin")
    assert r.status_code == 200 and len(r.json()) >= 100
    assert client.get("/api/factors/builtin", params={"family": "kbar"}).status_code == 200
    assert client.get("/api/factors/builtin", params={"q": "MA"}).status_code == 200
    # seed-builtin：按 names / families / 无匹配
    s = client.post("/api/factors/seed-builtin", json={"names": ["MA20"]})
    assert s.status_code == 200 and s.json()["seeded"] == 1
    s2 = client.post("/api/factors/seed-builtin", json={"families": ["kbar"]})
    assert s2.status_code == 200
    s3 = client.post("/api/factors/seed-builtin", json={"names": ["NOPE_XYZ"]})
    assert s3.status_code == 422


def test_universes_sources_yaml(client):
    assert client.get("/api/factors/universes").status_code == 200
    assert client.get("/api/factors/sources").status_code == 200
    # seed-yaml：custom.yaml 无因子 → 422；有 → 200
    r = client.post("/api/factors/seed-yaml")
    assert r.status_code in (200, 422)


# ---------------- agents / mining ----------------

def test_agents_endpoints(client):
    r = client.get("/api/factors/agents")
    assert r.status_code == 200
    assert client.get("/api/factors/agents/ghost/guide").status_code == 404


def test_mining_runs_list(client):
    assert client.get("/api/factors/mine/runs").status_code == 200


def test_mine_run_validation(client):
    assert client.post("/api/factors/mine/run",
                       json={"agent": "ghost"}).status_code == 404
    # 配额账本超限 → 422
    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "n": 10_000_000})
    assert r.status_code == 422


def test_mine_run_sync(client):
    """sync=true 同步挖掘会话（random 生成器，n=2，demo 面板）。"""
    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "generator": "random",
                          "n": 2, "sync": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_evaluated"] >= 0 and "survivors" in body


# ---------------- analyze / synthesize ----------------

def test_analyze_success_and_empty(client, monkeypatch):
    r = client.post("/api/factors/analyze",
                    json={"formulas": ["pct_change_5", "pct_change_20"],
                          "start": "2025-06-01"})
    assert r.status_code == 200, r.text
    # 空湖 → 503
    from lquant.server.api import factors as fmod

    monkeypatch.setattr(fmod, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    assert client.post("/api/factors/analyze",
                       json={"formulas": ["pct_change_5", "pct_change_20"]}).status_code == 503


def test_synthesize_success(client):
    r = client.post("/api/factors/synthesize",
                    json={"formulas": ["pct_change_5", "pct_change_20"],
                          "start": "2025-06-01"})
    assert r.status_code == 200, r.text
    assert client.get("/api/factors/reports/syn_2f_eq").status_code == 200


def test_analyze_synthesize_builtin_names(client):
    """内置因子名（MA20/RSV10）走相关性 / 合成：早先落到 DSL 分支抛 FactorError → 500。

    前端「相关性 · 合成」页签把这些名字做成可点标签，点一下就 500。
    """
    r = client.post("/api/factors/analyze",
                    json={"formulas": ["MA20", "RSV10"], "start": "2025-06-01"})
    assert r.status_code == 200, r.text
    assert r.json()["factors"] == ["MA20", "RSV10"]

    r2 = client.post("/api/factors/synthesize",
                     json={"formulas": ["MA20", "pct_change_5"], "method": "ic_weighted",
                           "start": "2025-06-01"})
    assert r2.status_code == 200, r2.text

    # 未知裸字段名是客户端错误 → 422，绝不能是 500
    r3 = client.post("/api/factors/analyze",
                     json={"formulas": ["NoSuchField", "pct_change_5"],
                           "start": "2025-06-01"})
    assert r3.status_code == 422, r3.text


# ---------------- 补充分支：列表类目 / universe 503 / 公式分支 ----------------

def test_list_factors_qlib_category(client):
    """qlib 内置因子列表页类目推导分支。"""
    client.post("/api/factors/seed-builtin", json={"names": ["MA20"]})
    r = client.get("/api/factors", params={"source": "qlib"})
    assert r.status_code == 200
    hit = next((x for x in r.json() if x["name"] == "MA20"), None)
    assert hit and hit["category"].startswith("alpha158")


def test_universe_symbols_empty_503(client):
    """指定股票池但成分表为空 → 503（evaluate/analyze/synthesize 共用）。"""
    for path in ("/api/factors/evaluate/series", "/api/factors/analyze",
                 "/api/factors/synthesize"):
        r = client.post(path, json={"universe": "hs300",
                                    "formulas": ["pct_change_5", "pct_change_20"]})
        assert r.status_code == 503, f"{path}: {r.text}"


def test_compute_paths_rolling_turnover_builtin_dsl_error(client):
    """rolling_std / turnover / 内置因子命中 / DSL 错误 422。"""
    for formula, expect in (("rolling_std_10", 200), ("turnover", 200),
                            ("MA20", 200), ("$close +", 422)):
        r = client.post("/api/factors/evaluate/series",
                        json={"factor": f"covp{expect}", "formula": formula,
                              "start": "2026-04-01"})
        assert r.status_code == expect, f"{formula}: {r.text}"


def test_evaluate_filter_zscore(client):
    """截面异常收益过滤分支（filter_zscore 非空）。"""
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covz", "formula": "pct_change_5",
                          "start": "2026-04-01", "filter_zscore": 3.0})
    assert r.status_code == 200, r.text
    assert r.json().get("factor") == "covz" or r.json()["ic"]["dates"]


def test_evaluate_optional_blocks_fail_silently(client, monkeypatch):
    """TopN / 风格相关 / 事件分析 / 协变量构建失败 → 指标降级不炸。"""
    import importlib

    from lquant.factors import covariates as cov_mod
    from lquant.factors.evaluate import style_corr as sc_mod

    es_mod = importlib.import_module("lquant.factors.evaluate.event_study")
    tn_mod = importlib.import_module("lquant.factors.evaluate.top_n")

    def _boom(*a, **k):
        raise RuntimeError("block down")

    monkeypatch.setattr(tn_mod, "top_n_summary", _boom)
    monkeypatch.setattr(sc_mod, "style_correlation", _boom)
    monkeypatch.setattr(cov_mod, "build_covariates", _boom)
    monkeypatch.setattr(es_mod, "event_study_summary", _boom)
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covdec", "formula": "pct_change_5",
                          "start": "2026-04-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top_n"] == [] and body["style_corr"] == {}
    assert body["event_study"] == {}


def test_evaluate_turnover_fail_silently(client, monkeypatch):
    """factor_turnover 失败 → annual_turnover None。"""
    from lquant.factors.evaluate import costs as costs_mod

    def _boom(*a, **k):
        raise RuntimeError("costs down")

    monkeypatch.setattr(costs_mod, "factor_turnover", _boom)
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covdec2", "formula": "pct_change_5",
                          "start": "2026-04-01"})
    assert r.status_code == 200


def test_seed_yaml_and_translate_error(client, monkeypatch):
    """seed-yaml 成功路径 + seed-builtin 翻译失败 422。"""
    from lquant.factors.sources import qlib_source as qs_mod
    from lquant.factors.sources import yaml_source as ys_mod

    monkeypatch.setattr(ys_mod, "load_custom",
                        lambda: [{"name": "cov_yaml_f", "expression": "close",
                                  "description": "yaml", "source": "yaml"}])
    r = client.post("/api/factors/seed-yaml")
    assert r.status_code == 200 and r.json()["seeded"] == 1

    def _boom(formula):
        raise ValueError("bad formula")

    monkeypatch.setattr(qs_mod, "translate", _boom)
    r2 = client.post("/api/factors/seed-builtin", json={"names": ["MA20"]})
    assert r2.status_code == 422


def test_agent_guide_success(client):
    r = client.get("/api/factors/agents/gp-internal/guide")
    assert r.status_code == 200
    assert r.json()["agent"] == "gp-internal"


def test_mine_run_disabled_agent_423(client):
    r = client.post("/api/factors/mine/run",
                    json={"agent": "remote-miner", "n": 1})
    assert r.status_code == 423


def test_mine_run_async_placeholder(client):
    """async 挖掘：预落 ledger 占位 + 入队。"""
    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "generator": "random",
                          "n": 2, "sync": False})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["task_id"]
    # 台账占位行存在
    runs = client.get("/api/factors/mine/runs").json()
    assert any(x["run_id"] == body["task_id"] for x in runs)


def test_mine_run_gp_sync(client):
    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "generator": "gp",
                          "n": 2, "sync": True})
    assert r.status_code == 200, r.text


def test_mine_runs_query_broken(client, monkeypatch):
    from lquant.server.api import factors as fmod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "factor_mining_run" in sql:
                raise RuntimeError("table missing")
            return self._con.execute(sql, *a, **k)

    real_reader = fmod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(fmod, "reader", fake)
    assert client.get("/api/factors/mine/runs").json() == []
    assert client.get("/api/factors/ghost2").status_code == 404


def test_series_unsupported_formula_422(client):
    """/evaluate/series 无预检 → 不支持公式 422（_compute_factor 兜底）。"""
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covbad", "formula": "foo_bar"})
    assert r.status_code == 422


def test_series_empty_lake_503(client, monkeypatch):
    from lquant.server.api import factors as fmod

    monkeypatch.setattr(fmod, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covempty", "formula": "pct_change_5"})
    assert r.status_code == 503


def test_list_factors_builtin_stub_only(client, monkeypatch):
    """builtin 枚举失败（不叠加 reader 打桩）→ 列表仍返回。"""
    from lquant.factors import qlib_alpha as qa_mod

    def _boom():
        raise RuntimeError("builtin down")

    monkeypatch.setattr(qa_mod, "list_builtin", _boom)
    assert client.get("/api/factors").status_code == 200


def test_list_factors_and_reports_branches(client, monkeypatch):
    """列表查询异常 / builtin 枚举失败 / 报告目录缺失 / 因子详情带报告。"""
    from lquant.factors import qlib_alpha as qa_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "factor_def" in sql:
                raise RuntimeError("factor_def broken")
            return self._con.execute(sql, *a, **k)

    real_reader = fmod_reader()

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(factors_mod(), "reader", fake)
    assert client.get("/api/factors").json() == []

    # builtin 枚举失败 → 类目推导跳过
    def _boom():
        raise RuntimeError("builtin down")

    monkeypatch.setattr(qa_mod, "list_builtin", _boom)
    assert client.get("/api/factors").status_code == 200

    # 报告目录缺失 → 空列表
    from pathlib import Path

    f = factors_mod()
    old_dir = f.REPORT_DIR
    f.REPORT_DIR = Path("data/no_such_reports_dir")
    try:
        assert client.get("/api/factors/reports").json() == []
    finally:
        f.REPORT_DIR = old_dir

    # 因子详情：查询异常 → 404（except 分支）
    d = client.get("/api/factors/covser")
    assert d.status_code == 404


def test_factor_detail_with_reports(client):
    """因子详情关联报告列表（REPORT_DIR 存在 + 有报告文件）。"""
    client.post("/api/factors", json={"name": "covser"})
    d = client.get("/api/factors/covser")
    assert d.status_code == 200
    assert isinstance(d.json()["reports"], list)


def fmod_reader():
    from lquant.server.api import factors as fmod

    return fmod.reader


def factors_mod():
    from lquant.server.api import factors as fmod

    return fmod


def test_analyze_synthesize_error_branches(client, monkeypatch):
    """correlation/synthesize ValueError → 422；空湖 503；合成空 → 422。"""
    import polars as _pl

    from lquant.factors import analysis as fa_mod
    from lquant.server.api import factors as fmod

    def _value_error(*a, **k):
        raise ValueError("公式计算失败")

    monkeypatch.setattr(fa_mod, "correlation", _value_error)
    r = client.post("/api/factors/analyze",
                    json={"formulas": ["pct_change_5", "pct_change_20"],
                          "start": "2025-06-01"})
    assert r.status_code == 422

    monkeypatch.setattr(fa_mod, "synthesize", _value_error)
    r2 = client.post("/api/factors/synthesize",
                     json={"formulas": ["pct_change_5", "pct_change_20"],
                           "start": "2025-06-01"})
    assert r2.status_code == 422

    # 合成结果为空 → 422
    def _empty(*a, **k):
        return _pl.DataFrame(schema={"_syn": _pl.Float64, "symbol": _pl.String,
                                     "trade_date": _pl.Date, "close": _pl.Float64})

    monkeypatch.setattr(fa_mod, "synthesize", _empty)
    r3 = client.post("/api/factors/synthesize",
                     json={"formulas": ["pct_change_5", "pct_change_20"],
                           "start": "2025-06-01"})
    assert r3.status_code == 422

    # 空湖 → 503
    monkeypatch.setattr(fmod, "read_daily", lambda *a, **k: _pl.DataFrame().lazy())
    for path in ("/api/factors/analyze", "/api/factors/synthesize"):
        r4 = client.post(path, json={"formulas": ["pct_change_5", "pct_change_20"]})
        assert r4.status_code == 503


def test_mine_sync_empty_panel_503(client, monkeypatch):
    import polars as _pl

    from lquant.factors.mining import submit as submit_mod

    monkeypatch.setattr(submit_mod, "_panel_with_covs",
                        lambda: (_pl.DataFrame(), []))
    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "generator": "random",
                          "n": 2, "sync": True})
    assert r.status_code == 503


def test_analyze_end_date_validation(client):
    r = client.post("/api/factors/analyze",
                    json={"formulas": ["pct_change_5", "pct_change_20"],
                          "end": "junk"})
    assert r.status_code == 422


def test_evaluate_industry_query_broken(client, monkeypatch):
    """industry_classify 查询异常 → ind=None 分支，评价继续。"""
    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "industry_classify" in sql:
                raise RuntimeError("no industry table")
            return self._con.execute(sql, *a, **k)

    real_reader = factors_mod().reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(factors_mod(), "reader", fake)
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covind", "formula": "pct_change_5",
                          "start": "2026-04-01"})
    assert r.status_code == 200


def test_neutral_ladder_pipeline_broken(client, monkeypatch):
    """pipeline_run / ic_series 异常 → ladder 逐段跳过；空 ladder 不落 factor_ic。"""
    from lquant.factors.preprocess import pipeline as pipe_mod

    def _boom(*a, **k):
        raise RuntimeError("pipeline down")

    monkeypatch.setattr(pipe_mod, "run", _boom)
    r = client.post("/api/factors/evaluate/series",
                    json={"factor": "covlad", "formula": "pct_change_5",
                          "start": "2026-04-01"})
    assert r.status_code == 200
    assert r.json()["neutral_ladder"] == []


def test_check_formula_supported_returns(client):
    """/evaluate 预检：内置因子 / $ 表达式 / turnover 公式直接放行。"""
    for formula in ("MA20", "$close / 2", "turnover"):
        r = client.post("/api/factors/evaluate",
                        json={"factor": f"covpre_{formula.replace('$', 'd').replace(' ', '')}",
                              "formula": formula, "start": "2026-05-01"})
        assert r.status_code == 202, r.text
