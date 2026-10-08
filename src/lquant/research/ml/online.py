"""滚动重训与每日推理编排（借 qlib ``workflow/online/OnlineManager`` 的语义）。

qlib 的 ``OnlineManager`` 是**批量**在线管理（定期重训 + 滚动推理），
``contrib/online/`` 那套是死代码（import 即 ModuleNotFoundError，见
``docs/research/qlib/00-qlib-integration-proposal.md``）。这里只借语义：

## 三段职责

1. **重训**（:func:`rolling_retrain`）：在 ``walk_forward_splits`` 的每个窗口上
   训练一次，每次注册一个新版本（``candidate``）。
2. **晋级**（:func:`safe_promote`）：**先验证再晋级** —— 载入 artifact、
   跑一次推理、确认输出形状与有限性。验证不过就留在候选，绝不让一个
   载不起来的模型上线；晋级过程本身失败则回滚到上一版（:func:`rollback`）。
3. **推理**（:func:`daily_inference`）：载入 production 版本，对指定日期
   产出信号并落 ``ml_signal``（带 ``model_version``，事后可审计
   「这天的信号是哪一版出的」）。

## 为什么「先验证再晋级」是必需的

``Model.save``/``load`` 走 pickle，模型文件损坏、处理器状态缺失、特征列改名
都不会在保存时报错 —— 只会在**线上第一次推理**时炸，而那时已经过了收盘。
把验证放在晋级前，故障窗口从「一次交易」缩到「一次训练」。

## 晋级判据

默认比较 ``test_rank_ic_mean``：新版本必须**不低于**线上版本（``min_improvement``
可要求必须更好）。没有线上版本时直接晋级。指标缺失（NaN/None）视为不可比较
→ **不晋级**（宁可用旧版，也不用一个指标都算不出来的新版）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl
from loguru import logger

from lquant.core.errors import MLError

__all__ = ["OnlineConfig", "rolling_retrain", "safe_promote", "daily_inference",
           "persist_signals", "load_signals", "verify_version"]

#: 晋级判据默认取这个指标（越小越好的指标不适用，这里只支持越大越好）
DEFAULT_METRIC = "test_rank_ic_mean"


@dataclass
class OnlineConfig:
    name: str                                   # 逻辑模型线（版本流的分组键）
    features: list[str]
    label_horizon: int = 5
    kind: str = "auto"
    processors: list[dict] | None = None
    top_n: int = 30
    train_months: int = 24
    valid_months: int = 6
    test_months: int = 6
    step_months: int = 6
    #: purge/embargo（单位：交易日根数），见 walk_forward_splits 的口径。
    #: None = 按 ``label_horizon`` 自动取 h（默认就该这样：标签是前瞻 h 日收益，
    #: 训练段尾部 h 根的标签会伸进下一段）。显式给值时才覆盖。
    purge_bars: int | None = None
    embargo_bars: int = 1
    promote_metric: str = DEFAULT_METRIC
    min_improvement: float = 0.0                # 新版本相对线上至少要高多少
    strategy_cls: object | None = None
    model_params: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 重训

def rolling_retrain(
    df: pl.DataFrame,
    cfg: OnlineConfig,
    *,
    record: bool = True,
    promote: bool = True,
    progress=None,
    cancel_check=None,
) -> dict:
    """在滚动窗口上逐段重训，每次注册一个版本；返回版本清单与样本外汇总。

    ``promote=False`` 时只产出候选版本（人工挑），适合先观察再上线。
    ``progress(done, total, phase)`` / ``cancel_check()`` 由任务队列注入。
    """
    from lquant.research.ml.backtest import train_and_predict
    from lquant.research.ml.dataset import DatasetConfig, build_dataset, walk_forward_splits
    from lquant.research.ml.panel import build_feature_panel

    if not cfg.features:
        raise MLError("OnlineConfig.features 不能为空")
    panel = build_feature_panel(df, cfg.features)
    ds = build_dataset(panel, DatasetConfig(
        features=cfg.features, label_horizon=cfg.label_horizon,
        processors=cfg.processors))

    # 标签是前瞻 label_horizon 日收益：训练段最后一根的标签要等到之后第 h 根
    # 才定型。purge 取 h，训练样本的标签终点就严格早于验证/测试段起点，不再
    # 「见过」测试期；embargo 再留 1 根，隔开边界处的自相关（特征窗口/波动）。
    # 两笔都是「按标签口径算出来的」，不是拍脑袋的常数。
    purge = cfg.label_horizon if cfg.purge_bars is None else cfg.purge_bars
    splits = walk_forward_splits(ds.dates, cfg.train_months, cfg.valid_months,
                                 cfg.test_months, cfg.step_months,
                                 purge_bars=purge, embargo_bars=cfg.embargo_bars)
    if not splits:
        raise MLError(
            f"数据跨度不足以切出滚动窗口（{len(ds.dates)} 个交易日；"
            f"需要 train={cfg.train_months} + valid={cfg.valid_months} + "
            f"test={cfg.test_months} 个月）")

    versions: list[dict] = []
    oos: list[float] = []
    total = len(splits)
    for i, sp in enumerate(splits):
        if cancel_check is not None and cancel_check():
            logger.warning("滚动重训被取消，保留已完成的版本")
            break
        if progress is not None:
            progress(done=i, total=total, phase="rolling",
                     message=f"窗口 {i + 1}/{total}")
        tr_e, va_e = sp["train"][1], sp["valid"][1]
        try:
            # 必须传 test_end：否则早期窗口的"样本外"指标会吃进后面所有
            # 窗口的数据，滚动重训的评估就全废了。
            # 必须传 window（而不是只传端点）：purge/embargo 的隔离带只有
            # split_window 才认，用连续 split 重建会把缺口并回训练段。
            ml = train_and_predict(ds, tr_e, va_e, test_end=sp["test"][1],
                                   window=sp, kind=cfg.kind, **cfg.model_params)
        except Exception as e:  # noqa: BLE001  单窗口失败不该毁掉整轮重训
            logger.error(f"窗口 {i + 1} 训练失败，跳过: {type(e).__name__}: {e}")
            versions.append({"window": i + 1, "status": "failed", "error": str(e)[:200]})
            continue

        metric = ml.summary().get(cfg.promote_metric)
        if metric is not None and np.isfinite(float(metric)):
            oos.append(float(metric))
        entry = {"window": i + 1, "status": "trained", "metric": metric,
                 "train_end": str(tr_e), "test_end": str(va_e),
                 "train_rows": ml.train_rows, "test_rows": ml.test_rows}

        if record:
            from lquant.research.ml.registry import register_run

            run_id = _run_id(cfg.name, i + 1, tr_e)
            mv = register_run(
                run_id=run_id, name=cfg.name, model=ml.model,
                processor=ml.processor,
                metrics={"ml": ml.summary(), "online": {"window": i + 1}},
                params=cfg.model_params, features=cfg.features,
                fit_window=ml.fit_window, dataset=ds.summary(),
                train_rows=ml.train_rows, test_rows=ml.test_rows,
                train_end=tr_e, test_end=va_e, stage="candidate",
                note=f"rolling window {i + 1}/{total}")
            entry["version"] = mv.version
            entry["run_id"] = run_id
            if promote:
                try:
                    res = safe_promote(cfg.name, mv.version, panel=panel,
                                       features=cfg.features,
                                       metric=metric, metric_name=cfg.promote_metric,
                                       min_improvement=cfg.min_improvement)
                    entry["promoted"] = res["promoted"]
                    entry["promote_reason"] = res["reason"]
                except Exception as e:  # noqa: BLE001  晋级失败保持原线上版
                    logger.error(f"晋级 v{mv.version} 失败（保持原线上版）: {e}")
                    entry["promoted"] = False
                    entry["promote_reason"] = f"晋级异常: {type(e).__name__}: {e}"
        versions.append(entry)

    if progress is not None:
        progress(done=total, total=total, phase="done", message="完成")
    return {
        "name": cfg.name,
        "windows": total,
        "versions": versions,
        "n_trained": sum(1 for v in versions if v["status"] == "trained"),
        "n_promoted": sum(1 for v in versions if v.get("promoted")),
        "oos_metric_mean": float(np.mean(oos)) if oos else None,
        "oos_metric_std": float(np.std(oos, ddof=1)) if len(oos) > 1 else None,
        "metric": cfg.promote_metric,
    }


def _run_id(name: str, window: int, train_end) -> str:
    import hashlib

    raw = f"{name}|{window}|{train_end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- 验证 + 晋级

def verify_version(name: str, version: int, *, panel: pl.DataFrame,
                   features: list[str]) -> dict:
    """晋级前验证：能载入、能变换、能预测、输出有限且行数对得上。

    这一步把「模型文件坏了」从**线上首次推理**提前到**晋级前** ——
    pickle 反序列化失败、处理器状态缺失、特征列改名都只在这里暴露。

    ``panel`` 可以是**原始湖表**（特征列还没算）也可以是已算好的面板：
    内部会先走 :func:`build_feature_panel`（幂等），避免调用方两套写法。
    """
    from lquant.research.ml import registry
    from lquant.research.ml.panel import build_feature_panel

    try:
        model, proc = registry.load_model(name, version)
    except Exception as e:  # noqa: BLE001
        # 载入失败统一成 MLError：调用方（safe_promote / API）只认这一种，
        # 让 UnpicklingError / FileNotFoundError 之类穿透出去会绕过降级逻辑。
        raise MLError(
            f"{name} v{version} 验证失败：artifact 载入异常 "
            f"（{type(e).__name__}: {e}）") from e
    panel = build_feature_panel(panel, features)
    sub = panel.select(["symbol", "trade_date", *features]).drop_nulls(features)
    if len(sub) == 0:
        raise MLError(f"{name} v{version} 验证失败：验证面板在指定特征上全为空")
    x = sub.select(features)
    if proc is not None:
        x = proc.transform(x)
    X = np.column_stack([x[f].cast(pl.Float64, strict=False).fill_null(0.0).to_numpy()
                         for f in features])
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    pred = np.asarray(model.predict(X), dtype=float)
    if pred.shape != (len(sub),):
        raise MLError(f"{name} v{version} 验证失败：预测形状 {pred.shape} "
                      f"与输入行数 {len(sub)} 不符")
    if not np.all(np.isfinite(pred)):
        n_bad = int((~np.isfinite(pred)).sum())
        raise MLError(f"{name} v{version} 验证失败：预测含 {n_bad} 个非有限值")
    return {"name": name, "version": version, "rows": len(sub),
            "signal_mean": float(pred.mean()), "signal_std": float(pred.std()),
            "processor": proc.name if proc is not None else None}


def safe_promote(
    name: str,
    version: int,
    *,
    panel: pl.DataFrame,
    features: list[str],
    metric: float | None = None,
    metric_name: str = DEFAULT_METRIC,
    min_improvement: float = 0.0,
) -> dict:
    """先验证、再按判据比较、最后晋级；任何一步失败都保持原线上版。

    返回 ``{promoted, reason, verification}``。**不抛异常**（除参数错误）：
    晋级失败是运行期常态（指标不如旧版），调用方据此记录而不是中断重训。
    """
    from lquant.research.ml import registry

    try:
        ver = verify_version(name, version, panel=panel, features=features)
    except Exception as e:  # noqa: BLE001
        return {"promoted": False, "reason": f"验证失败，未晋级: {e}",
                "verification": None}

    cur = registry.production(name)
    if cur is not None and cur.version == version:
        return {"promoted": False, "reason": f"v{version} 已是线上版",
                "verification": ver}

    if metric is None or not np.isfinite(float(metric)):
        return {"promoted": False,
                "reason": f"{metric_name} 不可用（None/NaN），不晋级（宁用旧版）",
                "verification": ver}

    if cur is not None:
        base = (cur.metrics or {}).get("ml", {}).get(metric_name)
        if base is None:
            base = (cur.metrics or {}).get(metric_name)
        if (base is not None and np.isfinite(float(base))
                and float(metric) < float(base) + min_improvement):
            return {"promoted": False,
                    "reason": f"{metric_name}={metric:.6f} 未超过线上 "
                              f"v{cur.version} 的 {float(base):.6f}"
                              f"（要求 +{min_improvement}）",
                    "verification": ver}

    mv = registry.promote(name, version, "production",
                          note=f"auto: {metric_name}={metric:.6f}")
    return {"promoted": True,
            "reason": f"晋级 v{version}（{metric_name}={metric:.6f}"
                      + (f"，取代 v{cur.version}" if cur else "，首个线上版") + "）",
            "verification": ver, "model": mv.as_dict()}


# ---------------------------------------------------------------- 推理

def _predict(panel: pl.DataFrame, features: list[str], model, proc) -> pl.DataFrame:
    """对面板逐行打分（与 ``verify_version`` 同一变换顺序）。"""
    sub = panel.select(["symbol", "trade_date", *features]).drop_nulls(features)
    if len(sub) == 0:
        return sub.with_columns(pl.lit(None, dtype=pl.Float64).alias("signal"))
    x = sub.select(features)
    if proc is not None:
        x = proc.transform(x)
    X = np.column_stack([x[f].cast(pl.Float64, strict=False).fill_null(0.0).to_numpy()
                         for f in features])
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return sub.with_columns(pl.Series("signal", np.asarray(model.predict(X),
                                                           dtype=float)))


def daily_inference(
    df: pl.DataFrame,
    cfg: OnlineConfig,
    *,
    date: date | str | None = None,
    version: int | None = None,
    persist: bool = True,
) -> dict:
    """用线上版本（或指定版本）对某日截面产出信号。

    信号落 ``ml_signal`` 时**带上 model_version** —— 这是事后审计
    「某天的信号是哪一版出的」的唯一依据（``production_asof`` 给出结论，
    这里给出证据）。
    """
    from lquant.research.ml import registry
    from lquant.research.ml.panel import build_feature_panel

    target = _as_date(date) if date is not None else None
    if version is None:
        prod = registry.production(cfg.name)
        if prod is None:
            raise MLError(f"模型线 {cfg.name} 没有线上版本，先重训并晋级")
        version = prod.version
    model, proc = registry.load_model(cfg.name, version)

    panel = build_feature_panel(df, cfg.features)
    if target is not None:
        panel = panel.filter(pl.col("trade_date") == target)
        if len(panel) == 0:
            raise MLError(f"{cfg.name}: {target} 无行情数据（检查日期与股票池）")
    sig = _predict(panel, cfg.features, model, proc)
    out = {
        "name": cfg.name, "version": version,
        "date": str(target) if target else None,
        "rows": len(sig),
        "signal_mean": float(sig["signal"].mean()) if len(sig) else None,
    }
    if persist:
        out["persisted"] = persist_signals(sig, name=cfg.name, version=version,
                                           run_id=None)
    return out


def persist_signals(sig: pl.DataFrame, *, name: str, version: int,
                    run_id: str | None = None) -> int:
    """信号落 ``ml_signal``（幂等 upsert）。返回写入行数。"""
    from lquant.core.db import writer
    from lquant.core.types import now_cn

    if len(sig) == 0:
        return 0
    frame = sig.select(["trade_date", "symbol", "signal"]).with_columns(
        pl.lit(name).alias("name"),
        pl.lit(int(version)).alias("model_version"),
        pl.lit(run_id).alias("run_id"),
        pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime).alias("created_at"),
    ).select(["name", "trade_date", "symbol", "signal", "model_version",
              "run_id", "created_at"])
    with writer() as con:
        con.register("_sig", frame)
        con.execute(
            "INSERT OR REPLACE INTO ml_signal SELECT name, trade_date, symbol,"
            " signal, model_version, run_id, created_at FROM _sig")
    return len(frame)


def load_signals(name: str, *, start=None, end=None, version: int | None = None) -> pl.DataFrame:
    """读信号（回测/监控用）。表不存在返回空帧。"""
    sql = ("SELECT trade_date, symbol, signal, model_version, run_id FROM ml_signal"
           " WHERE name = ?")
    args: list = [name]
    if start is not None:
        sql += " AND trade_date >= ?"
        args.append(_as_date(start))
    if end is not None:
        sql += " AND trade_date <= ?"
        args.append(_as_date(end))
    if version is not None:
        sql += " AND model_version = ?"
        args.append(int(version))
    sql += " ORDER BY trade_date, symbol"
    try:
        from lquant.core.db import reader

        with reader() as con:
            rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001  表未建
        return pl.DataFrame(schema={"trade_date": pl.Date, "symbol": pl.Utf8,
                                    "signal": pl.Float64,
                                    "model_version": pl.Int64, "run_id": pl.Utf8})
    return pl.DataFrame(rows, schema=["trade_date", "symbol", "signal",
                                      "model_version", "run_id"], orient="row")


def _as_date(v) -> date:
    return date.fromisoformat(v) if isinstance(v, str) else v
