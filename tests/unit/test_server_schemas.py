"""server/schemas 单元测试。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from lquant.server.schemas import BacktestRequest, FactorCreate, JobStatus


def test_factor_create_defaults():
    f = FactorCreate(name="n", expr="$close")
    assert f.category == "custom"
    assert f.universe == "all"
    assert f.min_window == 0
    f.model_dump()


def test_factor_create_missing_required():
    with pytest.raises(ValidationError):
        FactorCreate(name="n")  # 缺 expr


def test_backtest_request_full():
    r = BacktestRequest(
        strategy="demo",
        start=date(2024, 1, 1),
        end=date(2024, 6, 30),
        params={"a": 1},
        universe="hs300",
        initial_cash=5e5,
        benchmark="000905.SH",
        # date 字符串也可自动解析
    )
    assert r.initial_cash == 5e5


def test_backtest_request_date_from_string():
    r = BacktestRequest(strategy="demo", start="2024-01-01", end="2024-06-30")
    assert r.start == date(2024, 1, 1)
    assert r.end == date(2024, 6, 30)
    assert r.universe == "hs300"
    assert r.benchmark == "000300.SH"
    assert r.initial_cash == 1_000_000


def test_backtest_request_missing_required():
    with pytest.raises(ValidationError):
        BacktestRequest(strategy="demo", start="2024-01-01")  # 缺 end


def test_job_status_defaults():
    j = JobStatus(job_id="j1", status="pending")
    assert j.progress == 0.0
    assert j.message == ""
    j.model_dump()
