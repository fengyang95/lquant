"""行业分析能力：输入一个行业，从多个角度给出可解释的分析。

与 :mod:`lquant.security`（个股分析）**成对**：那边回答「这只票怎么样」，
这边回答「这个行业怎么样、在全行业里排第几」。两者共用
:mod:`lquant.core.report` 的报告契约原语（评分映射 / 指标构造 / 分位口径 /
JSON 安全）与综合分聚合算法，所以在同一页面上看到的「0–100 分」是同一把尺子。

与相邻模块的边界：

- :mod:`lquant.fundamental` —— **全市场**个股基本面排名（行业相对分位）。
  本包把行业当成分析对象，聚合到行业层面；个股层面的分位仍由那边负责。
- :mod:`lquant.factors` —— 因子层面（横截面排序 / IC / 行业中性化）。
  本包不做因子检验，也不产出可用于回测的因子列。
- :mod:`lquant.market` —— 板块**实时行情**采集（东财行业/概念/地域板块）。
  那是分钟级的看板快照，本包是 PIT 安全的日频分析。
- :mod:`lquant.server.api.analyses` —— 「自定义分析脚本库」（回测资金曲线分析），
  与本包的「行业分析」是两个不同概念。

对外：

    >>> from lquant.industry import analyze_industry          # doctest: +SKIP
    >>> report = analyze_industry("银行")                      # doctest: +SKIP
    >>> report["score"]["grade"]                               # doctest: +SKIP
    '强势'
"""
from __future__ import annotations

from lquant.industry.contract import (
    ANGLES,
    SCHEMA_VERSION,
    AngleSpec,
    grade,
)
from lquant.industry.loader import (
    BENCHMARK_CANDIDATES,
    IndustryUniverse,
    build_universe,
    clear_industry_cache,
    list_industries,
    resolve_asof,
    resolve_industry,
)
from lquant.industry.service import (
    DISCLAIMER,
    analyze_industry,
    industry_rotation,
    list_industry_names,
)

__all__ = [
    "ANGLES",
    "BENCHMARK_CANDIDATES",
    "DISCLAIMER",
    "SCHEMA_VERSION",
    "AngleSpec",
    "IndustryUniverse",
    "analyze_industry",
    "build_universe",
    "clear_industry_cache",
    "grade",
    "industry_rotation",
    "list_industries",
    "list_industry_names",
    "resolve_asof",
    "resolve_industry",
]
