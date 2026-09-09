"""submit 即重验（方案 6.3 硬护栏 3）。

平台不信任 Agent 的任何数字：重跑 G0-G3（含 val 段），对照 spec.claimed，
A/B 级入库，C/D 级落 factor_replication 表归档正确数字（D = 研报存疑）。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.mining.fitness import corrected_threshold

GRADE_TOL_A = 0.005
GRADE_TOL_B = 0.02


def _panel_with_covs(start=None):
    """读日线 + 协变量（市值/行业/换手率），与挖掘/评价统一口径。"""
    from lquant.core.db import reader as db_reader
    from lquant.data.store.parquet import read_daily
    from lquant.factors.covariates import build_covariates

    df = read_daily(start=start).collect()
    try:
        with db_reader() as con:
            ind = con.execute("SELECT symbol, std, code, std_date FROM industry_classify").pl()
    except Exception:  # noqa: BLE001
        ind = None
    covs = ["market_cap", "industry_sw1", "turnover_1m"]
    df, report = build_covariates(df, covs, industry_df=ind)
    present = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    return df, present


def _split_eval(df, cov_cols, expr):
    """train/val 切分 + 重算 train/val IC（含中性化）。"""
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.mining.runner import split_dates
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    dates = df["trade_date"].unique().to_list()
    train_d, val_d, _ = split_dates(dates)
    out = {}
    for label, dd in (("train", train_d), ("val", val_d)):
        sub = df.filter(pl.col("trade_date").is_in(dd))
        sub = forward_return(sub.sort(["symbol", "trade_date"]), "close", periods=[1, 5])
        d = compute_factor_col(sub, expr, "f").drop_nulls(["f", "fwd_ret_1"])
        if cov_cols:
            d = pipeline_run(d, "f", [
                {"op": "winsorize", "method": "mad", "n": 5},
                {"op": "standardize", "method": "zscore"},
                {"op": "neutralize", "method": "ols", "factors": cov_cols},
            ]).drop_nulls(["f"])
        s = ic_series(d, "f", "fwd_ret_1")
        out[label] = s
    return out


def verify_and_register(spec: dict) -> tuple[bool, dict]:
    """重验入口：G0 -> 重算 -> 对照 claimed -> 分级 -> 入库/归档。"""
    from lquant.data.store.catalog import upsert
    from lquant.factors.dsl.analyzer import check as dsl_check
    from lquant.factors.dsl.parser import parse
    from lquant.factors.evaluate.ic import _t_stat as tstat
    from lquant.factors.dsl.printer import canonical_id

    expr = spec["expr"]
    name = spec.get("name") or "cand_" + canonical_id(expr)
    g0 = g0_static(expr, allowed_fields=_daily_fields())
    payload = {"name": name, "expr": expr, "agent": spec.get("agent", "manual")}
    if not g0.passed:
        payload.update({"ok": False, "grade": "REJECTED", "stage": "G0",
                        "reason_code": g0.reason_code, "hint": g0.hint})
        return False, payload
    try:
        df, cov_cols = _panel_with_covs(start=spec.get("start"))
        splits = _split_eval(df, cov_cols, expr)
    except Exception as e:  # noqa: BLE001
        payload.update({"ok": False, "grade": "REJECTED", "stage": "RECOMPUTE",
                        "reason_code": "COMPUTE_FAIL", "hint": str(e)})
        return False, payload
    s_tr, s_val = splits["train"], splits["val"]
    if not len(s_tr) or not len(s_val):
        payload.update({"ok": False, "grade": "REJECTED", "stage": "RECOMPUTE",
                        "reason_code": "OOS_FAIL", "hint": "train/val 段 IC 序列为空"})
        return False, payload
    ic_tr = float(s_tr["ic"].mean())
    ic_val = float(s_val["ic"].mean())
    t_val = tstat(ic_val, float(s_val["ic"].std()), len(s_val))
    thr = corrected_threshold(spec.get("n_trials", 2))
    payload["recomputed"] = {"ic_train": round(ic_tr, 4), "ic_val": round(ic_val, 4),
                             "t_val": round(t_val, 2), "threshold": round(thr, 2)}
    claimed = (spec.get("claimed") or {}).get("ic_mean")
    grade = "A"
    if claimed is not None:
        diff = abs(claimed - ic_val)
        if diff <= GRADE_TOL_A:
            grade = "A"
        elif claimed * ic_val > 0 and diff <= GRADE_TOL_B:
            grade = "B"
        elif claimed * ic_val <= 0:
            grade = "D"    # 方向反了 —— 研报存疑
        else:
            grade = "C"
    payload["grade"] = grade
    if t_val is not None and abs(t_val) < thr:
        payload.update({"ok": False, "reason_code": "LOW_TSTAT",
                        "hint": f"|t_val|={abs(t_val):.2f} < 校正门槛 {thr:.2f}"})
        _archive(spec, payload)
        return False, payload
    if grade in ("A", "B") or claimed is None:
        now = dt_now()
        try:
            upsert("factor_def", pl.DataFrame([{
            "name": name, "expression": expr,
            "description": spec.get("rationale", ""),
            "source": "mined" if spec.get("agent") else "manual",
            "source_ref": spec.get("agent", "manual"),
            "factor_id": canonical_id(expr),
            "enabled": True, "created_at": now,
        }]))
        except Exception as e:  # noqa: BLE001
            payload["register_warning"] = str(e)
        payload["ok"] = True
        return True, payload
    _archive(spec, payload)
    payload["ok"] = False
    return False, payload


def g0_static(expr, allowed_fields=None):
    from lquant.factors.mining.gates import g0_static as _g0

    return _g0(expr, allowed_fields)


def _daily_fields():
    """日线表可用字段白名单（与 covariates/provider 口径一致）。"""
    return {"trade_date", "symbol", "open", "high", "low", "close", "volume",
            "amount", "pre_close", "turnover_rate", "adj_factor", "float_mv"}


def dt_now():
    import datetime as dt

    return dt.datetime.now()


def _archive(spec: dict, payload: dict) -> None:
    """C/D 级与拒绝件落 factor_replication 表 —— 研究资产，不是垃圾。"""
    import polars as pl

    from lquant.data.store.catalog import upsert

    try:
        upsert("factor_replication", pl.DataFrame([{
            "name": spec.get("name", ""), "expr": spec["expr"],
            "claimed_ic": (spec.get("claimed") or {}).get("ic_mean"),
            "recomputed_ic": payload.get("recomputed", {}).get("ic_val"),
            "grade": payload.get("grade", "X"),
            "agent": spec.get("agent", "manual"),
            "payload": str(payload)[:2000],
            "created_at": dt_now(),
        }]))
    except Exception as e:  # noqa: BLE001
        import loguru

        loguru.logger.warning(f"factor_replication 归档失败: {e}")
