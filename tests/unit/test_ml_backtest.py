"""research/ml/backtest.py 单测：训练预测、信号回测、一站式管线。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.research.ml.backtest import (
    MLResult,
    signal_backtest,
    train_and_predict,
)
from lquant.research.ml.dataset import DatasetConfig, build_dataset

SYMS = [f"{600000 + i}.SS" for i in range(12)]


def make_ohlcv(n_days: int = 60, seed: int = 11) -> pl.DataFrame:
    """12 只标的 × n_days 的 OHLC 长表，动量特征与未来收益正相关。"""
    rng = np.random.default_rng(seed)
    rows = []
    for s in SYMS:
        px = 10.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n_days)))
        mom = np.zeros(n_days)
        mom[5:] = px[5:] / px[:-5] - 1.0
        for i in range(n_days):
            d = date(2024, 1, 1) + timedelta(days=i)
            rows.append({
                "trade_date": d, "symbol": s,
                "open": px[i] * (1 + rng.normal(0, 0.002)),
                "high": px[i] * 1.01, "low": px[i] * 0.99,
                "close": px[i], "pre_close": px[i - 1] if i else px[i],
                "volume": 1e6, "mom": mom[i],
            })
    return pl.DataFrame(rows)


def make_ds(df: pl.DataFrame):
    return build_dataset(df, DatasetConfig(features=["mom"], label_horizon=5))


# ---------- train_and_predict ----------

def test_train_and_predict_ridge():
    ml = train_and_predict(make_ds(make_ohlcv()), date(2024, 1, 31),
                           date(2024, 2, 10), kind="ridge", signal_col="sig")
    assert ml.model.name == "ridge"
    assert ml.signal_col == "sig"
    assert ml.train_rows > 0 and ml.test_rows > 0
    assert "sig" in ml.predictions.columns
    s = ml.summary()
    assert s["model"] == "ridge"
    assert s["train_rows"] == ml.train_rows
    assert s["test_rows"] == ml.test_rows
    assert "test_ic_mean" in s and "test_ir" in s


def test_train_and_predict_empty_split_raises():
    ds = make_ds(make_ohlcv(n_days=30))
    with pytest.raises(ValueError, match="训练集或测试集为空"):
        train_and_predict(ds, date(2025, 1, 1), date(2025, 2, 1), kind="ridge")


# ---------- signal_backtest ----------

def test_signal_backtest_missing_columns():
    ds = make_ds(make_ohlcv())
    preds = ds.df.select(["trade_date", "symbol", "close"])
    with pytest.raises(KeyError, match="缺少行情列"):
        signal_backtest(ds, preds, strategy_cls=object)


def test_signal_backtest_runs_with_stub_strategy():
    from lquant.backtest.engine import EngineConfig
    from lquant.backtest.strategy.base import Strategy

    df = make_ohlcv()
    ds = make_ds(df)

    class StubStrategy(Strategy):
        """恒选前 3 只：只验证信号能进引擎并出结果。"""

        def __init__(self, factor: str = "sig", top_n: int = 3) -> None:
            super().__init__(factor=factor, top_n=top_n)

        def on_bar(self, ctx, bars):
            syms = sorted(bars)[:3]
            return [(s, 1.0 / 3) for s in syms]

    preds = ds.df.with_columns(pl.lit(1.0).alias("sig"))
    out = signal_backtest(ds, preds, strategy_cls=StubStrategy, signal_col="sig",
                          top_n=3, engine_cfg=EngineConfig(rebalance="weekly"),
                          extra_fields=["mom"])
    assert "result" in out and "metrics" in out
    assert len(out["result"].nav) > 0


# ---------- run_ml_pipeline ----------

def test_run_ml_pipeline_record_false(monkeypatch):
    from lquant.research.ml.backtest import run_ml_pipeline

    calls: dict = {}

    class FakeWriterCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_writer():
        calls["entered"] = True
        raise AssertionError("record=False 不应落库")

    monkeypatch.setattr("lquant.core.db.writer", fake_writer)
    out = run_ml_pipeline(make_ohlcv(), ["mom"], label_horizon=5,
                          train_end=date(2024, 1, 31), valid_end=date(2024, 2, 10),
                          kind="ridge", top_n=3, record=False)
    assert "ml_run_id" not in out
    assert out["dataset"]["symbols"] == 12
    assert out["ml"]["model"] == "ridge"
    assert "backtest" in out and "result" in out
    assert "turnover" not in out["backtest"]


def test_run_ml_pipeline_record_registers_version(tmp_path, monkeypatch):
    """record=True：注册进模型注册表 —— 拿 run_id、版本号、artifact 可定位。

    这条链路是 Phase 2.1 的核心：训练记录必须能回放到模型文件，
    而不是「有一行指标但不知道模型在哪」。
    """
    import json as _json

    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    monkeypatch.setenv("LQ_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.chdir(tmp_path)     # 让相对路径的 duckdb 落在 tmp 而不是工作区
    # 回测引擎要读 config/rules/cn_a_share.yaml（相对 root 解析）——
    # 不复制的话本测试只能靠 load_yaml 的 lru_cache 余温通过，单独跑必挂。
    import shutil as _shutil
    from pathlib import Path as _Path

    _shutil.copytree(_Path(__file__).resolve().parents[2] / "config",
                     tmp_path / "config", dirs_exist_ok=True)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import reader, writer
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_ml_run_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_ml_run_columns(con)   # 老库增列迁移（新库是空操作）

    from lquant.research.ml.backtest import run_ml_pipeline

    out = run_ml_pipeline(make_ohlcv(), ["mom"], label_horizon=5,
                          train_end=date(2024, 1, 31), valid_end=date(2024, 2, 10),
                          kind="ridge", top_n=3, record=True,
                          model_name="unit_line",
                          processors=[{"kind": "standardize"}])
    assert out["ml_run_id"]
    mv = out["model"]
    assert mv["name"] == "unit_line" and mv["version"] == 1
    assert mv["stage"] == "candidate"
    assert (tmp_path / mv["artifact_path"]).exists()
    assert (tmp_path / mv["processor_path"]).exists()

    with reader() as con:
        row = con.execute(
            "SELECT features, model_name, model_version, artifact_path"
            " FROM ml_run WHERE run_id = ?", [out["ml_run_id"]]).fetchone()
    assert _json.loads(row[0]) == ["mom"]
    assert (row[1], row[2]) == ("unit_line", 1)
    assert row[3] == mv["artifact_path"]

    # 第二次训练同一 model_name → 版本递增（同一模型线的版本流）
    out2 = run_ml_pipeline(make_ohlcv(), ["mom"], label_horizon=5,
                           train_end=date(2024, 1, 31), valid_end=date(2024, 2, 10),
                           kind="ridge", top_n=3, record=True,
                           model_name="unit_line",
                           processors=[{"kind": "standardize"}])
    assert out2["model"]["version"] == 2

    # 注册失败：不阻断主流程（研究脚本不该因为库坏了跑不完）
    def bad_register(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("lquant.research.ml.registry.register_run", bad_register)
    out3 = run_ml_pipeline(make_ohlcv(), ["mom"], label_horizon=5,
                           train_end=date(2024, 1, 31), valid_end=date(2024, 2, 10),
                           kind="ridge", top_n=3, record=True)
    assert "ml_run_id" not in out3
    get_settings.cache_clear()


def test_mlresult_summary_empty_ic():
    class DummyModel:
        name = "dummy"

    r = MLResult(model=DummyModel(), signal_col="s", train_rows=1, test_rows=1,
                 test_ic={})
    assert r.summary()["test_ic_mean"] is None
    assert r.summary()["test_t_stat"] is None
