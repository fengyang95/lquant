"""可 fit 处理器与 fit/infer 纪律（Phase 1.4）。

借 qlib ``DataHandlerLP`` 的 learn/infer 纪律：**只在训练段 fit，valid/test 只
transform**。本文件把纪律钉死在三层：

1. **接口层**：``transform`` 未 fit 直接抛错；状态只来自 fit 段。
2. **哨兵层**：``leakage_guard`` 能把「把 apply 段并进 fit 窗口」检测出来。
3. **指标层**：构造一个真实的协变量漂移场景（测试段均值 ≠ 训练段），
   证明「把测试段并进 fit 窗口」会让测试 R² **变好** —— 即泄漏真的会
   制造虚高指标，而哨兵能发现它。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import LeakageError
from lquant.research.ml.dataset import Dataset, DatasetConfig
from lquant.research.ml.processor import (
    ClipProcessor,
    CrossSectionalProcessor,
    Pipeline,
    ProcessorSpec,
    StandardizeProcessor,
    assert_fit_isolated,
    leakage_guard,
    make_processor,
)


def _frame(values: dict[str, list[float]], day: date = date(2026, 1, 5)) -> pl.DataFrame:
    n = len(next(iter(values.values())))
    return pl.DataFrame({"trade_date": [day] * n, **values})


@pytest.fixture
def train_test_split():
    """训练段 N(0,1)，测试段 N(5,1) —— 明确的协变量漂移。"""
    rng = np.random.default_rng(42)
    train = pl.DataFrame({
        "trade_date": [date(2026, 1, 5)] * 200,
        "f1": rng.normal(0.0, 1.0, 200),
        "f2": rng.normal(0.0, 1.0, 200),
    })
    test = pl.DataFrame({
        "trade_date": [date(2026, 2, 5)] * 200,
        "f1": rng.normal(5.0, 1.0, 200),
        "f2": rng.normal(5.0, 1.0, 200),
    })
    return train, test


# ----------------------------------------------------------- 基础契约

def test_transform_before_fit_raises():
    p = StandardizeProcessor()
    with pytest.raises(RuntimeError, match="未 fit"):
        p.transform(_frame({"f1": [1.0, 2.0]}))


def test_fit_requires_features_and_nonempty_train():
    p = StandardizeProcessor()
    with pytest.raises(ValueError, match="features"):
        p.fit(_frame({"f1": [1.0]}))
    with pytest.raises(KeyError, match="特征列不存在"):
        p.fit(_frame({"f1": [1.0]}), features=["nope"])
    with pytest.raises(ValueError, match="训练段为空"):
        p.fit(_frame({"f1": []}), features=["f1"])


def test_standardize_uses_train_statistics_only(train_test_split):
    """测试段必须用**训练段**的均值/标准差，不是它自己的。"""
    train, test = train_test_split
    p = StandardizeProcessor().fit(train, features=["f1"])
    tr = p.transform(train)
    te = p.transform(test)

    assert abs(float(tr["f1"].mean())) < 1e-9
    assert abs(float(tr["f1"].std()) - 1.0) < 1e-6
    # 手工核验：用训练段的 mean/std 变换测试段（而不是测试段自己的统计量）
    mu = float(train["f1"].mean())
    sd = float(train["f1"].std())
    expect = (test["f1"] - mu) / sd
    assert np.allclose(te["f1"].to_numpy(), expect.to_numpy(), atol=1e-12)
    # 测试段均值 ≈ (5-mu)/sd，量级 5~6：漂移被**如实保留**（不掩盖）
    assert float(te["f1"].mean()) > 4.0


def test_standardize_preserves_nulls_and_zero_variance():
    """null 保持 null（不补 0 掩盖缺失）；训练段零方差 → 只中心化、不缩放。

    零方差口径与 ``factors.preprocess._safe_std`` 一致：σ≤1e-12 时用 1.0 兜底，
    而不是把整列抹成 null —— 否则 apply 段的真实取值会被静默丢失。
    """
    train = pl.DataFrame({"trade_date": [date(2026, 1, 5)] * 3, "f1": [1.0, 1.0, 1.0]})
    apply = pl.DataFrame({"trade_date": [date(2026, 1, 5)] * 3,
                          "f1": [None, 1.0, 2.0]})
    p = StandardizeProcessor().fit(train, features=["f1"])
    assert p.state_["std"][0] == 1.0          # 兜底值，不是 0
    out = p.transform(apply)
    assert out["f1"].null_count() == 1        # null 仍是 null
    assert out["f1"][1] == 0.0                # (1-1)/1
    assert out["f1"][2] == 1.0                # (2-1)/1 —— 不缩放但保留差异


def test_clip_bounds_come_from_train_only(train_test_split):
    train, test = train_test_split
    p = ClipProcessor(lower=0.05, upper=0.95).fit(train, features=["f1"])
    lo = p.state_["lo"][0]
    hi = p.state_["hi"][0]
    assert lo == pytest.approx(float(train["f1"].quantile(0.05)), abs=1e-9)
    assert hi == pytest.approx(float(train["f1"].quantile(0.95)), abs=1e-9)
    out = p.transform(test)
    assert float(out["f1"].max()) == pytest.approx(hi)   # 测试段 5 被夹回训练界


def test_state_roundtrip_reproduces_transform(train_test_split):
    """状态可序列化 → 推理时复用同一份参数，不重算。"""
    train, test = train_test_split
    p = StandardizeProcessor().fit(train, features=["f1"])
    blob = p.state()

    q = StandardizeProcessor().load_state(blob)
    assert np.allclose(q.transform(test)["f1"].to_numpy(),
                       p.transform(test)["f1"].to_numpy(), atol=1e-15)
    assert q.features == ["f1"]


def test_pipeline_applies_in_order_and_fits_sequentially(train_test_split):
    train, test = train_test_split
    pipe = Pipeline([ClipProcessor(lower=0.05, upper=0.95),
                     StandardizeProcessor()])
    pipe.fit(train, features=["f1"])
    out = pipe.transform(test)
    assert abs(float(pipe.transform(train)["f1"].mean())) < 1e-9
    # 先截断再标准化：标准化后的值不可能超出训练段截断界的标准化区间
    hi_bound = pipe.processors[0].state_["hi"][0]
    mu = pipe.processors[1].state_["mean"][0]
    sd = pipe.processors[1].state_["std"][0]
    assert float(out["f1"].max()) == pytest.approx((hi_bound - mu) / sd, abs=1e-9)


def test_make_processor_rejects_unknown_kind():
    assert isinstance(make_processor("standardize"), StandardizeProcessor)
    assert isinstance(make_processor({"kind": "clip", "lower": 0.02}), ClipProcessor)
    with pytest.raises(KeyError, match="未知处理器"):
        make_processor("svm")


def test_cross_sectional_processor_is_stateless(train_test_split):
    """逐日截面处理不吸收跨期统计量 → 哨兵应报告「无状态」。"""
    train, test = train_test_split
    p = CrossSectionalProcessor([{"op": "standardize", "method": "zscore"}])
    rep = leakage_guard(p, train, test, features=["f1"])
    assert rep.fit_uses_data is False
    assert rep.leak_detected is False


# ----------------------------------------------------------- 哨兵

def test_guard_detects_standardize_leak(train_test_split):
    train, test = train_test_split
    rep = leakage_guard(StandardizeProcessor(), train, test, features=["f1", "f2"])
    assert rep.fit_uses_data is True
    assert rep.transforms_differ is True
    assert rep.leak_detected is True
    assert "只在 train 上 fit" in rep.note
    # 只在 train 上 fit 时测试段保留漂移；并进 fit 后被拉向 0
    assert abs(rep.apply_mean_train_fit) > abs(rep.apply_mean_all_fit)


def test_guard_detects_clip_leak(train_test_split):
    train, test = train_test_split
    rep = leakage_guard(ClipProcessor(), train, test, features=["f1"])
    assert rep.fit_uses_data is True
    # 截断界被测试段撑大 → 测试值本该被夹住却放行
    assert rep.transforms_differ is True
    assert rep.leak_detected is True


def test_guard_reports_no_leak_when_processor_has_no_state():
    train = pl.DataFrame({"trade_date": [date(2026, 1, 5)] * 5, "f1": [1.0, 2, 3, 4, 5]})
    test = pl.DataFrame({"trade_date": [date(2026, 2, 5)] * 5, "f1": [1.0, 2, 3, 4, 5]})

    rep = leakage_guard(CrossSectionalProcessor([]), train, test, features=["f1"])
    assert rep.fit_uses_data is False and rep.leak_detected is False
    assert set(rep.as_dict()) == {
        "processor", "fit_uses_data", "transforms_differ",
        "apply_mean_train_fit", "apply_mean_all_fit", "leak_detected", "note"}


def test_assert_fit_isolated_rejects_stateless_processor():
    """需要标准化的场景里，「无状态」= 静默没做变换，也必须报错。"""
    train = pl.DataFrame({"trade_date": [date(2026, 1, 5)] * 5, "f1": [1.0, 2, 3, 4, 5]})
    test = pl.DataFrame({"trade_date": [date(2026, 2, 5)] * 5, "f1": [2.0, 3, 4, 5, 6]})
    with pytest.raises(LeakageError, match="对 fit 窗口不敏感"):
        assert_fit_isolated(CrossSectionalProcessor([]), train, test, features=["f1"])


# ----------------------------------------------------------- 指标层：泄漏确实制造虚高

def _shifted_days(n_train: int = 25, n_test: int = 25, n_sym: int = 30,
                  shift: float = 5.0, seed: int = 9) -> pl.DataFrame:
    """训练段 f ~ N(0,1)；测试段 f ~ N(shift,1)。

    标签定义为**当日的截面 z-score × 2**（A 股最常见的标签口径）：
    所以「把测试段并进 fit 窗口」会让标准化后的测试特征恰好对齐当日截面，
    模型误差骤降 —— 这就是泄漏制造虚高指标的机制。
    """
    rng = np.random.default_rng(seed)
    rows = []
    d = date(2026, 1, 5)
    days = 0
    while days < n_train + n_test:
        if d.weekday() < 5:
            mu = 0.0 if days < n_train else shift
            f = rng.normal(mu, 1.0, n_sym)
            y = 2.0 * (f - f.mean()) + rng.normal(0, 0.01, n_sym)
            for i in range(n_sym):
                rows.append({"trade_date": d, "symbol": f"S{i:03d}",
                             "f1": float(f[i]), "fwd_ret_1": float(y[i])})
            days += 1
        d += timedelta(days=1)
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def _r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def _fit_predict_r2(train_df: pl.DataFrame, test_df: pl.DataFrame,
                    *, fit_on_all: bool) -> float:
    """走完「标准化 → 预测」得到测试段 R²。

    预测器就是 ``y_hat = 2 × 标准化后的特征``（标定到标签量级），
    这样指标只反映**预处理口径**的差异 —— 本用例要验证的正是这一步。

    fit_on_all=False → 诚实做法：只在训练段 fit 标准化器。
    fit_on_all=True  → 泄漏做法：把测试段并进 fit 窗口。
    """
    proc = StandardizeProcessor()
    feats = ["f1"]
    if fit_on_all:
        proc.fit(pl.concat([train_df.select(feats), test_df.select(feats)],
                           how="vertical"), features=feats)
    else:
        proc.fit(train_df, features=feats)
    pred = 2.0 * proc.transform(test_df)["f1"].to_numpy()
    return _r2(test_df["fwd_ret_1"].to_numpy(), pred)


def test_leaking_test_into_fit_inflates_test_metric_and_guard_catches_it():
    """验收标准：把测试段并入 fit 窗口 → 测试指标变好，且哨兵能检测到。"""
    df = _shifted_days()
    dates = sorted(df["trade_date"].unique().to_list())
    split = dates[24]
    train = df.filter(pl.col("trade_date") <= split)
    test = df.filter(pl.col("trade_date") > split)

    r2_honest = _fit_predict_r2(train, test, fit_on_all=False)
    r2_leaky = _fit_predict_r2(train, test, fit_on_all=True)
    # 泄漏让测试 R² 明显变好 —— 这正是它危险的地方
    assert r2_leaky > r2_honest + 0.05, (r2_honest, r2_leaky)

    # 哨兵在不看标签、不训练模型的情况下就能发现这个风险
    rep = leakage_guard(StandardizeProcessor(), train, test, features=["f1"])
    assert rep.leak_detected is True


# ----------------------------------------------------------- Dataset 接线

def test_dataset_fit_processor_fits_on_train_only():
    """``fit_processor`` 的状态必须等于「只在 train 上 fit」，不等于全量 fit。"""
    df = _shifted_days(n_train=20, n_test=20)
    ds = Dataset(
        df=df,
        cfg=DatasetConfig(features=["f1"], label_horizon=1,
                          processors=[{"kind": "standardize"}]),
        dates=sorted(df["trade_date"].unique().to_list()),
    )
    train = ds.slice()          # 全部也行：本测试只关心 fit 的口径
    first_day = ds.dates[10]
    train_part = ds.slice(end=first_day)

    proc, _ = ds.fit_processor(train_part)
    assert proc is not None
    # 只在 train 上 fit 的均值 = train 段均值；不等于全量均值
    assert proc.state_["mean"][0] == pytest.approx(
        float(train_part["f1"].mean()), abs=1e-12)
    assert proc.state_["mean"][0] != pytest.approx(float(train["f1"].mean()), abs=1e-6)


def test_dataset_split_processed_applies_same_state_to_all_parts():
    df = _shifted_days(n_train=20, n_test=20)
    dates = sorted(df["trade_date"].unique().to_list())
    ds = Dataset(
        df=df,
        cfg=DatasetConfig(features=["f1"], label_horizon=1,
                          processors=[ProcessorSpec("standardize")]),
        dates=dates,
    )
    tr, va, te, proc = ds.split_processed(dates[14], dates[19])
    assert proc is not None and proc.name == "standardize"
    # 三段都用同一份 state：train 标准化后均值≈0，test 保留漂移
    assert abs(float(tr["f1"].mean())) < 1e-9
    assert float(te["f1"].mean()) > 1.0
    # 标签列不能被处理器动过
    assert "fwd_ret_1" in te.columns
    assert np.allclose(te["fwd_ret_1"].to_numpy(),
                       ds.slice(start=dates[20])["fwd_ret_1"].to_numpy(), atol=0)


def test_train_and_predict_records_processor_and_fit_window():
    """训练结果要带上处理器与 fit 窗口 —— Phase 2 模型注册表要靠它回放。"""
    from lquant.research.ml.backtest import train_and_predict

    df = _shifted_days(n_train=20, n_test=20)
    dates = sorted(df["trade_date"].unique().to_list())
    ds = Dataset(
        df=df,
        cfg=DatasetConfig(features=["f1"], label_horizon=1,
                          processors=[{"kind": "standardize"}]),
        dates=dates,
    )
    ml = train_and_predict(ds, dates[14], dates[17], kind="ridge")
    assert ml.processor is not None
    assert ml.fit_window is not None
    assert ml.fit_window["train_rows"] > 0
    s = ml.summary()
    assert s["processor"] == "standardize"
    assert s["processor_state"]["mean"]        # 状态随结果落库
    assert s["fit_window"]["train_end"] == str(dates[14])


def test_train_and_predict_without_processors_keeps_raw_features():
    """未声明处理器时不静默做全样本标准化（树模型的默认口径）。"""
    from lquant.research.ml.backtest import train_and_predict

    df = _shifted_days(n_train=20, n_test=20)
    dates = sorted(df["trade_date"].unique().to_list())
    ds = Dataset(df=df, cfg=DatasetConfig(features=["f1"], label_horizon=1),
                 dates=dates)
    ml = train_and_predict(ds, dates[14], dates[17], kind="ridge")
    assert ml.processor is None
    assert "processor" not in ml.summary()
    # 特征未被缩放：信号仍保留原始量级（预测值是 f 的线性函数）
    assert float(ml.predictions["ml_signal"].std()) > 0
