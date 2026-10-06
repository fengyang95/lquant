"""个股分析能力：输入一个代码，从多个角度给出可解释的分析。

与相邻模块的边界：

- :mod:`lquant.fundamental` —— **全市场**基本面排名（行业相对分位）。本包复用其
  分位口径与指标目录，但面向**单票**，且把技术/估值/资金/相对强度/风险一起端出来。
- :mod:`lquant.factors` —— 因子层面（横截面排序 / IC）。本包不做因子检验。
- :mod:`lquant.indicators` —— 技术指标**唯一实现源**，本包直接复用，不另写一份。
- :mod:`lquant.server.api.analyses` —— 那是「自定义分析脚本库」（回测资金曲线分析），
  与本包的「个股分析」是两个不同概念。

对外：

    >>> from lquant.security import analyze_security
    >>> report = analyze_security("600519.SH")     # doctest: +SKIP
    >>> report["score"]["grade"]                    # doctest: +SKIP
    '偏多'
"""
from __future__ import annotations

from lquant.security.contract import (
    ANGLES,
    SCHEMA_VERSION,
    AngleSpec,
    grade,
)
from lquant.security.loader import (
    BENCHMARK_CANDIDATES,
    CANONICAL_FINANCIAL,
    MarketData,
    load_all,
    resolve_asof,
)
from lquant.security.service import DISCLAIMER, analyze_security

__all__ = [
    "ANGLES",
    "BENCHMARK_CANDIDATES",
    "CANONICAL_FINANCIAL",
    "DISCLAIMER",
    "SCHEMA_VERSION",
    "AngleSpec",
    "MarketData",
    "analyze_security",
    "grade",
    "load_all",
    "resolve_asof",
]
