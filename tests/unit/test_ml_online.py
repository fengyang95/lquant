"""特征面板解析 + 滚动重训/每日推理编排（Phase 2.2/2.3）。

要守住的三件事：

1. **特征解析不静默降级**：解析不了的特征必须报错（少一个特征训练出来的
   模型照样能跑，但语义已经变了）。
2. **滚动重训的样本外窗口有界**：不传 ``test_end`` 会让早期窗口的"样本外"
   指标吃进后面所有窗口的数据 —— 越早的窗口越好看，是典型未来信息泄漏。
3. **晋级先验证再上线**：模型/处理器载不起来必须在晋级前暴露，而不是
   线上第一次推理时（那时已过收盘）。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import MLError
from lquant.research.ml.online import (
    OnlineConfig,
    daily_inference,
    load_signals,
    rolling_retrain,
    safe_promote,
    verify_version,
)
from lquant.research.ml.panel import (
    available_features,
    build_feature_panel,
    resolve_feature,
)

D0 = date(2024, 1, 1)


@pytest.fixture()
def ml_env(tmp_path, monkeypatch):
    """隔离根目录 / DuckDB / 模型目录，并建全量表结构。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    yield tmp_path
    get_settings.cache_clear()


def _months_later(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, 1)


def _panel(months: int = 30, n_sym: int = 12, seed: int = 3) -> pl.DataFrame:
    """合成日线：价格随机游走 + Alpha158 需要的全部原始列。

    两个容易踩的坑（都直接导致「数据集为空」）：

    - ``n_sym`` 必须 ≥ ``DatasetConfig.min_samples_per_day``（默认 10），
      否则 ``build_dataset`` 把每一天都当"截面样本太少"剔掉；
    - ``months`` 必须按**交易日**展开。用 ``D0 + timedelta(days=months*21)``
      生成的是日历日，30 个月会缩成 ~21 个月 —— 滚动窗口一个都切不出来。
    """
    rng = np.random.default_rng(seed)
    horizon = _months_later(D0, months)
    dates: list[date] = []
    d = D0
    while d < horizon:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    for j in range(n_sym):
        px = 10.0 + j
        for d in dates:
            pre = px
            px = max(pre * (1 + rng.normal(0.0004, 0.015)), 1.0)
            vol = float(rng.integers(1e6, 5e6))
            rows.append({"trade_date": d, "symbol": f"S{j:03d}",
                         "open": pre, "high": max(pre, px) * 1.01,
                         "low": min(pre, px) * 0.99, "close": px,
                         "pre_close": pre, "volume": vol,
                         "amount": vol * px, "turnover_rate": 1.0 + j,
                         "float_mv": 1e10 * (j + 1)})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


# ----------------------------------------------------------- 特征解析

def test_resolve_feature_passes_through_lake_column():
    df = pl.DataFrame({"trade_date": [D0], "symbol": ["S0"], "close": [10.0]})
    out = resolve_feature(df, "close")
    assert out["close"].to_list() == [10.0]


def test_resolve_feature_computes_alpha158():
    df = _panel(months=6, n_sym=2)
    out = resolve_feature(df, "MA5")
    assert "MA5" in out.columns
    assert out["MA5"].null_count() > 0        # 预热期为 null


def test_resolve_feature_computes_simple_formula():
    df = _panel(months=6, n_sym=2)
    out = resolve_feature(df, "pct_change_5")
    assert "pct_change_5" in out.columns
    manual = (df.sort(["symbol", "trade_date"])
              .with_columns(pl.col("close").pct_change(5).over("symbol").alias("m")))
    got = out.sort(["symbol", "trade_date"])
    a = got["pct_change_5"].to_numpy()
    b = manual["m"].to_numpy()
    assert np.allclose(a[~np.isnan(a)], b[~np.isnan(b)], atol=1e-12)


def test_resolve_feature_unknown_raises_with_options():
    df = _panel(months=3, n_sym=2)
    with pytest.raises(MLError, match="无法解析特征"):
        resolve_feature(df, "definitely_not_a_feature")


def test_build_feature_panel_rejects_empty():
    with pytest.raises(MLError, match="不能为空"):
        build_feature_panel(_panel(months=3, n_sym=2), [])


def test_available_features_lists_three_sources():
    df = _panel(months=3, n_sym=2)
    out = available_features(df)
    assert "close" in out["columns"]
    assert out["alpha158_count"] == 158
    assert "pct_change_20" in out["formulas"]
    # 元数据列不该被当成特征候选
    assert "trade_date" not in out["columns"] and "symbol" not in out["columns"]


# ----------------------------------------------------------- 滚动重训

def _cfg(**kw) -> OnlineConfig:
    base = dict(name="line", features=["pct_change_20"], label_horizon=5,
                kind="ridge", train_months=12, valid_months=3,
                test_months=3, step_months=3, top_n=3)
    base.update(kw)
    return OnlineConfig(**base)


def test_rolling_retrain_registers_versions_and_promotes(ml_env):
    out = rolling_retrain(_panel(months=30), _cfg(), promote=True)
    assert out["windows"] >= 2
    assert out["n_trained"] >= 2
    assert out["n_promoted"] >= 1
    versions = [v["version"] for v in out["versions"] if v["status"] == "trained"]
    assert versions == sorted(versions) and versions[0] == 1
    # 每个版本都要有 artifact
    from lquant.research.ml import registry

    for v in versions:
        mv = registry.get_model("line", v)
        assert mv is not None and mv.artifact_path


