"""选股策略库：一策略一纯函数 + evidence。入口见 ``registry``。"""
from lquant.portfolio.strategies import rules  # noqa: F401  触发策略注册
from lquant.portfolio.strategies.registry import (  # noqa: F401
    STRATEGIES,
    StrategyResult,
    run_all,
    run_strategy,
)
