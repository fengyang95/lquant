"""参数扫描（sweep）：逐档改 strategy 参数跑回测，返回统一指标表。

设计文档 API 契约里的 `POST /api/backtests/sweep` 是异步队列；本模块是
**纯计算核心**：输入一块行情 DataFrame + 扫描规格，输出 {value: 指标} 网格。

引擎只在意输出表的列集稳定（前端画参数-收益/回撤曲面就靠这几列），
扫描什么参数、取哪些值全由调用方决定，这里不做策略语义假设。
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from lquant.backtest.engine import Engine, EngineConfig
from lquant.server.jobs import JobCanceled

# 固定输出列：前端契约，改列名/加列都要同步更新前端
_OUT_COLS = ["value", "total_return", "annual_return", "sharpe",
             "max_drawdown", "n_trades", "turnover"]


@dataclass
class SweepSpec:
    """扫描公共段：除被扫参数外，其余回测参数固定在这里。"""

    factor: str                                   # 因子列名（需已在 data 里）
    rebalance: str = "daily"
    initial_cash: float = 1_000_000.0
    max_position_weight: float = 1.0


def _metrics_res(metrics: dict, value) -> dict:
    m = metrics
    # turnover 是 {n_trades, total_amount, turnover_per_period} 结构 ——
    # 但这里是「参数-收益/回撤曲面」的固定列契约，必须展平成单标量。
    tov = m.get("turnover") or {}
    return {
        "value": value,
        "total_return": m.get("total_return"),
        "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe"),
        "max_drawdown": m.get("max_drawdown"),
        "n_trades": m.get("n_trades", 0),
        "turnover": tov.get("turnover_per_period") if isinstance(tov, dict) else m.get("turnover"),
    }


def run_sweep(data: pl.DataFrame, param: str, values: list,
              spec: SweepSpec, *, strategy_cls=None,
              strategy_kwargs: dict | None = None,
              cancel_check=None) -> pl.DataFrame:
    """对 `param` 在 `values` 上逐档回测，返回网格表。

    `strategy_cls` 构造签名需接受 `{**strategy_kwargs, param: value}`；
    默认是 factor_topn（支持 factor= 与 top_n=）。
    cancel_check：每档开始前轮询，返回 True 时抛 JobCanceled 协作式收尾。
    """
    from lquant.backtest.security_meta import load_security_meta
    from lquant.backtest.strategy.factor_topn import FactorTopNStrategy

    cls = strategy_cls or FactorTopNStrategy
    base = dict(strategy_kwargs or {})
    # security 元数据只读一次，复用给每档 —— 否则 N 档就是 N 次全表查询
    meta = load_security_meta()

    rows = []
    for v in values:
        if cancel_check is not None and cancel_check():
            raise JobCanceled(f"参数扫描在第 {len(rows)}/{len(values)} 档被取消")
        kw = {**base, param: v}          # 未知参数会在构造时 KeyError → 快失败
        strat = cls(factor=spec.factor, **kw)
        res = Engine(strat, config=EngineConfig(
            initial_cash=spec.initial_cash, rebalance=spec.rebalance,
            max_position_weight=spec.max_position_weight,
        ), meta=meta, with_db_meta=False).run(data, extra_fields=[spec.factor])
        rows.append(_metrics_res(res.metrics, v))

    # 固定列序（_metrics_res 的键序即 _OUT_COLS）；values 非空时 rows 至少一行
    return pl.DataFrame(rows, orient="row").select(*_OUT_COLS)