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
from lquant.research.ml.online import (
    OnlineConfig,
    daily_inference,
    load_signals,
    persist_signals,
    rolling_retrain,
    safe_promote,
    verify_version,
)
from lquant.research.ml.panel import (
    available_features,
    build_feature_panel,
    resolve_feature,
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
from lquant.research.ml.registry import (
    STAGES,
    ModelVersion,
    archive,
    events,
    get_model,
    list_models,
    load_model,
    load_processor,
    model_paths,
    model_root,
    production,
    production_asof,
    promote,
    register_run,
    rollback,
    save_artifact,
)

__all__ = [
    "Dataset", "DatasetConfig", "build_dataset", "walk_forward_splits",
    "Model", "LGBMModel", "SklearnModel", "RidgeModel", "make_model", "available_backends",
    "train_and_predict", "signal_backtest", "run_ml_pipeline", "MLResult",
    "Processor", "StandardizeProcessor", "ClipProcessor", "CrossSectionalProcessor",
    "Pipeline", "ProcessorSpec", "make_processor",
    "leakage_guard", "assert_fit_isolated", "LeakageReport", "LeakageError",
    # 模型注册表（Phase 2.1）
    "STAGES", "ModelVersion", "model_root", "model_paths", "save_artifact",
    "register_run", "list_models", "get_model", "load_model", "load_processor",
    "promote", "rollback", "archive", "production", "production_asof", "events",
    # 特征面板 + 在线编排（Phase 2.2/2.3）
    "resolve_feature", "build_feature_panel", "available_features",
    "OnlineConfig", "rolling_retrain", "safe_promote", "verify_version",
    "daily_inference", "persist_signals", "load_signals",
]
