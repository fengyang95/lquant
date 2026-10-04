"""模型注册表（Phase 2.1）：训练记录 ↔ artifact ↔ 在线版本必须互相定位。

要守住的不变量：

1. **版本单调**：同一 ``name`` 下版本号从 1 起步、逐次 +1，不重号不跳号；
2. **production 至多一个**：晋级新版本时旧线上版自动 ``archived``，同事务写事件；
3. **artifact 可回放**：模型与处理器状态一起存，``load_model`` 载入后预测逐点一致
   （处理器状态若丢，推理就得用推理段统计量重算 → 泄漏）；
4. **as-of 可答**：``production_asof(时刻)`` 靠**重放事件流**回答「当时用的哪一版」，
   而不是靠当前 stage 猜。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import MLError

D0 = date(2026, 1, 5)


@pytest.fixture()
def ml_env(tmp_path, monkeypatch):
    """隔离根目录 + DuckDB + 模型目录，并建全量表结构。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
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


def _panel(n: int = 80, n_sym: int = 8, shift: float = 0.0) -> pl.DataFrame:
    rng = np.random.default_rng(5)
    rows = []
    for i in range(n):
        d = D0 + timedelta(days=i)
        f = rng.normal(shift, 1.0, n_sym)
        y = rng.normal(0, 0.01, n_sym)
        for j in range(n_sym):
            rows.append({"trade_date": d, "symbol": f"S{j:03d}",
                         "f1": float(f[j]), "fwd_ret_1": float(y[j]),
                         "close": 10.0 + j, "open": 10.0, "high": 10.0,
                         "low": 10.0, "pre_close": 10.0, "volume": 1e6,
                         "amount": 1e7})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def _dataset(processors: list[dict] | None = None):
    from lquant.research.ml.dataset import Dataset, DatasetConfig

    df = _panel()
    dates = sorted(df["trade_date"].unique().to_list())
    cfg = DatasetConfig(features=["f1"], label_horizon=1, processors=processors)
    return Dataset(df=df, cfg=cfg, dates=dates), df, dates


def _trained(processors=None, kind="ridge"):
    from lquant.research.ml.backtest import train_and_predict

    ds, _, dates = _dataset(processors)
    return train_and_predict(ds, dates[39], dates[59], kind=kind), ds, dates


# ----------------------------------------------------------- 基础注册

def test_register_run_allocates_version_and_writes_artifact(ml_env):
    from lquant.research.ml import registry

    ml, ds, _ = _trained(processors=[{"kind": "standardize"}])
    mv = registry.register_run(
        run_id="r1", name="demo", model=ml.model, processor=ml.processor,
        metrics={"ic": 0.03}, features=["f1"], fit_window=ml.fit_window,
        dataset=ds.summary(), train_rows=ml.train_rows, test_rows=ml.test_rows,
        train_end="2026-02-13", test_end="2026-03-05",
    )
    assert mv.name == "demo" and mv.version == 1 and mv.stage == "candidate"
    assert mv.artifact_path and mv.processor_path

    root = ml_env / "models" / "demo" / "v1"
    assert (root / "model.pkl").exists()
    assert (root / "processor.json").exists()
    assert (root / "meta.json").exists()
    # 落库的是**相对仓库根**的路径（换机器仍可定位）
    assert not mv.artifact_path.startswith("/")

    # ml_run 记录必须能定位到 artifact（Phase 2.1 的核心诉求）
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute(
            "SELECT model_name, model_version, stage, artifact_path, processor_state,"
            " fit_window FROM ml_run WHERE run_id='r1'").fetchone()
    assert row[0] == "demo" and row[1] == 1 and row[2] == "candidate"
    assert row[3] == mv.artifact_path
    assert json.loads(row[4])["mean"]           # 处理器状态可读
    assert json.loads(row[5])["train_rows"] > 0


def test_versions_are_monotonic_per_name(ml_env):
    from lquant.research.ml import registry

    ml, ds, _ = _trained()
    v1 = registry.register_run(run_id="a", name="line", model=ml.model,
                               metrics={}, features=["f1"])
    v2 = registry.register_run(run_id="b", name="line", model=ml.model,
                               metrics={}, features=["f1"])
    v3 = registry.register_run(run_id="c", name="other", model=ml.model,
                               metrics={}, features=["f1"])
    assert (v1.version, v2.version) == (1, 2)
    assert v3.version == 1          # 不同 name 各自从 1 起
    assert [v.version for v in registry.list_models("line")] == [1, 2]


