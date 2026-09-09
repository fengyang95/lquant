"""Rust 对齐对拍：Python 参考实现 vs Rust 扩展。

两类用例：
- **参考实现单测**（无 Rust 也跑）：锁 _rust/{ops,metrics,broker}_ref 的语义，
  它们是 Rust 的「参照物」，本身必须正确。
- **对拍**（`importorskip`，未编译 Rust 时整组 skip）：
  同一输入喂 Rust 扩展与 Python 参考，逐位一致；一处算法改动若只改了一侧，这里拦下。

运行：`make rust-build && pytest tests/unit/test_rust_alignment.py`
CI 冒烟不要求 Rust：`make test -m "not slow"` 会跳过对拍块。
"""
from __future__ import annotations

import importlib
import math

import pytest

from lquant._rust.broker_ref import match_order as ref_match_order
from lquant._rust.metrics_ref import max_drawdown, rank_ic
from lquant._rust.ops_ref import ts_corr, ts_regbeta

# ---------- 参考实现单测（常跑） ----------

class TestOpsRef:
    def test_ts_corr_perfect_positive(self):
        x = [1.0, 2.0, 3.0, 4.0]
        y = [10.0, 20.0, 30.0, 40.0]
        out = ts_corr(x, y, n=3)
        assert out[:2] == [None, None]          # 窗口不足 → None
        assert out[2:] == pytest.approx([1.0, 1.0])   # 完全线性正相关

    def test_ts_corr_zero_variance_is_none(self):
        x = [1.0, 1.0, 1.0, 1.0]
        y = [1.0, 2.0, 3.0, 4.0]
        # 窗口内 x 恒值 → vx<=0 → None（而非 NaN/0）
        assert ts_corr(x, y, n=3)[2] is None

    def test_ts_regbeta_slope(self):
        x = [1.0, 2.0, 3.0, 4.0]
        y = [2.0, 4.0, 6.0, 8.0]                # y = 2x
        out = ts_regbeta(y, x, n=3)
        assert out[2:] == pytest.approx([2.0, 2.0])

    def test_ts_regbeta_window_too_short(self):
        assert ts_regbeta([1, 2, 3], [1, 1, 1], n=5)[0] is None


class TestMetricsRef:
    def test_rank_ic_perfect(self):
        assert rank_ic([1, 2, 3, 4, 5], [0.1, 0.2, 0.3, 0.4, 0.5]) == pytest.approx(1.0)
        assert rank_ic([5, 4, 3, 2, 1], [0.1, 0.2, 0.3, 0.4, 0.5]) == pytest.approx(-1.0)

    def test_rank_ic_ties_average_rank(self):
        # 并列因子：[1,1,3] → 平均秩 [1.5,1.5,3]，比分等秩配对 IC 更高
        ic = rank_ic([1.0, 1.0, 3.0], [1.0, 3.0, 2.0])
        assert ic == pytest.approx(rank_ic([1.5, 1.5, 3.0], [1.0, 3.0, 2.0]))

    def test_rank_ic_insufficient_is_nan(self):
        assert math.isnan(rank_ic([1, 2], [1, 2]))

    def test_max_drawdown(self):
        assert max_drawdown([1.0, 1.2, 0.6, 1.0]) == pytest.approx(0.5)
        assert max_drawdown([1.0, 1.1, 1.2]) == pytest.approx(0.0)


class TestBrokerRef:
    def test_etf_no_stamp_tax_min_commission(self):
        # 对应 Rust 单测 etf_no_stamp_tax
        qty, fee = ref_match_order("510300.SH", True, 1000.0, 4.0,
                                   commission_rate=0.00025, commission_min=5.0,
                                   transfer_fee_rate=0.0, tax_rate=0.0, lot_size=100.0)
        assert qty == 1000.0
        assert fee == 5.0                        # amount*rate=1 < min 5 → 补到 5

    def test_sell_pays_stamp_tax(self):
        qty, fee = ref_match_order("000001.SZ", False, 1000.0, 10.0,
                                   commission_rate=0.00025, commission_min=5.0,
                                   transfer_fee_rate=0.00001, tax_rate=0.0005, lot_size=100.0)
        amount = 1000.0 * 10.0
        assert fee == pytest.approx(
            max(5.0, amount * 0.00025) + amount * 0.00001 + amount * 0.0005)

    def test_non_lot_multiple_floors(self):
        qty, _ = ref_match_order("600000.SH", True, 950.0, 10.0,
                                 commission_rate=0.00025, commission_min=5.0,
                                 transfer_fee_rate=0.0, tax_rate=0.0, lot_size=100.0)
        assert qty == 900.0


