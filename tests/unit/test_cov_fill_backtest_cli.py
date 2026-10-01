"""批次一覆盖补充：cli/commands/backtest.py 全分支（打桩因子链路）。"""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
from click.testing import CliRunner

from lquant.cli.commands import backtest as bt


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _daily_df(n: int = 4) -> pl.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "trade_date": date(2024, 1, 1 + i),
            "symbol": f"{600000 + i:06d}.SH",
            "close": 10.0 + i,
        })
    return pl.DataFrame(rows)


def _patch_chain(monkeypatch, *, qsum=None, empty_df=False, cov=True):
    df = pl.DataFrame() if empty_df else _daily_df()
    monkeypatch.setattr("lquant.data.store.parquet.read_daily",
                        lambda **kw: df.lazy())
    from lquant.factors import analysis, covariates, evaluate, replication
    from lquant.factors.evaluate import quantile
    from lquant.factors.preprocess import pipeline

    monkeypatch.setattr(analysis, "compute_factor_col",
                        lambda df, expr, name: df.with_columns(
                            pl.lit(1.0).alias(name)))
    monkeypatch.setattr(covariates, "build_covariates",
                        lambda df, cols, industry_df=None: (
                            df, [{"covariate": "industry_sw1", "coverage": 1.0}]
                        ) if cov else (df, []))
    monkeypatch.setattr(evaluate, "forward_return",
                        lambda d, col, periods: d.with_columns(
                            pl.col("close").shift(-1).alias("fwd_ret_1")))
    monkeypatch.setattr(quantile, "quantile_summary",
                        lambda d, f, r, n: qsum if qsum is not None else {
                            "groups": {"1": 0.01}, "long_short": {"spread": 0.02},
                            "monotonicity": 1.0})
    monkeypatch.setattr(pipeline, "drop_nonfinite", lambda d, col: d)
    monkeypatch.setattr(pipeline, "run", lambda d, col, steps: d)
    monkeypatch.setattr(replication, "load_spec",
                        lambda path: type("S", (), {
                            "expr": "rank(close)",
                            "window": {"start": "2024-01-01", "end": "2024-01-04"},
                        })())


def test_run_requires_factor_or_spec(fake_settings, monkeypatch):
    _patch_chain(monkeypatch)
    r = CliRunner().invoke(bt.run, [])
    assert r.exit_code != 0
    assert "需要 --factor" in r.output


def test_run_empty_daily_raises(fake_settings, monkeypatch):
    _patch_chain(monkeypatch, empty_df=True)
    r = CliRunner().invoke(bt.run, ["--factor", "close"])
    assert r.exit_code != 0
    assert "日线数据为空" in r.output


def test_run_success_without_industry_table(fake_settings, monkeypatch):
    """industry_classify 缺表 → ind=None 兜底，流水线仍走 cov_cols 中性化。"""
    _patch_chain(monkeypatch)
    r = CliRunner().invoke(bt.run, ["--factor", "close", "--start", "2024-01-01",
                                    "--end", "2024-01-04"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["factor"] == "close"
    assert payload["n_groups"] == 5
    assert payload["long_short"]["spread"] == 0.02
    assert "多空分层回测" in payload["note"]


def test_run_success_with_industry_table(fake_settings, monkeypatch):
    _patch_chain(monkeypatch)
    from lquant.core.db import writer

    with writer() as con:
        con.execute("CREATE TABLE industry_classify ("
                    "symbol VARCHAR, std VARCHAR, code VARCHAR, name VARCHAR, "
                    "std_date DATE, source VARCHAR)")
    r = CliRunner().invoke(bt.run, ["--factor", "close"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["monotonicity"] == 1.0


def test_run_with_spec_yaml(fake_settings, monkeypatch, tmp_path):
    _patch_chain(monkeypatch, cov=False)  # coverage=0 → 不走中性化流水线
    spec = tmp_path / "spec.yaml"
    spec.write_text("expr: rank(close)\n", encoding="utf-8")
    r = CliRunner().invoke(bt.run, ["--spec", str(spec)])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["factor"] == "rank(close)"


def test_run_empty_groups_raises(fake_settings, monkeypatch):
    _patch_chain(monkeypatch, qsum={"groups": {}, "long_short": {}})
    r = CliRunner().invoke(bt.run, ["--factor", "close"])
    assert r.exit_code != 0
    assert "分层结果为空" in r.output
