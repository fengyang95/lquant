"""适配器协议测试：注册 / 获取 / 输出同形。"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.backtest.adapter import (
    AdapterOutput,
    get_adapter,
    list_adapters,
    register_adapter,
)


class _DummyAdapter:
    name = "dummy_test_engine"

    def run(self, df: pl.DataFrame, **params) -> AdapterOutput:
        return AdapterOutput(metrics={"total_return": 0.1},
                             meta={"params": params})


def test_adapter_register_and_get():
    register_adapter(_DummyAdapter)
    assert "dummy_test_engine" in list_adapters()
    out = get_adapter("dummy_test_engine").run(pl.DataFrame(), top_n=3)
    assert isinstance(out, AdapterOutput)
    assert out.metrics["total_return"] == 0.1
    assert out.meta["params"] == {"top_n": 3}


def test_unknown_adapter_raises():
    with pytest.raises(KeyError):
        get_adapter("no_such_engine")
