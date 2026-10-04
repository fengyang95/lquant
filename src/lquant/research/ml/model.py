"""Model 接口：fit / predict / save / load。

借 qlib 的 Model 三段式接口（fit / predict / finetune），但不引入其运行时 ——
qlib 的依赖树太重，为一个接口拖进整套框架不值得。

后端优先级：LightGBM > sklearn 梯度提升 > 岭回归。
自动降级很重要：lightgbm 依赖 OpenMP 运行时，很多机器上装了包也 import 不了
（macOS 上缺 libomp 是常态），不能让整个研究流程因此卡死。
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

__all__ = ["Model", "LGBMModel", "SklearnModel", "RidgeModel", "EnsembleModel",
           "make_model", "available_backends"]


class Model:
    """统一接口。新增模型后端只需实现 fit / predict。"""

    name = "base"

    def __init__(self, **params) -> None:
        self.params = params
        self.model = None
        self.feature_names: list[str] = []

    def fit(self, X: np.ndarray, y: np.ndarray, **kw) -> Model:
        raise NotImplementedError

    def _check_ready(self) -> None:
        """子类在 predict 开头调用。检查与实现分离，避免 super().predict() 直接抛异常。"""
        if self.model is None:
            raise RuntimeError("模型未训练，请先 fit")

    def predict(self, X: np.ndarray) -> np.ndarray:
        self._check_ready()
        raise NotImplementedError

    def finetune(self, X: np.ndarray, y: np.ndarray, **kw) -> Model:
        """增量训练。默认退化为全量重训。"""
        return self.fit(X, y, **kw)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            # kind 必须落盘：Model.load 是基类 classmethod，只凭 params 无法
            # 知道该还原成哪个后端 —— 缺了它会把 RidgeModel 还原成抽象
            # Model，predict 直接 NotImplementedError（artifact 不可回放）。
            pickle.dump({"params": self.params, "features": self.feature_names,
                         "model": self.model, "kind": self.name,
                         "cls": type(self).__name__}, f)
        return p

    @classmethod
    def load(cls, path: str | Path) -> Model:
        with open(path, "rb") as f:
            blob = pickle.load(f)
        target = cls
        if cls is Model:
            # 基类调用：按落盘的 kind 还原具体后端；找不到就退化为基类
            # （至少 .name/.params 可读，而不是静默用错后端）。
            kind = blob.get("kind")
            target = _BACKENDS.get(kind) or _BACKENDS.get(blob.get("cls", ""), Model)
        obj = target(**blob.get("params", {}))
        obj.model = blob["model"]
        obj.feature_names = blob.get("features", [])
        return obj


class LGBMModel(Model):
    """LightGBM 回归。A 股截面选股的主力模型。"""

    name = "lightgbm"

    def __init__(self, **params) -> None:
        default = dict(n_estimators=300, learning_rate=0.05, num_leaves=31,
                       subsample=0.8, colsample_bytree=0.8, random_state=42,
                       n_jobs=4, verbose=-1)
        default.update(params)
        super().__init__(**default)

    def fit(self, X, y, **kw) -> LGBMModel:
        import lightgbm as lgb
        self.model = lgb.LGBMRegressor(**self.params)
        self.model.fit(X, y, **kw)
        return self

    def predict(self, X) -> np.ndarray:
        self._check_ready()
        return np.asarray(self.model.predict(X), dtype=float)


class SklearnModel(Model):
    """sklearn 梯度提升。特征量小、样本少时的稳妥选择。"""

    name = "gbrt"

    def __init__(self, **params) -> None:
        default = dict(n_estimators=200, learning_rate=0.05, max_depth=3,
                       subsample=0.8, random_state=42)
        default.update(params)
        super().__init__(**default)

    def fit(self, X, y, **kw) -> SklearnModel:
        # 用 GradientBoostingRegressor 而不是 HistGradientBoostingRegressor：
        # 后者在 sklearn 1.9 里不接受 n_estimators / subsample，参数名跨版本不稳定
        from sklearn.ensemble import GradientBoostingRegressor
        self.model = GradientBoostingRegressor(**self.params)
        self.model.fit(X, y)
        return self

    def predict(self, X) -> np.ndarray:
        self._check_ready()
        return np.asarray(self.model.predict(X), dtype=float)


class RidgeModel(Model):
    """岭回归。作为线性基准：如果 GBDT 打不过它，说明信号里没有非线性。"""

    name = "ridge"

    def __init__(self, **params) -> None:
        default = dict(alpha=1.0)
        default.update(params)
        super().__init__(**default)

    def fit(self, X, y, **kw) -> RidgeModel:
        from sklearn.linear_model import Ridge
        self.model = Ridge(**self.params)
        self.model.fit(X, y)
        return self

    def predict(self, X) -> np.ndarray:
        self._check_ready()
        return np.asarray(self.model.predict(X), dtype=float)


class EnsembleModel(Model):
    """多 seed 集成：同一后端训 N 次（只换随机种子），预测取均值。

    **为什么值得做**：GBDT 类模型的单次训练对 `random_state` 敏感 ——
    特征子采样与行采样让每次的树结构不同，IC 的波动可达 ±0.01 量级，
    足以让「A 因子比 B 因子好」这种结论翻面。取多个种子的均值把这份
    训练噪声压掉（方差降到 1/N），代价是训练时间 ×N。

    与「调参」的区别：集成**不改变**模型族的表达能力，只是降低估计方差；
    所以它不该被用来「刷高」指标 —— 单 seed 的均值与集成应当接近，
    若集成显著更好，说明单次训练严重欠拟合/不稳定，那是另一个问题。

    保存/载入：子模型列表放在 ``self.model`` 里（与单模型同一字段），
    因此 ``Model.save/load`` 无需特判 —— pickle 能直接序列化 sklearn/lgbm 对象。

    ``params`` 必须同时保留 ``base`` / ``n_seeds``：``Model.load`` 是拿
    落盘的 ``params`` 调 ``__init__`` 重建对象的，若只存子模型参数，载入后
    ``n_seeds`` 会静默退回默认值（``summary()`` 与实际训练规模不符）。
    子后端参数单独放在 ``sub_params`` 里，避免把 ``base``/``n_seeds``
    透传给 LightGBM/sklearn 造成 unknown-parameter 报错。
    """

    name = "ensemble"

    def __init__(self, base: str = "auto", n_seeds: int = 5, **params) -> None:
        if n_seeds < 2:
            raise ValueError(f"n_seeds 必须 ≥ 2（1 个种子就是单模型），收到 {n_seeds}")
        if base == "ensemble":
            raise ValueError("不支持嵌套集成，请用 base= 指定单模型后端")
        super().__init__(base=base, n_seeds=int(n_seeds), **params)
        self.base = base
        self.n_seeds = int(n_seeds)
        self.sub_params = dict(params)
        self.model = []            # list[Model]

    @property
    def models(self) -> list[Model]:
        return self.model

    def _check_ready(self) -> None:
        """基类只看 ``model is None``，集成装的是列表 —— 空列表同样是未训练，
        否则会在 predict 里抛 np.vstack 的 "need at least one array" 而不是
        给出可读的「未训练」提示。"""
        if not self.model:
            raise RuntimeError("集成模型未训练，请先 fit")

    def _seeds(self) -> list[int]:
        """确定性种子序列（不用随机数生成器，保证可复现）。"""
        return [42 + i for i in range(self.n_seeds)]

    def fit(self, X, y, **kw) -> EnsembleModel:
        self.model = []
        for seed in self._seeds():
            m = make_model(self.base, **{**self.sub_params, "random_state": seed})
            m.fit(X, y, **kw)
            self.model.append(m)
        return self

    def predict(self, X) -> np.ndarray:
        self._check_ready()
        preds = np.vstack([np.asarray(m.predict(X), dtype=float) for m in self.model])
        # 逐样本均值：单模型预测里若有 NaN，均值会跟着变 NaN —— 这是想要的
        # （静默忽略会掩盖某个种子训练失败）
        return np.nanmean(preds, axis=0)

    def predict_all(self, X) -> np.ndarray:
        """返回 (n_seeds, n_samples) 的原始预测矩阵（看种子间分歧用）。"""
        self._check_ready()
        return np.vstack([np.asarray(m.predict(X), dtype=float) for m in self.model])

    def summary(self) -> dict:
        return {"base": self.base, "n_seeds": self.n_seeds,
                "sub_models": [m.name for m in self.model]}


_BACKENDS: dict[str, type[Model]] = {
    "lightgbm": LGBMModel,
    "gbrt": SklearnModel,
    "ridge": RidgeModel,
    "ensemble": EnsembleModel,
}


def available_backends() -> list[str]:
    """探测可用后端。lightgbm 要真 import 一次才知道（动态库可能缺失）。"""
    ok = []
    try:
        import lightgbm  # noqa: F401
        ok.append("lightgbm")
    except Exception:
        pass
    try:
        from sklearn.ensemble import GradientBoostingRegressor  # noqa: F401
        ok.append("gbrt")
        ok.append("ridge")
    except Exception:
        pass
    return ok


def make_model(kind: str = "auto", *, n_seeds: int = 1, **params) -> Model:
    """构造模型。``kind="auto"`` 时按可用性挑最优后端。

    ``n_seeds > 1`` 时返回 :class:`EnsembleModel`（同后端训 N 次取均值），
    用于压低单次训练对随机种子的敏感性。``n_seeds=1``（默认）行为不变。
    """
    if n_seeds and n_seeds > 1:
        if kind == "ensemble":
            raise ValueError("kind='ensemble' 时请用 base= 指定后端，不要嵌套集成")
        return EnsembleModel(base=kind, n_seeds=n_seeds, **params)
    if kind != "auto":
        if kind not in _BACKENDS:
            raise KeyError(f"未知模型 {kind!r}，可选: {sorted(_BACKENDS)}")
        return _BACKENDS[kind](**params)
    avail = available_backends()
    for k in ("lightgbm", "gbrt", "ridge"):
        if k in avail:
            return _BACKENDS[k](**params)
    raise RuntimeError("无任何可用 ML 后端，请安装 lightgbm 或 scikit-learn")
