"""技术指标层（单一实现源）。

设计要点：

1. **注册表驱动**：新增指标只写一个带 ``@register_indicator`` 的函数，
   ``GET /api/data/indicators`` 与前端可自动枚举，不改上层。
2. **单一实现源**：``lquant.factors.indicators`` 只是本包的兼容转发，
   历史上「同一个 XMA 有两份实现、一份用未来数据一份不用」的事故不会再发生。
3. **防未来函数**：``lquant.indicators.future`` 提供前缀不变性判据，
   接入新指标时先跑 ``assert_no_lookahead``。
4. **形态类不塞进因子 DSL**：DSL 的算子必须是可静态推导 ``min_window`` 的
   窗口化纯函数，表达不了中枢/浪型这类分段结构。指标以「预计算信号列」
   的形式反哺因子层。
"""
from __future__ import annotations

from lquant.indicators import momentum, tiandao, trend, volume  # noqa: F401  触发注册
from lquant.indicators.composite import WARMUP, add_all
from lquant.indicators.future import (
    LookaheadViolation,
    assert_no_lookahead,
    check_prefix_invariance,
)
from lquant.indicators.momentum import add_kdj, add_rsi
from lquant.indicators.registry import (
    CATEGORIES,
    INDICATORS,
    PANES,
    compute,
    compute_many,
    min_window,
    outputs,
    register_indicator,
    required_history,
)
from lquant.indicators.tiandao import (
    TIANDAO_DEFAULT_N,
    add_tiandao,
    xma_half_window,
    xma_truncated,
)
from lquant.indicators.trend import add_bbi, add_boll, add_ema, add_ma, add_macd
from lquant.indicators.volume import add_turnover_ma, add_volume_ratio, add_volume_surge

__all__ = [
    "CATEGORIES",
    "INDICATORS",
    "PANES",
    "TIANDAO_DEFAULT_N",
    "LookaheadViolation",
    "WARMUP",
    "add_all",
    "add_bbi",
    "add_boll",
    "add_ema",
    "add_kdj",
    "add_ma",
    "add_macd",
    "add_rsi",
    "add_tiandao",
    "add_turnover_ma",
    "add_volume_ratio",
    "add_volume_surge",
    "assert_no_lookahead",
    "check_prefix_invariance",
    "compute",
    "compute_many",
    "min_window",
    "outputs",
    "register_indicator",
    "required_history",
    "xma_half_window",
    "xma_truncated",
]
