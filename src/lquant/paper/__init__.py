"""模拟盘：从回测走向实盘的中间态。

引擎（PaperEngine/PaperBroker）：事件驱动撮合，涨跌停拒单、T+N 可卖、费率复用 RuleSet。
告警（compare_nav/compare_trades）：模拟盘回放 vs 回测对拍，偏差大即执行层有 bug。

正确的上线顺序是：因子有效 → 回测达标 → 模拟盘对拍 ok → 才谈实盘。
"""
from __future__ import annotations

from lquant.paper import reconcile, service, store  # noqa: F401
from lquant.paper.alert import DeviationReport, compare_nav, compare_trades
from lquant.paper.engine import PaperBroker, PaperConfig, PaperEngine, PaperOrder, PaperPosition

__all__ = ["PaperEngine", "PaperBroker", "PaperConfig", "PaperOrder", "PaperPosition",
           "compare_nav", "compare_trades", "DeviationReport",
           "reconcile", "service", "store"]