# ---------- 对拍：Rust 扩展 vs Python 参考（编译过 Rust 才跑） ----------

def _finite_close(a, b):
    """None 对 None；否则逐位有限值近似。"""
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        if x is None:
            assert y is None
        else:
            assert y is not None and x == pytest.approx(y, rel=1e-9, abs=1e-9)


def _rust_module(name: str):
    """惰性加载 Rust 扩展，未编译返回 None —— 对拍类据此 skipif。"""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


LQ_OPS = _rust_module("lq_ops")
LQ_METRICS = _rust_module("lq_metrics")
LQ_BACKTEST = _rust_module("lq_backtest")

_NO_RUST = "未编译 Rust：先 make rust-build"


@pytest.mark.rust
@pytest.mark.skipif(LQ_OPS is None, reason=_NO_RUST)
class TestOpsParity:
    # lq-ops 走 `#[pyfunction]` 数组接口（与 ops_ref 同形，绕开 polars 插件
    # FFI 的 Rust0.49/Python1.44 版本错配）。逐位比较 Rust 输出与参考实现。
    def test_ts_corr_parity(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        y = [2.0, 1.0, 4.0, 3.0, 6.0, 5.0]
        n = 3
        _finite_close(LQ_OPS.ts_corr(x, y, n), ts_corr(x, y, n))

    def test_ts_regbeta_parity(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 3.0, 5.0, 7.0, 11.0]
        n = 3
        _finite_close(LQ_OPS.ts_regbeta(y, x, n), ts_regbeta(y, x, n))


@pytest.mark.rust
@pytest.mark.skipif(LQ_METRICS is None, reason=_NO_RUST)
class TestMetricsParity:
    def test_rank_ic_parity(self):
        f = [1.0, 5.0, 3.0, 2.0, 4.0]
        r = [0.1, 0.05, 0.3, 0.2, 0.4]
        assert rank_ic(f, r) == pytest.approx(LQ_METRICS.rank_ic(f, r), rel=1e-9)

    def test_tied_rank_ic_parity(self):
        f = [2.0, 2.0, 1.0, 3.0]
        r = [1.0, 3.0, 2.0, 4.0]
        assert rank_ic(f, r) == pytest.approx(LQ_METRICS.rank_ic(f, r), rel=1e-9)

    def test_drawdown_parity(self):
        nav = [1.0, 1.3, 0.8, 0.9, 1.1, 0.4]
        assert max_drawdown(nav) == pytest.approx(LQ_METRICS.max_drawdown(nav), rel=1e-9)


@pytest.mark.rust
@pytest.mark.skipif(LQ_BACKTEST is None, reason=_NO_RUST)
class TestBacktestParity:
    def test_match_order_parity(self):
        args = ("510300.SH", True, 1000.0, 4.0, 0.00025, 5.0, 0.0, 0.0, 100.0)
        q1, f1 = ref_match_order(*args)
        q2, f2 = LQ_BACKTEST.match_order_py(*args)
        assert q1 == pytest.approx(q2, rel=1e-9)
        assert f1 == pytest.approx(f2, rel=1e-9)

    def test_match_order_sell_tax_parity(self):
        args = ("000001.SZ", False, 500.0, 12.0, 0.0003, 5.0, 0.00001, 0.0005, 100.0)
        q1, f1 = ref_match_order(*args)
        q2, f2 = LQ_BACKTEST.match_order_py(*args)
        assert q1 == pytest.approx(q2, rel=1e-9)
        assert f1 == pytest.approx(f2, rel=1e-9)