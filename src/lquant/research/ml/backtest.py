"""模型信号 → 组合 → 回测的串联。

这里也负责计算模型层面的评价指标（IC、分组收益），
因为「模型好不好」和「策略赚不赚钱」是两件事：
IC 高但换手爆炸的模型，扣完成本可能还不如等权。
两个都要看。
"""
from __future__ import annotations

from dataclasses import dataclass

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
    #: 特征处理器（None = 未声明处理器）。随 artifact 一起存，推理时必须复用
    #: 同一份状态 —— 用测试段重算标准化参数就是泄漏。
    processor: object | None = None
    fit_window: dict | None = None

    def summary(self) -> dict:
        out = {
            "model": self.model.name,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "test_ic_mean": self.test_ic.get("ic", {}).get("mean"),
            "test_rank_ic_mean": self.test_ic.get("rank_ic", {}).get("mean"),
            "test_ir": self.test_ic.get("ic", {}).get("ir"),
            "test_t_stat": self.test_ic.get("ic", {}).get("t_stat"),
        }
        if self.processor is not None:
            out["processor"] = self.processor.name
            out["processor_state"] = self.processor.state()
        if hasattr(self.model, "summary"):
            out["ensemble"] = self.model.summary()
        if self.fit_window:
            out["fit_window"] = dict(self.fit_window)
        return out


def train_and_predict(ds: Dataset, train_end, valid_end, *, test_end=None,
                      window: dict | None = None,
                      kind: str = "auto", n_seeds: int = 1,
                      signal_col: str = "ml_signal", **params) -> MLResult:
    """按日期切分训练，输出测试集的预测信号。

    特征处理器（``DatasetConfig.processors``）**只在训练段 fit**，valid/test
    只 transform —— 这是 qlib ``DataHandlerLP`` 的 learn/infer 纪律：
    处理器参数是训练窗口的函数，测试段的分布信息绝不参与拟合。

    ``test_end``：测试段右端点。**滚动重训必须传** —— 不传时测试段一直取到
    数据末端，早期窗口的"样本外"指标会把后面所有窗口的数据都算进来，
    越早的窗口看起来越好（未来信息泄漏进评估）。

    ``window``：``walk_forward_splits(..., purge_bars/embargo_bars)`` 返回的
    带缺口区间。给了它就走 :meth:`Dataset.split_window`（缺口如实跳过），
    否则退回连续边界切分。滚动重训带了 purge/embargo 时必须给 —— 用连续的
    ``split`` 重建会把隔离带并回训练段，purge 白做。

    ``n_seeds > 1`` 时训 N 个种子取均值（``EnsembleModel``），
    压低单次训练对随机种子的敏感性；代价是训练时间 ×N。
    """
    if window is not None:
        train_raw, _, test_raw = ds.split_window(window)
        if test_end is None:
            test_end = window["test"][1]
    else:
        train_raw, _, test_raw = ds.split(train_end, valid_end, test_end)
    if not len(train_raw) or not len(test_raw):
        raise ValueError("训练集或测试集为空，检查切分日期")

    proc, train = ds.fit_processor(train_raw)
    test = proc.transform(test_raw) if proc is not None else test_raw

    Xtr, ytr, _ = ds.xy(train)
    Xte, yte, dte = ds.xy(test)
    model = make_model(kind, n_seeds=n_seeds, **params)
    model.feature_names = ds.features
    model.fit(Xtr, ytr)

    pred = model.predict(Xte)
    out = test.with_columns(pl.Series(signal_col, pred))

    # 用预测值直接算 IC（预测 vs 真实前瞻收益）
    ic = ic_summary(out, signal_col, ds.cfg.label_col(), date_col=ds.cfg.date_col)
    ic.pop("series", None)
    fit_window = {
        "train_end": str(train_end), "valid_end": str(valid_end),
        "test_end": str(test_end) if test_end else None,
        "train_rows": len(train), "test_rows": len(test),
        "train_start": str(train[ds.cfg.date_col].min()) if len(train) else None,
        "train_stop": str(train[ds.cfg.date_col].max()) if len(train) else None,
    }
    return MLResult(model=model, signal_col=signal_col, train_rows=len(train),
                    test_rows=len(test), test_ic=ic, predictions=out,
                    processor=proc, fit_window=fit_window)


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
                    processors: list[dict] | None = None,
                    n_seeds: int = 1,
                    model_name: str | None = None,
                    stage: str = "candidate",
                    note: str | None = None,
                    **model_params) -> dict:
    """一站式：建数据集 → 训练 → 预测 → 回测 → 注册模型版本。

    ``record=True`` 时通过 ``research.ml.registry`` 注册：写 ``ml_run`` 记录、
    落模型与处理器 artifact、分配单调版本号。默认策略用因子 TopN；
    未安装任何 ML 后端时会明确报错而不是静默跳过。

    ``processors``：可 fit 的特征处理器声明（``research.ml.processor``）。
    未填时**不做任何全样本标准化** —— 树模型不需要，线性/神经网络必须显式声明，
    且一律只在训练段 fit。

    ``model_name``：逻辑模型线（同一策略反复重训共享一个 name，版本号递增）。
    缺省按「horizon + top_n」拼一个稳定名字，使同一配置的多次训练聚成版本流。
    """
    import uuid as _uuid

    from lquant.research.ml.dataset import DatasetConfig, build_dataset
    from lquant.research.ml.panel import build_feature_panel

    # features 可以是**原始湖表**里的列，也可以是待计算的内置因子/公式名
    # （MA20、pct_change_20…）。不先算一遍的话，API/CLI 把裸日线递进来时会
    # 直接 KeyError「特征列不存在」—— 而 /ml/features 恰恰在向用户宣传这些名字。
    # build_feature_panel 对已存在的列是幂等透传，预featurize 的调用方不受影响。
    df = build_feature_panel(df, features)

    if strategy_cls is None:
        from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
        strategy_cls = FactorTopNStrategy

    cfg = DatasetConfig(features=features, label_horizon=label_horizon,
                        processors=processors)
    ds = build_dataset(df, cfg)
    ml = train_and_predict(ds, train_end, valid_end, kind=kind,
                           n_seeds=n_seeds, **model_params)
    bt = signal_backtest(ds, ml.predictions, strategy_cls=strategy_cls, top_n=top_n,
                         engine_cfg=engine_cfg)
    out = {
        "dataset": ds.summary(),
        "ml": ml.summary(),
        "backtest": {k: v for k, v in bt["metrics"].items() if k != "turnover"},
        "result": bt["result"],
    }

    if record:
        run_id = _uuid.uuid4().hex[:12]
        name = model_name or f"ml_h{label_horizon}_top{top_n}"
        try:
            from lquant.research.ml.registry import register_run

            mv = register_run(
                run_id=run_id, name=name, model=ml.model, processor=ml.processor,
                metrics={"ml": ml.summary(), "backtest": out["backtest"]},
                params=model_params, features=features,
                fit_window=ml.fit_window, dataset=ds.summary(),
                train_rows=ml.train_rows, test_rows=ml.test_rows,
                train_end=train_end, test_end=valid_end,
                stage=stage, note=note,
            )
            out["ml_run_id"] = run_id
            out["model"] = mv.as_dict()
        except Exception as e:  # noqa: BLE001 - 注册失败不阻断研究主流程
            print(f"[warn] 模型注册失败: {e}")
    return out
