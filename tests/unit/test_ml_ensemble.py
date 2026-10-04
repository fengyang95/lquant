"""research/ml 多 seed 集成（EnsembleModel）：确定性、均值语义、落盘回放。"""
from __future__ import annotations

import pickle
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.research.ml.backtest import train_and_predict
from lquant.research.ml.dataset import DatasetConfig, build_dataset
from lquant.research.ml.model import (
    EnsembleModel,
    Model,
    RidgeModel,
    SklearnModel,
    make_model,
)


def _xy(n: int = 200, seed: int = 0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 4))
    y = X @ np.array([1.0, -2.0, 0.5, 0.0]) + rng.normal(scale=0.3, size=n)
    return X, y


# ---------- 构造与参数校验 ----------

def test_n_seeds_one_keeps_plain_backend():
    """n_seeds=1 是默认路径，必须原样返回单模型（不套一层集成）。"""
    m = make_model("ridge", n_seeds=1, alpha=2.0)
    assert isinstance(m, RidgeModel)
    assert not isinstance(m, EnsembleModel)
    assert m.params["alpha"] == 2.0


def test_make_model_n_seeds_wraps_ensemble():
    m = make_model("gbrt", n_seeds=3)
    assert isinstance(m, EnsembleModel)
    assert m.base == "gbrt"
    assert m.n_seeds == 3
    assert m.models == []           # 未训练时没有子模型


def test_ensemble_rejects_single_seed():
    with pytest.raises(ValueError, match="n_seeds 必须 ≥ 2"):
        EnsembleModel(base="ridge", n_seeds=1)


def test_ensemble_rejects_nesting():
    """嵌套集成没有意义（方差压两次不改变族），要在构造期就拒绝。"""
    with pytest.raises(ValueError, match="不支持嵌套集成"):
        EnsembleModel(base="ensemble", n_seeds=3)
    with pytest.raises(ValueError, match="不要嵌套集成"):
        make_model("ensemble", n_seeds=3)


def test_predict_before_fit_raises():
    m = EnsembleModel(base="ridge", n_seeds=2)
    X, _ = _xy(10)
    with pytest.raises(RuntimeError, match="未训练"):
        m.predict(X)
    with pytest.raises(RuntimeError, match="未训练"):
        m.predict_all(X)


# ---------- 训练语义 ----------

def test_fit_creates_one_model_per_seed_with_fixed_seeds():
    m = EnsembleModel(base="ridge", n_seeds=4)
    X, y = _xy()
    m.fit(X, y)
    assert len(m.models) == 4
    assert [s.params["random_state"] for s in m.models] == [42, 43, 44, 45]
    # 子模型参数只含子后端参数：base/n_seeds 不能透传给 sklearn
    assert "base" not in m.models[0].params
    assert "n_seeds" not in m.models[0].params


def test_user_params_reach_sub_models():
    m = make_model("ridge", n_seeds=2, alpha=7.0)
    X, y = _xy()
    m.fit(X, y)
    assert all(s.params["alpha"] == 7.0 for s in m.models)


def test_predict_is_seed_mean_and_predict_all_is_raw():
    m = EnsembleModel(base="gbrt", n_seeds=3)
    X, y = _xy()
    m.fit(X, y)
    Xte, _ = _xy(37, seed=9)
    all_pred = m.predict_all(Xte)
    assert all_pred.shape == (3, 37)
    assert np.allclose(m.predict(Xte), all_pred.mean(axis=0), equal_nan=True)


def test_fit_is_deterministic():
    """种子序列写死（42+i）而不是靠全局随机数，两次训练必须逐位一致。"""
    X, y = _xy()
    Xte, _ = _xy(25, seed=5)
    a = EnsembleModel(base="gbrt", n_seeds=3).fit(X, y)
    b = EnsembleModel(base="gbrt", n_seeds=3).fit(X, y)
    d = np.abs(a.predict(Xte) - b.predict(Xte))
    assert float(np.nanmax(d)) == 0.0


def test_summary_reports_scale():
    m = EnsembleModel(base="gbrt", n_seeds=3).fit(*_xy())
    s = m.summary()
    assert s["base"] == "gbrt"
    assert s["n_seeds"] == 3
    assert s["sub_models"] == ["gbrt"] * 3


