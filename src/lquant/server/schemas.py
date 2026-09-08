"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class FactorCreate(BaseModel):
    name: str
    expr: str
    category: str = "custom"
    universe: str = "all"
    min_window: int = 0


class BacktestRequest(BaseModel):
    strategy: str
    params: dict = Field(default_factory=dict)
    start: date
    end: date
    universe: str = "hs300"
    initial_cash: float = 1_000_000
    benchmark: str = "000300.SH"


class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: float = 0.0
    message: str = ""
