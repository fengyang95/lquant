"""ETF 因子回测：把 ETF 池 + 因子列喂进原生 ``Engine``。

**规则侧零改动** —— ``Engine.run`` 内部 ``build_rules`` 会按 ``parse_symbol().sec_type``
给 ETF 自动挂 ``config/rules/cn_a_share.yaml`` 的 ``etf:`` 段：免印花税、免过户费、
佣金 rate 0.0001/min 0.1、price_tick 0.001、涨跌停 ``by_track_index``、``t_plus``
按基金类型（qdii/commodity/bond/money = T+0）。所以本模块**不碰**任何规则配置，
只负责四件事：

1. 把数据限制到 ETF（复用 ``factors.etf.build_etf_panel``，顺带挂上 etf_meta 列，
   让 ``fund_size`` 这类 ETF 专属因子也能回测）；
2. 把 ``etf_meta`` 的 per-instrument 属性（``fund_type`` / ``sellable_after_days``）
   注入 ``Engine(meta=...)``，比「按名称关键字猜 fund_type」更准；
3. 用 ``factors.analysis.compute_factor_col`` 算因子列 —— **与因子评价链路同一实现**，
   避免「同一公式在评价能算、在回测算不出」这种口径分叉；
4. 跑 ``FactorTopNStrategy``（ETF 池上等权持有因子最高 N 只）。

与 ``server/api/backtests.py:run_backtest`` 的关系：后者是通用入口（可带 universe），
本模块是 ETF 专用、可被 CLI 与 API 复用的薄封装（不落库；落库由调用方做）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["etf_factor_backtest", "load_etf_meta_map"]


def load_etf_meta_map() -> dict[str, dict]:
    """``{symbol: {fund_type, sellable_after_days, name}}`` —— 从 ``etf_meta`` 读。

    任何异常都退化为空字典（与 ``security_meta.load_security_meta`` 同约定）：
    合成数据 / 无库沙箱下回测要照常跑，缺 etf_meta 时 ``RuleSet.for_symbol``
    仍会按名称关键字推断 fund_type 兜底。
    """
    try:
        from lquant.data.store.catalog import EtfMetaRepo

        df = EtfMetaRepo().all()
    except Exception:  # noqa: BLE001 - 元数据缺失不致命
        return {}
    if df is None or not len(df) or "symbol" not in df.columns:
        return {}
    out: dict[str, dict] = {}
    for r in df.iter_rows(named=True):
        m: dict[str, Any] = {}
        if r.get("fund_type"):
            m["fund_type"] = str(r["fund_type"])
        days = r.get("sellable_after_days")
        if days is not None:
            m["sellable_after_days"] = int(days)
        if r.get("name"):
            m["name"] = str(r["name"])
        if m:
            out[str(r["symbol"])] = m
    return out


def etf_factor_backtest(
    formula: str,
    *,
    universe: str = "etf_all",
    symbols: list[str] | None = None,
    start=None,
    end=None,
    top_n: int = 30,
    rebalance: str = "monthly",
    initial_cash: float = 1_000_000.0,
    benchmark: str | None = None,
    price_mode: str = "next_open",
    exit_strategy: str | None = None,
    exit_params: dict | None = None,
    with_db_meta: bool = True,
):
    """在 ETF 池上跑因子 TopN 回测。

    返回 ``(BacktestResult, meta)``；``meta`` 记录口径（池/降级披露/标的数/参数），
    供落库与报告复用。数据为空或因子全空 → 抛 ``ValueError``（调用方转 503/422）。
    """
    from lquant.backtest.engine import Engine, EngineConfig
    from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.etf import build_etf_panel, etf_universe_label, is_etf_universe

    # covs=[]：回测不需要中性化协变量，跳过 provider（省掉一次全池滚动计算）。
    # etf_meta 列仍会通过 build_etf_panel 挂上，规模/份额类因子照常可回测。
    panel, _cov_cols, info = build_etf_panel(
        start=start, end=end, symbols=symbols, universe=universe, covs=[]
    )
    if not len(panel):
        raise ValueError("ETF 面板为空 —— 先同步 ETF 日线与元数据")

    col = "f"
    d = compute_factor_col(panel, formula, name=col).drop_nulls([col])
    if not len(d):
        raise ValueError(
            f"ETF 因子 {formula!r} 计算后全为空（数据窗口不足，或该表达式在 ETF 字段集下无效）"
        )
    used = sorted(d["symbol"].unique().to_list())

    strategy = FactorTopNStrategy(factor=col, top_n=top_n)
    if exit_strategy:
        from lquant.backtest.exit import ExitOverlay, get_exit_strategy

        strategy = ExitOverlay(
            strategy, get_exit_strategy(exit_strategy, **(exit_params or {}))
        )

    cfg = EngineConfig(
        initial_cash=initial_cash,
        rebalance=rebalance,
        benchmark=benchmark,
        price_mode=price_mode,
    )
    res = Engine(
        strategy, config=cfg, meta=load_etf_meta_map(), with_db_meta=with_db_meta
    ).run(d, extra_fields=[col])

    meta = {
        "engine": "factor_topn",
        "formula": formula,
        "universe": universe,
        "universe_label": (
            etf_universe_label(universe) if is_etf_universe(universe) else universe
        ),
        "universe_note": info.get("note"),
        "n_symbols": len(used),
        "symbols": used,
        "top_n": top_n,
        "rebalance": rebalance,
        "start": start,
        "end": end,
        "exit_strategy": exit_strategy,
        "benchmark": benchmark,
    }
    return res, meta
