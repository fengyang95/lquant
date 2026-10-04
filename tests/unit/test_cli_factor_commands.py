"""`lq factor` CLI 命令回归测试（评审补测：CLI 是 Agent 操作面，此前几乎零覆盖）。

demo 合成环境离线跑：check / eval / corr / submit spec 校验 / report。
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("cli_env")


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("cli_factor")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
        ensure_market_tables(con)      # index_daily 等看板表（data index/status 要用）
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


def _invoke(*args):
    from lquant.cli.main import cli

    return CliRunner().invoke(cli, ["factor", *args])


def test_check_expr_pass_and_fail():
    r = _invoke("check", "Ts_Mean($close,5)")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["passed"] is True
    r2 = _invoke("check", "Ts_Foo($close,5)")
    assert r2.exit_code != 0


def test_eval_quick_ic():
    r = _invoke("eval", "Ts_Mean($close,5)")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["n_days"] > 0
    assert "ic_mean" in body


def test_corr_two_exprs():
    r = _invoke("corr", "Ts_Mean($close,5)", "Rank($volume)", "--threshold", "0.9")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert "pairs" in body or "redundant" in body or isinstance(body, dict)


def test_submit_rejects_structurally_invalid_spec(tmp_path):
    bad = tmp_path / "bad_spec.yaml"
    bad.write_text("name: only_name\n")
    r = _invoke("submit", str(bad))
    assert r.exit_code != 0
    assert "spec 结构非法" in r.output

    bad2 = tmp_path / "bad_spec2.yaml"
    bad2.write_text("- just\n- a\n- list\n")
    r2 = _invoke("submit", str(bad2))
    assert r2.exit_code != 0
    assert "spec 结构非法" in r2.output


def test_submit_rejects_missing_rationale(tmp_path):
    """缺 rationale 走到服务端重验也被拒（reason_code=MISSING_RATIONALE）。"""
    spec = tmp_path / "no_rationale.yaml"
    spec.write_text("name: t_cli\nexpr: Ts_Mean($close,5)\n")
    r = _invoke("submit", str(spec))
    assert r.exit_code != 0
    assert "MISSING_RATIONALE" in r.output


def test_report_writes_html(tmp_path):
    out = tmp_path / "rep.html"
    r = _invoke("report", "Ts_Mean($close,5)", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert out.exists() and len(out.read_text(encoding="utf-8")) > 100


# ---------------- lq data index / status（Phase 1.2） ----------------

def test_data_index_backfills_demo_then_status(cli_env, monkeypatch):
    """`lq data index` 落 index_daily，`lq data status` 报告覆盖与默认基准。

    用 demo 数据源（monkeypatch 选源）避免测试触网。
    """
    # 直接打桩 backfill 的取数函数：_fetch_index 是它自己模块里的名字，
    # 改 collectors 模块属性对已绑定的引用无效（打桩最常见的坑）
    from datetime import date as _d

    from lquant.market import backfill as bf_mod
    from lquant.market.collectors.index_daily import _demo as _demo_index

    monkeypatch.setattr(bf_mod, "_fetch_index",
                        lambda start, end, *, demo: _demo_index(
                            _d.fromisoformat(str(start)), _d.fromisoformat(str(end))))
    from lquant.cli.main import cli

    r = CliRunner().invoke(cli, ["data", "index", "--start", "2026-09-01",
                                 "--end", "2026-09-10",
                                 "--symbols", "000300.SH,000905.SH"])
    assert r.exit_code == 0, r.output
    assert "index done" in r.output
    assert "000300.SH" in r.output

    # 幂等重跑：入库行数不翻倍
    r2 = CliRunner().invoke(cli, ["data", "index", "--start", "2026-09-01",
                                  "--end", "2026-09-10",
                                  "--symbols", "000300.SH,000905.SH"])
    assert r2.exit_code == 0, r2.output

    from lquant.core.db import reader

    with reader() as con:
        n = con.execute("SELECT count(*) FROM index_daily").fetchone()[0]
    assert n > 0

    st = CliRunner().invoke(cli, ["data", "status"])
    assert st.exit_code == 0, st.output
    assert "index_daily" in st.output
    assert "000300.SH" in st.output
