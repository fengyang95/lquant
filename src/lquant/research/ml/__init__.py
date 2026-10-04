"""ML 选股：借 qlib 的 Model 三段式接口，不引入其运行时。

设计取舍见 ADR-11：qlib 的 Model 抽象（fit / predict / finetune）很干净，
但它的 DataHandler 与自家二进制数据格式强耦合，照搬代价太高。
所以只借接口，数据在 Polars 长表里流转。

    from lquant.research.ml import run_ml_pipeline
    out = run_ml_pipeline(df, features=["mom20", "vol20", "bp"],
                          label_horizon=5, train_end="2025-06-30",
                          valid_end="2025-12-31", top_n=20)
"""
from __future__ import annotations

from lquant.research.ml.backtest import (
    MLResult,
    run_ml_pipeline,
    signal_backtest,
    train_and_predict,
)
from lquant.research.ml.dataset import (
    Dataset,
    DatasetConfig,
    build_dataset,
    walk_forward_splits,
)
from lquant.research.ml.model import (
    LGBMModel,
    Model,
    RidgeModel,
    SklearnModel,
    available_backends,
    make_model,
)
from lquant.research.ml.processor import (
    ClipProcessor,
    CrossSectionalProcessor,
    LeakageError,
    LeakageReport,
    Pipeline,
    Processor,
    ProcessorSpec,
    StandardizeProcessor,
    assert_fit_isolated,
    leakage_guard,
    make_processor,
)

__all__ = [
    "Dataset", "DatasetConfig", "build_dataset", "walk_forward_splits",
    "Model", "LGBMModel", "SklearnModel", "RidgeModel", "make_model", "available_backends",
    "train_and_predict", "signal_backtest", "run_ml_pipeline", "MLResult",
    "Processor", "StandardizeProcessor", "ClipProcessor", "CrossSectionalProcessor",
    "Pipeline", "ProcessorSpec", "make_processor",
    "leakage_guard", "assert_fit_isolated", "LeakageReport", "LeakageError",
]
