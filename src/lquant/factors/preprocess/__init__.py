"""预处理层：去极值 → 标准化 → 中性化 → 正交化。

导入本包即自动注册所有方法（避免「忘了 import 导致注册表为空」这类
只在运行时才暴露的问题）。

    from lquant.factors.preprocess import run
    clean = run(df, "mom_20")                      # 默认配方
    clean = run(df, ["mom_20", "vol_20"],
                [{"op": "winsorize", "method": "mad", "n": 3},
                 {"op": "standardize", "method": "rank"},
                 {"op": "orthogonalize", "method": "symmetric"}])
"""
from __future__ import annotations

from lquant.factors.preprocess import (  # noqa: F401  触发注册
    neutralize,
    orthogonalize,
    power,
    rolling,
    standardize,
    winsorize,
)
from lquant.factors.preprocess.pipeline import describe, normalize_steps, run
from lquant.factors.preprocess.registry import (
    METHODS,
    STAGES,
    default_pipeline,
    get_method,
    list_methods,
    method,
)
from lquant.factors.preprocess.winsorize import (
    MAD_K,
    from_alphapurify_n,
    to_alphapurify_n,
)

__all__ = [
    "METHODS", "STAGES", "method", "get_method", "list_methods", "default_pipeline",
    "run", "normalize_steps", "describe",
    "MAD_K", "to_alphapurify_n", "from_alphapurify_n",
    "winsorize", "standardize", "neutralize", "orthogonalize", "rolling", "power",
]
