"""research/ml/model.py 单测：接口、后端降级、持久化。"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from lquant.research.ml.model import (
    LGBMModel,
    Model,
    RidgeModel,
    SklearnModel,
    available_backends,
    make_model,
)


def _xy(n: int = 60, seed: int = 0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 3))
    y = X @ np.array([1.0, -2.0, 0.5]) + rng.normal(scale=0.01, size=n)
    return X, y


# ---------- 基类接口 ----------

def test_base_fit_predict_not_implemented():
    m = Model()
    X, y = _xy(10)
    with pytest.raises(NotImplementedError):
        m.fit(X, y)
    with pytest.raises(RuntimeError, match="未训练"):
        m.predict(X)
    # predict 前置检查独立于实现：训练后仍因未实现而抛 NotImplementedError
    m2 = Model()
    m2.model = object()
    with pytest.raises(NotImplementedError):
        m2.predict(X)


def test_finetune_defaults_to_refit():
    m = RidgeModel()
    X, y = _xy()
    m.fit(X, y)
    m2 = m.finetune(X[:10], y[:10])
    assert m2 is m and m2.predict(X).shape == (len(X),)


def test_save_load_roundtrip(tmp_path):
    m = RidgeModel(alpha=0.5)
    X, y = _xy()
    m.feature_names = ["f1", "f2", "f3"]
    m.fit(X, y)
    p = m.save(tmp_path / "sub" / "m.pkl")
    assert p.exists()
    loaded = RidgeModel.load(p)
    assert loaded.params["alpha"] == 0.5
    assert loaded.feature_names == ["f1", "f2", "f3"]
    np.testing.assert_allclose(loaded.predict(X), m.predict(X))


# ---------- 具体后端 ----------

def test_sklearn_model_fit_predict():
    m = SklearnModel(n_estimators=10)
    X, y = _xy()
    out = m.fit(X, y)
    assert out is m and m.name == "gbrt"
    pred = m.predict(X)
    assert pred.dtype == float
    corr = np.corrcoef(pred, y)[0, 1]
    assert corr > 0.9


def test_ridge_model():
    m = RidgeModel()
    X, y = _xy()
    m.fit(X, y)
    assert m.name == "ridge"
    assert np.corrcoef(m.predict(X), y)[0, 1] > 0.95


def test_lgbm_with_fake_module(monkeypatch):
    """lightgbm 缺 libomp 的机器上，用假模块驱动 LGBMModel 的 fit/predict 路径。"""
    calls: dict = {}

    class FakeReg:
        def __init__(self, **params):
            calls["params"] = params
            self.n_estimators = params.get("n_estimators")

        def fit(self, X, y, **kw):
            calls["fit_X"] = X
            self._y = y
            return self

        def predict(self, X):
            return np.full(len(X), float(np.mean(self._y)))

    fake = types.ModuleType("lightgbm")
    fake.LGBMRegressor = FakeReg
    monkeypatch.setitem(sys.modules, "lightgbm", fake)

    m = LGBMModel(n_estimators=5)
    X, y = _xy()
    assert m.fit(X, y) is m
    assert calls["params"]["n_estimators"] == 5
    assert calls["params"]["verbose"] == -1
    pred = m.predict(X)
    assert pred.dtype == float and len(pred) == len(X)


# ---------- 后端探测与工厂 ----------

def test_available_backends_and_auto():
    avail = available_backends()
    assert "gbrt" in avail and "ridge" in avail  # venv 已装 sklearn
    m = make_model("auto")
    assert isinstance(m, (LGBMModel, SklearnModel, RidgeModel))


def test_make_model_explicit_and_unknown():
    assert isinstance(make_model("ridge"), RidgeModel)
    assert isinstance(make_model("gbrt", n_estimators=5), SklearnModel)
    with pytest.raises(KeyError, match="未知模型"):
        make_model("xgboost")


def test_make_model_no_backend(monkeypatch):
    monkeypatch.setattr("lquant.research.ml.model.available_backends", lambda: [])
    with pytest.raises(RuntimeError, match="无任何可用"):
        make_model("auto")