def test_register_rejects_unknown_stage(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    with pytest.raises(MLError, match="未知 stage"):
        registry.register_run(run_id="x", name="n", model=ml.model, metrics={},
                              features=["f1"], stage="prod")


def test_events_record_every_transition(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    registry.register_run(run_id="a", name="line", model=ml.model, metrics={},
                          features=["f1"])
    ev = registry.events("line")
    assert len(ev) == 1 and ev[0]["to_stage"] == "candidate"
    assert ev[0]["from_stage"] is None


# ----------------------------------------------------------- 版本状态

def test_promote_keeps_single_production(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    for i in range(3):
        registry.register_run(run_id=f"r{i}", name="line", model=ml.model,
                              metrics={}, features=["f1"])
    registry.promote("line", 1, "production", note="首版上线")
    assert registry.production("line").version == 1

    registry.promote("line", 2, "production", note="v2 上线")
    prod = [v for v in registry.list_models("line") if v.stage == "production"]
    assert [v.version for v in prod] == [2]           # 至多一个
    assert registry.get_model("line", 1).stage == "archived"

    # 事件流里能看出「1 上线 → 1 下线 → 2 上线」的完整轨迹
    kinds = [(e["version"], e["to_stage"]) for e in registry.events("line")]
    assert (1, "production") in kinds and (1, "archived") in kinds


def test_promote_unknown_version_raises(ml_env):
    from lquant.research.ml import registry

    with pytest.raises(MLError, match="不在注册表里"):
        registry.promote("nope", 9, "production")


def test_promote_rejects_unknown_stage(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    registry.register_run(run_id="a", name="n", model=ml.model, metrics={},
                          features=["f1"])
    with pytest.raises(MLError, match="未知 stage"):
        registry.promote("n", 1, "live")


def test_production_asof_replays_event_stream(ml_env):
    """as-of 必须靠重放：直接读当前 stage 会把「现在」当成「当时」。"""
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    for i in range(3):
        registry.register_run(run_id=f"r{i}", name="line", model=ml.model,
                              metrics={}, features=["f1"])
    registry.promote("line", 1, "production")
    registry.promote("line", 2, "production")
    # 取事件流里的真实时刻作为 at 的边界 —— 两次晋级相隔微秒，
    # 用 datetime.now()+1s 这种写法根本夹不进中间（会测出假失败）。
    ev = registry.events("line")
    t1 = next(e["occurred_at"] for e in ev
              if e["version"] == 1 and e["to_stage"] == "production")
    t2 = next(e["occurred_at"] for e in ev
              if e["version"] == 2 and e["to_stage"] == "production")
    assert t2 > t1

    assert registry.production_asof("line", t1).version == 1
    assert registry.production_asof("line", t2).version == 2
    # 更早（还没人上线）→ 无生产版本
    assert registry.production_asof("line", datetime(2000, 1, 1)) is None


def test_rollback_returns_previous_production(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    for i in range(3):
        registry.register_run(run_id=f"r{i}", name="line", model=ml.model,
                              metrics={}, features=["f1"])
    registry.promote("line", 1, "production")
    registry.promote("line", 2, "production")
    mv = registry.rollback("line")
    assert mv is not None and mv.version == 1 and mv.stage == "production"
    assert registry.production("line").version == 1
    assert registry.get_model("line", 2).stage == "archived"


def test_rollback_without_history_returns_none(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    registry.register_run(run_id="a", name="line", model=ml.model, metrics={},
                          features=["f1"])
    registry.promote("line", 1, "production")
    assert registry.rollback("line") is None      # 只有一版上线过，无处可回
    assert registry.rollback("unknown") is None   # 名字都不存在


# ----------------------------------------------------------- 载入回放

def test_load_model_roundtrip_matches_predictions(ml_env):
    """载入后预测必须与训练时逐点一致 —— artifact 是完整可回放的。"""
    from lquant.research.ml import registry

    ml, ds, dates = _trained(processors=[{"kind": "standardize"}])
    registry.register_run(run_id="r1", name="demo", model=ml.model,
                          processor=ml.processor, metrics={}, features=["f1"])
    model, proc = registry.load_model("demo", 1)

    test = ds.slice(start=dates[40])
    X = test["f1"].cast(pl.Float64).to_numpy().reshape(-1, 1)
    raw_pred = np.asarray(model.predict(X), dtype=float)

    # 训练时的处理器状态与载入的完全一致（否则推理口径就漂了）
    assert proc.state_["mean"] == ml.processor.state_["mean"]
    assert proc.state_["std"] == ml.processor.state_["std"]
    Xp = proc.transform(test)["f1"].to_numpy().reshape(-1, 1)
    assert np.allclose(np.asarray(model.predict(Xp), dtype=float),
                       np.asarray(ml.model.predict(
                           ml.processor.transform(test)["f1"]
                           .to_numpy().reshape(-1, 1)), dtype=float),
                       atol=1e-12)
    assert raw_pred.shape == (len(test),)


def test_load_model_by_production_when_version_omitted(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    registry.register_run(run_id="a", name="line", model=ml.model, metrics={},
                          features=["f1"])
    registry.register_run(run_id="b", name="line", model=ml.model, metrics={},
                          features=["f1"])
    registry.promote("line", 2, "production")
    model, proc = registry.load_model("line")       # 缺省取 production
    assert proc is None                              # 本用例没声明处理器
    assert model.name == "ridge"


def test_load_model_missing_artifact_raises(ml_env):
    from lquant.research.ml import registry

    ml, _, _ = _trained()
    registry.register_run(run_id="a", name="line", model=ml.model, metrics={},
                          features=["f1"])
    (ml_env / "models" / "line" / "v1" / "model.pkl").unlink()
    with pytest.raises(MLError, match="模型文件缺失"):
        registry.load_model("line", 1)


def test_load_model_unknown_version_raises(ml_env):
    from lquant.research.ml import registry

    with pytest.raises(MLError, match="不存在"):
        registry.load_model("ghost", 3)


def test_pipeline_processor_state_roundtrips(ml_env):
    """Pipeline 的复合状态也要能载入（否则多步配方无法推理）。"""
    from lquant.research.ml import registry

    ml, _, _ = _trained(processors=[{"kind": "clip", "lower": 0.05, "upper": 0.95},
                                    {"kind": "standardize"}])
    registry.register_run(run_id="p", name="pipe", model=ml.model,
                          processor=ml.processor, metrics={}, features=["f1"])
    _, proc = registry.load_model("pipe", 1)
    assert proc.name == "pipeline"
    assert [p.name for p in proc.processors] == ["clip", "standardize"]
    df = pl.DataFrame({"f1": [1.0, 2.0, 3.0]})
    assert proc.transform(df)["f1"].to_list() == ml.processor.transform(df)["f1"].to_list()


# ----------------------------------------------------------- 空表韧性

def test_list_models_without_table_returns_empty(tmp_path, monkeypatch):
    """迁移没跑（表不存在）时返回 [] 而不是炸 API。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "empty.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.research.ml import registry

    assert registry.list_models() == []
    assert registry.events() == []
    get_settings.cache_clear()


# ----------------------------------------------------------- 路径与退化口径

def test_model_root_defaults_under_repo_data_dir(ml_env, monkeypatch):
    """未设 LQ_MODEL_DIR 时落在 ``<root>/data/models``（可随仓库搬家）。"""
    from lquant.core.config import get_settings
    from lquant.research.ml import registry

    monkeypatch.delenv("LQ_MODEL_DIR", raising=False)
    get_settings.cache_clear()
    assert registry.model_root() == ml_env / "data" / "models"
    # LQ_MODEL_DIR 优先
    monkeypatch.setenv("LQ_MODEL_DIR", str(ml_env / "elsewhere"))
    get_settings.cache_clear()
    assert registry.model_root() == ml_env / "elsewhere"


def test_rel_path_falls_back_to_absolute_outside_root(ml_env, tmp_path):
    """仓库外的 artifact 路径没法相对化 → 存绝对路径（而不是抛异常丢记录）。"""
    from lquant.research.ml.registry import _abs, _rel

    outside = tmp_path.parent / "outside-root" / "model.pkl"
    assert _rel(outside) == str(outside)
    # 相对路径进库、绝对路径直接用；空值 → None
    assert _abs(None) is None
    assert _abs(str(ml_env / "models" / "m.pkl")) == ml_env / "models" / "m.pkl"
    assert _abs("/tmp/abs.pkl").as_posix() == "/tmp/abs.pkl"


def test_list_models_and_events_survive_missing_tables(ml_env, monkeypatch):
    """迁移没跑/表缺失时返回空列表，而不是让调用方 500。"""
    from lquant.research.ml import registry

    def _boom():
        raise RuntimeError("no such table: ml_model")

    monkeypatch.setattr(registry, "reader", _boom, raising=False)
    monkeypatch.setattr("lquant.core.db.reader", _boom)
    assert registry.list_models("nope") == []
    assert registry.events("nope") == []


# ----------------------------------------------------------- archive / production 不变量

def test_archive_is_a_stage_transition(ml_env):
    from lquant.research.ml import registry

    ml, _ds, _ = _trained()
    mv = registry.register_run(run_id="a", name="line", model=ml.model,
                               metrics={}, features=["f1"], stage="candidate")
    archived = registry.archive("line", mv.version, note="手工下架")
    assert archived.stage == "archived"
    assert registry.get_model("line", mv.version).stage == "archived"
    # 事件流里能看到这次流转，as-of 才答得出来
    evs = [e for e in registry.events("line") if e["to_stage"] == "archived"]
    assert evs and evs[-1]["note"] == "手工下架"


def test_production_picks_highest_when_invariant_broken(ml_env):
    """手改库/并发写导致多个 production 时取版本号最大者并告警，而不是随机取。"""
    from lquant.core.db import writer
    from lquant.research.ml import registry

    ml, _ds, _ = _trained()
    v1 = registry.register_run(run_id="a", name="line", model=ml.model,
                               metrics={}, features=["f1"], stage="candidate")
    v2 = registry.register_run(run_id="b", name="line", model=ml.model,
                               metrics={}, features=["f1"], stage="candidate")
    # 直接改库制造「两个 production」（绕过 promote 的不变量维护）
    with writer() as con:
        con.execute("UPDATE ml_model SET stage='production' WHERE name='line'")
    got = registry.production("line")
    assert got.version == max(v1.version, v2.version)
