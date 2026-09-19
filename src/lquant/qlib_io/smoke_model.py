#!/usr/bin/env python
"""qlib 冒烟模型：sklearn Ridge（不依赖 lightgbm/libomp）。

qlib.contrib.model 包的 __init__ 会连带 import lightgbm（macOS 上缺
libomp 直接炸），导致 contrib 里所有模型都不可用。冒烟链路验证用本文件：
init_instance_by_config 的 module_path 支持文件路径，绕开 contrib 包。

    model:
        class: SmokeRidge
        module_path: src/lquant/qlib_io/smoke_model.py
        kwargs: {alpha: 1.0}
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from qlib.model.base import Model


class SmokeRidge(Model):
    """最小 qlib Model 实现：fit(dataset) / predict(dataset, segment)。"""

    def __init__(self, alpha: float = 1.0, **kwargs) -> None:
        self.alpha = alpha
        self.kwargs = kwargs
        self._model = None

    def fit(self, dataset, reweighter=None) -> SmokeRidge:
        from sklearn.linear_model import Ridge

        df = dataset.prepare("train", col_set=["feature", "label"], data_key="learn")
        df = df.dropna()
        x, y = df["feature"].values, df["label"].values.ravel()
        self._model = Ridge(alpha=self.alpha, **self.kwargs)
        self._model.fit(x, y)
        return self

    def predict(self, dataset, segment: str = "test") -> pd.Series:
        if self._model is None:
            raise ValueError("SmokeRidge 未训练，先 fit")
        x = dataset.prepare(segment, col_set="feature", data_key="infer")
        # Ridge 不接受 NaN（新股缺历史等）；这些行预测置 NaN，交给下游 IC/回测跳过
        ok = x.notna().all(axis=1)
        pred = pd.Series(np.nan, index=x.index)
        if ok.any():
            pred.loc[ok] = self._model.predict(x.loc[ok].values)
        return pred.sort_index()
