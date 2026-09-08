"""模型信号 → 组合 → 回测的串联。

这里也负责计算模型层面的评价指标（IC、分组收益），
因为「模型好不好」和「策略赚不赚钱」是两件事：
IC 高但换手爆炸的模型，扣完成本可能还不如等权。
两个都要看。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from lquant.backtest.engine import Engine, EngineConfig
from lquant.factors.evaluate.ic import ic_summary
from lquant.research.ml.dataset import Dataset
from lquant.research.ml.model import Model, make_model

__all__ = ["MLResult", "train_and_predict", "signal_backtest", "run_ml_pipeline"]


@dataclass
class MLResult:
    model: Model
    signal_col: str
    train_rows: int
    test_rows: int
    test_ic: dict
    predictions: pl.DataFrame | None = None

    def summary(self) -> dict:
        return {
            "model": self.model.name,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "test_ic_mean": self.test_ic.get("ic", {}).get("mean"),
            "test_rank_ic_mean": self.test_ic.get("rank_ic", {}).get("mean"),
            "test_ir": self.test_ic.get("ic", {}).get("ir"),
            "test_t_stat": self.test_ic.get("ic", {}).get("t_stat"),
        }


def train_and_predict(ds: Dataset, train_end, valid_end, *, kind: str = "auto",
                      signal_col: str = "ml_signal", **params) -> MLResult:
    """按日期切分训练，输出测试集的预测信号。"""
    train, _, test = ds.split(train_end, valid_end)
    if not len(train) or not len(test):
        raise ValueError("训练集或测试集为空，检查切分日期")

    Xtr, ytr, _ = ds.xy(train)
    Xte, yte, dte = ds.xy(test)
    model = make_model(kind, **params)
    model.feature_names = ds.features
    model.fit(Xtr, ytr)

    pred = model.predict(Xte)
    out = test.with_columns(pl.Series(signal_col, pred))

    # 用预测值直接算 IC（预测 vs 真实前瞻收益）
    ic = ic_summary(out, signal_col, ds.cfg.label_col(), date_col=ds.cfg.date_col)
    ic.pop("series", None)
    return MLResult(model=model, signal_col=signal_col, train_rows=len(train),
                    test_rows=len(test), test_ic=ic, predictions=out)


def signal_backtest(ds: Dataset, predictions: pl.DataFrame, *,
                    strategy_cls, signal_col: str = "ml_signal",
                    top_n: int = 30, engine_cfg: EngineConfig | None = None,
                    extra_fields: list[str] | None = None, **strategy_params) -> dict:
    """把模型信号喂给回测引擎。

    predictions 必须包含行情列（open/high/low/close/pre_close），
    因为要在 T+1 开盘撮合。
    """
    cols = {"open", "high", "low", "close", "pre_close", ds.cfg.date_col, ds.cfg.symbol_col}
    miss = cols - set(predictions.columns)
    if miss:
        raise KeyError(f"预测结果缺少行情列: {sorted(miss)}")

    strategy = strategy_cls(factor=signal_col, top_n=top_n, **strategy_params)
    eng = Engine(strategy, config=engine_cfg or EngineConfig(rebalance="weekly"))
    fields = (extra_fields or []) + [signal_col]
    res = eng.run(predictions, date_col=ds.cfg.date_col,
                  symbol_col=ds.cfg.symbol_col, extra_fields=fields)
    return {"result": res, "metrics": res.metrics}


def run_ml_pipeline(df: pl.DataFrame, features: list[str], *,
                    label_horizon: int = 5, train_end, valid_end,
                    kind: str = "auto", top_n: int = 30,
                    strategy_cls=None, engine_cfg: EngineConfig | None = None,
                    record: bool = True,
                    **model_params) -> dict:
    """一站式：建数据集 → 训练 → 预测 → 回测（R-ML5 实验记录落 ml_run 表）。

    默认策略用因子 TopN；未安装任何 ML 后端时会明确报错而不是静默跳过。
    record=False 可关掉落库（快速试验）。
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt

    from lquant.research.ml.dataset import DatasetConfig, build_dataset

    if strategy_cls is None:
        from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
        strategy_cls = FactorTopNStrategy

    cfg = DatasetConfig(features=features, label_horizon=label_horizon)
    ds = build_dataset(df, cfg)
    ml = train_and_predict(ds, train_end, valid_end, kind=kind, **model_params)
    bt = signal_backtest(ds, ml.predictions, strategy_cls=strategy_cls, top_n=top_n,
                         engine_cfg=engine_cfg)
    out = {
        "dataset": ds.summary(),
        "ml": ml.summary(),
        "backtest": {k: v for k, v in bt["metrics"].items() if k != "turnover"},
        "result": bt["result"],
    }

    if record:
        try:
            from lquant.core.db import writer

            run_id = _uuid.uuid4().hex[:12]
            with writer() as con:
                con.execute(
                    "INSERT OR REPLACE INTO ml_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, ml.model.name,
                     _json.dumps(model_params, default=str),
                     _json.dumps(features),
                     _json.dumps({"ml": ml.summary(),
                                  "backtest": out["backtest"]}, default=str),
                     ml.train_rows, ml.test_rows,
                     str(train_end), str(valid_end), _dt.now()],
                )
            out["ml_run_id"] = run_id
        except Exception as e:  # noqa: BLE001 - 实验记录失败不阻断研究主流程
            print(f"[warn] ml_run 记录失败: {e}")
    return out
