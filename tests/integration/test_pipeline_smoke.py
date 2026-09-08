"""端到端冒烟：解析因子 → 计算 → （可选）评价。"""
import polars as pl

from lquant.factors.engine import FactorEngine
from lquant.factors.ops import cs_ops, ts_ops  # noqa: F401


def test_compute_factor(daily_bars):
    eng = FactorEngine(daily_bars.lazy())
    out = eng.compute("Ts_Return($close, 1)", "ret1")
    assert "ret1" in out.columns
    assert out["ret1"].null_count() >= 1   # 首日无收益
