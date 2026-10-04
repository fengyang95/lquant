"""可 fit 的特征处理器（借 qlib ``DataHandlerLP`` 的 fit/infer 纪律）。

**为什么必须有这一层**：lquant 的**截面**预处理（``factors.preprocess``）是因果的
（逐日 ``by=trade_date``），本身不泄漏。缺的是**跨截面、需要全局统计量的变换**：
z-score 的均值/标准差、去极值截断的上下界、PCA 的载荷…… 这些一旦在
「全样本」上拟合，测试段的分布信息就进了训练 —— 模型指标虚高，上线崩塌。

qlib 的解法是 ``DataHandlerLP`` 的三组处理器：

- ``shared``   ：与窗口无关的（如 Copy）
- ``infer``    ：推理时也要用、且参数**固定**的（如已存好的 scaler）
- ``learn``    ：在 ``fit_start~fit_end`` 上 **fit 一次**，之后只 transform

本模块把这套语义落成最小实现：

    proc = StandardizeProcessor().fit(train, features=["f1", "f2"])
    train_p = proc.transform(train)     # 训练段
    test_p  = proc.transform(test)      # 只 transform，绝不再 fit

**纪律由接口形状保证**：``transform`` 只用 ``fit`` 阶段记下的状态；
想泄漏必须显式写 ``fit(all_data)`` —— 那在代码评审里一眼可见。

配套 ``leakage_guard``：把「把 apply 段并进 fit 窗口」与「只在 train 上 fit」
两次结果对比，能**检测**到泄漏。测试用它把纪律钉死。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from lquant.core.errors import LeakageError

__all__ = [
    "LeakageError",
    "Processor",
    "StandardizeProcessor",
    "ClipProcessor",
    "CrossSectionalProcessor",
    "Pipeline",
    "ProcessorSpec",
    "make_processor",
    "leakage_guard",
    "LeakageReport",
]

#: 与 factors.preprocess 的零方差口径保持一致（见 winsorize/standardize 的
#: zero_variance 元数据）：σ≈0 时用 1.0 兜底，结果恒为 0，不产生 null/inf。
_EPS = 1e-12


class Processor(ABC):
    """两段式特征处理器：``fit`` 只吃训练段，``transform`` 可作用于任意段。"""

    name = "base"

    def __init__(self) -> None:
        self.features: list[str] = []
        self._fitted = False
        self.state_: dict[str, Any] = {}

    # ---- 契约 ----

    @abstractmethod
    def _fit_state(self, train: pl.DataFrame) -> dict[str, Any]:
        """从**训练段**算出状态（均值/标准差/截断界…）。子类只实现这个。"""

    @abstractmethod
    def _apply(self, df: pl.DataFrame, state: dict[str, Any]) -> pl.DataFrame:
        """按已拟合状态变换数据。**不得**读取 df 的全局统计量。"""

    # ---- 模板方法（子类不要覆盖）----

    def fit(self, train: pl.DataFrame, *, features: list[str] | None = None) -> Processor:
        if features is not None:
            self.features = list(features)
        if not self.features:
            raise ValueError(f"{self.name}: fit 前必须给出 features")
        missing = [f for f in self.features if f not in train.columns]
        if missing:
            raise KeyError(f"{self.name}: 特征列不存在: {missing}")
        if len(train) == 0:
            raise ValueError(f"{self.name}: 训练段为空，无法 fit（空拟合 = 静默无变换）")
        self.state_ = self._fit_state(train)
        self.state_["features"] = list(self.features)
        self._fitted = True
        return self

    def transform(self, df: pl.DataFrame) -> pl.DataFrame:
        if not self._fitted:
            raise RuntimeError(f"{self.name}: 未 fit 就 transform（会静默用全样本统计量）")
        return self._apply(df, self.state_)

    def fit_transform(self, train: pl.DataFrame, *,
                      features: list[str] | None = None) -> pl.DataFrame:
        return self.fit(train, features=features).transform(train)

    # ---- 序列化（随模型 artifact 一起走）----

    def state(self) -> dict[str, Any]:
        """可 JSON 序列化的拟合状态（模型注册表/回放用）。"""
        return {"name": self.name, **self.state_}

    def load_state(self, state: dict[str, Any]) -> Processor:
        self.state_ = {k: v for k, v in state.items() if k != "name"}
        self.features = list(self.state_.get("features", []))
        self._fitted = True
        return self

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "features": list(self.features),
                "fitted": self._fitted}


def _col_list(series: list[str]) -> str:
    return ", ".join(series)


class StandardizeProcessor(Processor):
    """按**训练段**的逐特征均值/标准差做 z-score。

    线性模型 / 神经网络必须有它：量纲差 3 个数量级的特征会让梯度被单列主导，
    等价于那列独占了全部权重。

    零方差口径与 ``factors.preprocess.zscore`` 一致：``std<=1e-12`` 时用 1.0
    兜底 → 该特征恒为 0（不是 null）。
    """

    name = "standardize"

    def _fit_state(self, train: pl.DataFrame) -> dict[str, Any]:
        agg = train.select([
            pl.col(f).cast(pl.Float64, strict=False).mean().alias(f"mean:{f}")
            for f in self.features
        ] + [
            pl.col(f).cast(pl.Float64, strict=False).std().alias(f"std:{f}")
            for f in self.features
        ]).row(0, named=True)
        mean = [float(agg[f"mean:{f}"] or 0.0) for f in self.features]
        std = [float(agg[f"std:{f}"] or 0.0) for f in self.features]
        std = [s if (np.isfinite(s) and s > _EPS) else 1.0 for s in std]
        return {"mean": mean, "std": std, "n_fit": len(train)}

    def _apply(self, df: pl.DataFrame, state: dict[str, Any]) -> pl.DataFrame:
        exprs = []
        for f, mu, sd in zip(self.features, state["mean"], state["std"], strict=True):
            exprs.append(
                pl.when(pl.col(f).is_null())
                .then(None)
                .otherwise((pl.col(f).cast(pl.Float64, strict=False) - mu) / sd)
                .alias(f)
            )
        return df.with_columns(exprs)


class ClipProcessor(Processor):
    """按**训练段**的分位数拟合截断上下界，再截断任意段。

    与 ``StandardizeProcessor`` 的区别：截断是**非仿射**的，因此截面 RankIC
    对「界拟合在全样本」是敏感的 —— 这正是最容易被忽视的泄漏形态：
    测试段的极端值把上界撑大，测试样本因此免于截断，指标看起来更好。
    """

    name = "clip"

    def __init__(self, *, lower: float = 0.01, upper: float = 0.99) -> None:
        super().__init__()
        self.lower = lower
        self.upper = upper

    def _fit_state(self, train: pl.DataFrame) -> dict[str, Any]:
        lo, hi = [], []
        for f in self.features:
            q = train.select([
                pl.col(f).cast(pl.Float64, strict=False).quantile(self.lower).alias("lo"),
                pl.col(f).cast(pl.Float64, strict=False).quantile(self.upper).alias("hi"),
            ]).row(0, named=True)
            lo.append(None if q["lo"] is None else float(q["lo"]))
            hi.append(None if q["hi"] is None else float(q["hi"]))
        return {"lo": lo, "hi": hi, "lower": self.lower, "upper": self.upper,
                "n_fit": len(train)}

    def _apply(self, df: pl.DataFrame, state: dict[str, Any]) -> pl.DataFrame:
        exprs = []
        for f, lo, hi in zip(self.features, state["lo"], state["hi"], strict=True):
            if lo is None or hi is None:
                continue
            exprs.append(pl.col(f).cast(pl.Float64, strict=False).clip(lo, hi).alias(f))
        return df.with_columns(exprs) if exprs else df


class CrossSectionalProcessor(Processor):
    """薄封装 ``factors.preprocess.run``（**逐日截面**，本身因果）。

    它存在的意义是让「截面预处理」也走同一套 fit/transform 接口，
    从而可以和其他处理器串成一条 ``Pipeline``。因为分组列是 ``trade_date``，
    fit 阶段不吸收任何跨期统计量 —— ``_fit_state`` 是空字典，
    泄漏检测会正确报告 ``fit_uses_data=False``。
    """

    name = "cross_sectional"

    def __init__(self, steps: list[dict] | None = None, *,
                 by: str = "trade_date") -> None:
        super().__init__()
        self.steps = steps
        self.by = by

    def _fit_state(self, train: pl.DataFrame) -> dict[str, Any]:
        return {"by": self.by, "steps": self.steps}

    def _apply(self, df: pl.DataFrame, state: dict[str, Any]) -> pl.DataFrame:
        from lquant.factors.preprocess.pipeline import run as pipeline_run

        return pipeline_run(df, self.features, state.get("steps"),
                            by=state.get("by", "trade_date"))


class Pipeline(Processor):
    """顺序串联多个处理器：``fit`` 依次 fit，``transform`` 依次 apply。

    顺序有讲究（与 ``factors.preprocess`` 同一原则）：先截断再标准化，
    否则极值会把 σ 撑大、把其余样本压扁。
    """

    name = "pipeline"

    def __init__(self, processors: list[Processor]) -> None:
        super().__init__()
        if not processors:
            raise ValueError("Pipeline 至少要有一个处理器")
        self.processors = list(processors)

    def _fit_state(self, train: pl.DataFrame) -> dict[str, Any]:
        states = []
        cur = train
        for p in self.processors:
            p.fit(cur, features=self.features)
            states.append({"name": p.name, "state": p.state_})
            cur = p.transform(cur)          # 后一个处理器看前一个的输出
        return {"steps": states, "features": list(self.features)}

    def _apply(self, df: pl.DataFrame, state: dict[str, Any]) -> pl.DataFrame:
        # 子处理器的状态已在 _fit_state 里写好（就是 self.processors 各自身上的
        # state_），这里只按顺序 transform —— 顺序即 fit 时的顺序。
        out = df
        for p in self.processors:
            out = p.transform(out)
        return out

    def load_state(self, state: dict[str, Any]) -> Pipeline:
        self.state_ = dict(state)
        self.features = list(state.get("features", []))
        for p, step in zip(self.processors, state.get("steps", []), strict=False):
            p.load_state(step["state"])
        self._fitted = True
        return self


@dataclass
class ProcessorSpec:
    """处理器声明（可写进 YAML / API 请求 / DatasetConfig）。"""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


def make_processor(spec: ProcessorSpec | dict[str, Any] | str) -> Processor:
    """按声明构造处理器。未知 kind 直接报错（别静默退化成无变换）。"""
    if isinstance(spec, str):
        spec = ProcessorSpec(spec)
    elif isinstance(spec, dict):
        spec = ProcessorSpec(kind=spec.get("kind", spec.get("name", "")),
                             params={k: v for k, v in spec.items()
                                     if k not in ("kind", "name")})
    kind = spec.kind
    params = dict(spec.params)
    if kind in ("standardize", "zscore"):
        return StandardizeProcessor(**params)
    if kind in ("clip", "winsorize"):
        return ClipProcessor(**params)
    if kind in ("cross_sectional", "section"):
        return CrossSectionalProcessor(**params)
    if kind == "pipeline":
        subs = [make_processor(s) for s in params.get("steps", [])]
        return Pipeline(subs)
    raise KeyError(f"未知处理器 {kind!r}（可选: standardize/clip/cross_sectional/pipeline）")


@dataclass
class LeakageReport:
    """泄漏哨兵报告。

    ``apply_mean_train_fit`` / ``apply_mean_all_fit`` 是解释性数字：
    只在 train 上 fit 时，apply 段的均值会保留它与 train 的漂移；
    把 apply 并进 fit 后，apply 的均值被强行拉向 0（标准化类处理器）。
    判定本身只看 ``fit_uses_data`` 与 ``transforms_differ``。
    """

    processor: str
    fit_uses_data: bool              # fit(train) 与 fit(train+apply) 状态是否不同
    transforms_differ: bool          # 两种 fit 下 apply 的变换结果是否不同
    apply_mean_train_fit: float      # 只在 train 上 fit 时 apply 段的均值
    apply_mean_all_fit: float        # 在 train+apply 上 fit 时 apply 段的均值
    leak_detected: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "processor": self.processor,
            "fit_uses_data": self.fit_uses_data,
            "transforms_differ": self.transforms_differ,
            "apply_mean_train_fit": self.apply_mean_train_fit,
            "apply_mean_all_fit": self.apply_mean_all_fit,
            "leak_detected": self.leak_detected,
            "note": self.note,
        }


def leakage_guard(processor: Processor, train: pl.DataFrame, apply: pl.DataFrame,
                  *, features: list[str] | None = None, tol: float = 1e-9) -> LeakageReport:
    """检测「把 apply 段并进 fit 窗口」是否会改变 apply 的变换结果。

    判定逻辑：

    1. ``fit_uses_data``：``fit(train)`` 与 ``fit(train+apply)`` 的状态是否不同。
       无状态处理器（如逐日截面）天然为 False —— 不泄漏。
    2. ``transforms_differ``：两种 fit 下，**apply 段的变换结果**是否不同。
       这才是泄漏的定义式：一旦调用方把测试段并进 fit 窗口，测试特征就会变。
    3. ``leak_detected = fit_uses_data and transforms_differ``。

    注意这是**数据相关的**检测：状态不同但恰好对该 apply 数据不产生差异
    （``transforms_differ=False``）就不算泄漏 —— 这是正确行为，
    而不是漏检。

    这不是「证明没泄漏」的充分条件，而是**发现泄漏的必要哨兵** ——
    一个无状态处理器永远不泄漏；一个有状态的处理器若报告 leak_detected，
    说明调用方一旦把测试段并进 fit 窗口，测试指标就会被污染。
    """
    feats = features or processor.features
    if not feats:
        raise ValueError("leakage_guard 需要 features（或先设置 processor.features）")

    def _fresh() -> Processor:
        # 复制一份同型处理器，避免污染调用方实例
        import copy

        return copy.deepcopy(processor)

    p_train = _fresh().fit(train, features=feats)
    out_train_fit = p_train.transform(apply)

    both = pl.concat([train.select(feats), apply.select(feats)], how="vertical")
    p_all = _fresh().fit(both, features=feats)
    out_all_fit = p_all.transform(apply)

    fit_uses_data = _state_signature(p_train.state_) != _state_signature(p_all.state_)
    differs = not _frames_close(out_train_fit, out_all_fit, feats, tol)

    return LeakageReport(
        processor=processor.name,
        fit_uses_data=bool(fit_uses_data),
        transforms_differ=bool(differs),
        apply_mean_train_fit=_mean(out_train_fit, feats),
        apply_mean_all_fit=_mean(out_all_fit, feats),
        leak_detected=bool(fit_uses_data and differs),
        note=("有状态处理器：把 apply 并进 fit 会改变 apply 的变换结果，"
              "必须只在 train 上 fit" if (fit_uses_data and differs) else
              "未检测到窗口泄漏（无状态，或状态差异不影响该段数据）"),
    )


def assert_fit_isolated(processor: Processor, train: pl.DataFrame,
                        apply: pl.DataFrame, *, features: list[str] | None = None) -> LeakageReport:
    """严格模式：要求处理器**必须**对 fit 窗口敏感（有状态），否则报错。

    「无状态」在需要标准化的场景里等于**没做变换**，是另一种 bug
    （静默无效）。研究流程里宁可直接炸，也不要跑出一个假的「已标准化」。
    """
    rep = leakage_guard(processor, train, apply, features=features)
    if not rep.fit_uses_data:
        raise LeakageError(
            f"{processor.name}: 处理器对 fit 窗口不敏感（无状态）—— "
            "要么选错处理器，要么它其实什么都没做")
    return rep


# ---------------------------------------------------------------- 内部工具

def _state_signature(state: dict[str, Any]) -> str:
    """状态的可比较签名（浮点转定点，避免 repr 差异）。"""
    import json

    def _norm(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 12)
        if isinstance(v, list):
            return [_norm(x) for x in v]
        if isinstance(v, dict):
            return {k: _norm(x) for k, x in sorted(v.items())}
        return v

    return json.dumps(_norm({k: v for k, v in state.items() if k != "n_fit"}),
                      sort_keys=True, default=str)


def _mean(df: pl.DataFrame, feats: list[str]) -> float:
    """特征矩阵的逐特征均值的平均 —— 衡量整体位置漂移（可正可负）。"""
    vals: list[float] = []
    for f in feats:
        if f not in df.columns:
            continue
        m = df[f].cast(pl.Float64, strict=False).mean()
        if m is not None and np.isfinite(m):
            vals.append(float(m))
    return sum(vals) / len(vals) if vals else 0.0


def _frames_close(a: pl.DataFrame, b: pl.DataFrame, feats: list[str], tol: float) -> bool:
    """逐特征比较两帧是否数值一致。"""
    for f in feats:
        if f not in a.columns or f not in b.columns:
            return False
        va = a[f].cast(pl.Float64, strict=False).to_numpy()
        vb = b[f].cast(pl.Float64, strict=False).to_numpy()
        if va.shape != vb.shape:
            return False
        na, nb = np.isnan(va), np.isnan(vb)
        if not np.array_equal(na, nb):
            return False
        if not np.allclose(va[~na], vb[~nb], atol=tol, rtol=0):
            return False
    return True