def test_ensemble_beats_worst_seed_mse():
    """集成是逐样本均值，MSE 必然 ≤ 各种子 MSE 的均值（Jensen），
    因此也不会比最差的种子更差 —— 这不是「刷指标」，只是降方差。"""
    X, y = _xy(300, seed=3)
    Xte, yte = _xy(120, seed=31)
    m = EnsembleModel(base="gbrt", n_seeds=5).fit(X, y)
    per_seed = [float(np.mean((p - yte) ** 2)) for p in m.predict_all(Xte)]
    mse = float(np.mean((m.predict(Xte) - yte) ** 2))
    assert mse <= float(np.mean(per_seed)) + 1e-12
    assert mse <= float(np.max(per_seed)) + 1e-12


# ---------- 落盘回放 ----------

def test_save_load_roundtrip_restores_seeds_and_predictions(tmp_path):
    X, y = _xy()
    Xte, _ = _xy(25, seed=7)
    m = EnsembleModel(base="gbrt", n_seeds=3).fit(X, y)

    path = m.save(tmp_path / "ens.pkl")
    loaded = Model.load(path)

    assert isinstance(loaded, EnsembleModel)
    # 关键回归点：params 里若丢掉 base/n_seeds，载入会静默退回 n_seeds=5
    assert loaded.base == "gbrt"
    assert loaded.n_seeds == 3
    assert len(loaded.models) == 3
    # 子模型还原成具体后端，而不是抽象 Model（否则 predict 抛 NotImplementedError）
    assert all(type(s) is SklearnModel for s in loaded.models)
    assert loaded.feature_names == m.feature_names
    d = np.abs(loaded.predict(Xte) - m.predict(Xte))
    assert float(np.nanmax(d)) == 0.0


def test_load_unknown_kind_falls_back_to_base(tmp_path):
    """老 artifact 缺 kind/cls 时必须退化为基类，而不是静默用错后端。"""
    path = tmp_path / "old.pkl"
    with open(path, "wb") as f:
        pickle.dump({"params": {"alpha": 1.0}, "features": ["mom"],
                     "model": object()}, f)
    loaded = Model.load(path)
    assert type(loaded) is Model
    assert loaded.params == {"alpha": 1.0}
    assert loaded.feature_names == ["mom"]
    with pytest.raises(NotImplementedError):
        loaded.predict(np.zeros((2, 1)))


# ---------- 管线集成 ----------

SYMS = [f"{600000 + i}.SS" for i in range(12)]


def _ohlcv(n_days: int = 90, seed: int = 13) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in SYMS:
        px = 10.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n_days)))
        mom = np.zeros(n_days)
        mom[5:] = px[5:] / px[:-5] - 1.0
        for i in range(n_days):
            rows.append({
                "trade_date": date(2024, 1, 1) + timedelta(days=i), "symbol": s,
                "open": px[i], "high": px[i] * 1.01, "low": px[i] * 0.99,
                "close": px[i], "pre_close": px[i - 1] if i else px[i],
                "volume": 1e6, "mom": mom[i],
            })
    return pl.DataFrame(rows)


def test_pipeline_reports_ensemble_in_summary():
    ds = build_dataset(_ohlcv(), DatasetConfig(features=["mom"], label_horizon=5))
    ml = train_and_predict(ds, date(2024, 2, 1), date(2024, 2, 20),
                           kind="ridge", n_seeds=3, signal_col="sig")
    assert ml.model.name == "ensemble"
    s = ml.summary()
    assert s["model"] == "ensemble"
    assert s["ensemble"] == {"base": "ridge", "n_seeds": 3,
                             "sub_models": ["ridge"] * 3}


def test_pipeline_single_seed_has_no_ensemble_block():
    ds = build_dataset(_ohlcv(), DatasetConfig(features=["mom"], label_horizon=5))
    ml = train_and_predict(ds, date(2024, 2, 1), date(2024, 2, 20),
                           kind="ridge", signal_col="sig")
    assert ml.model.name == "ridge"
    assert "ensemble" not in ml.summary()
