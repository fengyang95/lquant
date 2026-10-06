"""基本面 API 的单元级补充：表缺失降级、JSON 安全转换、样本不足分支。

这些分支在「一切正常」的集成测试里走不到，但它们正是线上最容易出事的地方
（表没建、分位样本不够、numpy 标量混进响应体）。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from lquant.server.api import fundamental as fd


class _BrokenCon:
    """模拟表不存在 / 未同步：execute 直接抛。"""

    def execute(self, *a, **kw):  # noqa: ANN002, ANN003
        raise RuntimeError("Table with name financial_pit does not exist")


def test_load_financial_degrades_on_missing_table():
    out = fd._load_financial(_BrokenCon(), date(2026, 4, 1), ("indicator.roe",))
    assert isinstance(out, pl.DataFrame) and out.is_empty()


def test_load_industry_degrades_on_missing_table():
    out = fd._load_industry(_BrokenCon(), date(2026, 4, 1))
    assert isinstance(out, pl.DataFrame) and out.is_empty()


def test_json_safe_converts_numpy_scalars_and_survives_bad_item():
    assert fd._json_safe(np.array(5)) == 5                    # 0 维数组：.item() 可用
    # 多元素数组的 .item() 会抛 → 退化成字符串，而不是 500
    assert isinstance(fd._json_safe(np.array([1, 2])), str)


def test_json_safe_handles_nan_and_nesting():
    assert fd._json_safe(float("nan")) is None
    assert fd._json_safe(float("inf")) is None
    assert fd._json_safe({"a": [float("nan"), 1.0]}) == {"a": [None, 1.0]}
    assert fd._json_safe((True, None, "x")) == [True, None, "x"]


@pytest.fixture(autouse=True)
def _clear_fundamental_cache():
    """每个用例前后清空面板缓存。

    评分面板是按 ``(asof, min_samples)`` 缓存的重对象，用例之间共享同一组
    参数，不隔离就会读到上一个用例写入的 monkeypatch 结果 —— 表现为
    「单独跑绿、整文件跑红」。
    """
    fd.clear_fundamental_cache()
    yield
    fd.clear_fundamental_cache()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import os

    base = tmp_path_factory.mktemp("fd_unit")
    prev = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    yield base
    os.chdir(prev)
    get_settings.cache_clear()


def test_score_one_when_no_financial_data(env, monkeypatch):
    """财务表为空 → available=False + hint，而不是 500。"""
    monkeypatch.setattr(fd, "_load_financial", lambda *a, **k: pl.DataFrame())
    out = fd.score_one(symbol="600519.SH", asof="2026-04-01", min_samples=5)
    assert out["available"] is False and "财务数据为空" in out["hint"]


def test_score_one_when_percentiles_unavailable(env, monkeypatch):
    """面板非空但分位样本不足 → 走 detail 为空的分支。"""
    panel = pl.DataFrame({
        "symbol": ["600519.SH"], "stat_date": [date(2025, 12, 31)],
        "pub_date": [date(2026, 3, 20)], "item": ["indicator.roe"], "value": [20.0]})
    ind = pl.DataFrame({"symbol": ["600519.SH"], "std": ["sw"], "code": ["白酒"],
                        "name": ["白酒"], "std_date": [date(2024, 1, 1)]})
    monkeypatch.setattr(fd, "_load_financial", lambda *a, **k: panel)
    monkeypatch.setattr(fd, "_load_industry", lambda *a, **k: ind)
    out = fd.score_one(symbol="600519.SH", asof="2026-04-01", min_samples=100)
    assert out["available"] is False and "分位样本不足" in out["hint"]


def test_score_many_when_percentiles_unavailable(env, monkeypatch):
    panel = pl.DataFrame({
        "symbol": ["600519.SH"], "stat_date": [date(2025, 12, 31)],
        "pub_date": [date(2026, 3, 20)], "item": ["indicator.roe"], "value": [20.0]})
    ind = pl.DataFrame({"symbol": ["600519.SH"], "std": ["sw"], "code": ["白酒"],
                        "name": ["白酒"], "std_date": [date(2024, 1, 1)]})
    monkeypatch.setattr(fd, "_load_financial", lambda *a, **k: panel)
    monkeypatch.setattr(fd, "_load_industry", lambda *a, **k: ind)
    out = fd.score_many(fd.UniverseIn(asof="2026-04-01", min_samples=100))
    assert out["available"] is False and out["rows"] == []


def test_score_many_when_no_financial_data(env, monkeypatch):
    monkeypatch.setattr(fd, "_load_financial", lambda *a, **k: pl.DataFrame())
    out = fd.score_many(fd.UniverseIn(asof="2026-04-01"))
    assert out["available"] is False and out["n_scored"] == 0


def test_reconcile_rejects_blank_symbol():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        fd.run_reconcile(fd.ReconcileIn(symbol="      "))
    assert e.value.status_code == 422