def test_rolling_retrain_test_window_is_bounded(ml_env):
    """样本外窗口必须有界：早期窗口的 test_end 必须早于数据末端。

    不传 test_end 时测试段会一直取到数据末尾 —— 越早的窗口"样本外"指标
    越好看（吃了未来数据）。这里直接断言每个窗口的 test_end 单调且不是末端。
    """
    panel = _panel(months=30)
    out = rolling_retrain(panel, _cfg(), promote=False)
    ends = [v["test_end"] for v in out["versions"] if v["status"] == "trained"]
    assert len(ends) >= 2
    assert ends == sorted(ends)
    assert ends[0] < ends[-1]
    assert ends[-1] < str(panel["trade_date"].max())


def test_rolling_retrain_without_promote_keeps_candidates(ml_env):
    from lquant.research.ml import registry

    out = rolling_retrain(_panel(months=30), _cfg(), promote=False)
    assert out["n_promoted"] == 0
    assert registry.production("line") is None
    # 候选版本必须都留在 candidate，供人工挑
    cands = registry.list_models("line", stage="candidate")
    assert len(cands) == out["n_trained"]


def test_rolling_retrain_insufficient_history_raises(ml_env):
    with pytest.raises(MLError, match="数据跨度不足"):
        rolling_retrain(_panel(months=6), _cfg(), promote=False)


def test_rolling_retrain_empty_features_raises(ml_env):
    with pytest.raises(MLError, match="features 不能为空"):
        rolling_retrain(_panel(months=30), _cfg(features=[]), promote=False)


def test_rolling_retrain_cancel_keeps_finished_versions(ml_env):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1          # 第二个窗口前取消

    out = rolling_retrain(_panel(months=30), _cfg(), promote=False,
                          cancel_check=cancel)
    assert out["n_trained"] == 1
    assert out["windows"] >= 2          # total 反映计划窗口数


# ----------------------------------------------------------- 验证 + 晋级

def test_verify_version_ok_and_reports_shape(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    ver = verify_version("line", 1, panel=panel, features=["pct_change_20"])
    assert ver["rows"] > 0
    assert np.isfinite(ver["signal_mean"]) and np.isfinite(ver["signal_std"])


def test_verify_version_detects_broken_artifact(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    (ml_env / "models" / "line" / "v1" / "model.pkl").write_bytes(b"not a pickle")
    with pytest.raises(MLError):
        verify_version("line", 1, panel=panel, features=["pct_change_20"])


def test_safe_promote_rejects_unverifiable_version(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    (ml_env / "models" / "line" / "v1" / "model.pkl").unlink()
    res = safe_promote("line", 1, panel=panel, features=["pct_change_20"],
                       metric=0.5)
    assert res["promoted"] is False
    assert "验证失败" in res["reason"]
    assert res["verification"] is None


def test_safe_promote_requires_metric(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    res = safe_promote("line", 1, panel=panel, features=["pct_change_20"],
                       metric=float("nan"))
    assert res["promoted"] is False and "不可用" in res["reason"]


def test_safe_promote_compares_against_production(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    from lquant.research.ml import registry

    registry.promote("line", 1, "production")
    # 把 v1 的指标写成很高的值，v2 无法超过 → 不晋级
    from lquant.core.db import writer

    with writer() as con:
        con.execute("UPDATE ml_model SET metrics = ? WHERE name='line' AND version=1",
                    ['{"ml": {"test_rank_ic_mean": 0.99}}'])
    res = safe_promote("line", 2, panel=panel, features=["pct_change_20"],
                       metric=0.01, min_improvement=0.0)
    assert res["promoted"] is False and "未超过线上" in res["reason"]
    assert registry.production("line").version == 1     # 线上版没被动

    # 指标足够好 → 晋级，旧的自动 archived
    res2 = safe_promote("line", 2, panel=panel, features=["pct_change_20"],
                        metric=1.5)
    assert res2["promoted"] is True
    assert registry.production("line").version == 2
    assert registry.get_model("line", 1).stage == "archived"


def test_safe_promote_first_version_goes_live(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    res = safe_promote("line", 1, panel=panel, features=["pct_change_20"],
                       metric=0.02)
    assert res["promoted"] is True and "首个线上版" in res["reason"]


# ----------------------------------------------------------- 每日推理

def test_daily_inference_persists_signals_with_version(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=True)
    cfg = _cfg()
    target = sorted(panel["trade_date"].unique().to_list())[-1]
    out = daily_inference(panel, cfg, date=target)
    assert out["rows"] > 0 and out["persisted"] == out["rows"]

    sig = load_signals("line", start=target, end=target)
    assert len(sig) == out["rows"]
    # 信号必须带 model_version —— 事后审计「这天是哪一版出的」靠它
    assert sig["model_version"].unique().to_list() == [out["version"]]


def test_daily_inference_without_production_raises(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=False)
    with pytest.raises(MLError, match="没有线上版本"):
        daily_inference(panel, _cfg(), persist=False)


def test_daily_inference_unknown_date_raises(ml_env):
    panel = _panel(months=30)
    rolling_retrain(panel, _cfg(), promote=True)
    with pytest.raises(MLError, match="无行情数据"):
        daily_inference(panel, _cfg(), date=date(1999, 1, 1), persist=False)


def test_load_signals_without_table_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    out = load_signals("nope")
    assert len(out) == 0 and "signal" in out.columns
    get_settings.cache_clear()
