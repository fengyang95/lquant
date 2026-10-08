"""submit 即重验（方案 6.3 硬护栏 3）。

平台不信任 Agent 的任何数字：重跑 G0-G3（含 val 段），对照 spec.claimed，
A/B 级入库，C/D 级落 factor_replication 表归档正确数字（D = 研报存疑）。
"""
from __future__ import annotations

import json
import math
import time

import polars as pl

from lquant.factors.mining.fitness import corrected_threshold

GRADE_TOL_A = 0.005
GRADE_TOL_B = 0.02


def _read_industry_df(retries: int = 3, delay: float = 0.5):
    """读 PIT 行业分类表，带锁冲突重试 —— 重试耗尽必须报错，绝不静默降级。

    e2e 实测：另一个进程持有 DuckDB 文件锁（常驻 uvicorn / 并行 CLI 都会）时，
    reader() 抛 IOException: Conflicting lock。这里若吞掉异常，行业协变量会
    「随机消失」—— 同一表达式 eval 报 3 个协变量、audit 报 2 个，中性化口径
    分叉，IC 数字不可比且无人察觉。表不存在（未 init_db 的新环境）仍按
    「无行业数据」处理，交给 CovariateUnavailable 显式上报 coverage=0。
    """
    import duckdb

    from lquant.core.db import reader as db_reader

    last: Exception | None = None
    for attempt in range(retries):
        try:
            with db_reader() as con:
                return con.execute(
                    "SELECT symbol, std, code, std_date FROM industry_classify").pl()
        except duckdb.CatalogException:
            return None     # 新环境未建表：无行业数据（会被 coverage=0 显式暴露）
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError(
        f"industry_classify 读取失败（重试 {retries} 次）: {last}。"
        "常见原因：另一个进程持有 DuckDB 文件锁（常驻 uvicorn 或并行 CLI）。"
        "拒绝静默降级 —— 无行业协变量的中性化 IC 与有行业时不可比。"
    ) from last


DEFAULT_COVS = ("market_cap", "industry_sw1", "turnover_1m")


def _panel_with_covs(start=None, covs=None):
    """读日线 + 协变量，与挖掘/评价统一口径。

    ``covs=None``（缺省）= 既有统一口径（``DEFAULT_COVS``），所有既有调用点
    行为零漂移；显式传清单（如看板另类数据 covariate ``mf_main_ratio``/
    ``lhb_on_board``，见 ``factors/sources/board.py``）时逐个走 provider
    注册表 join 面板 —— 是**替换**语义：想在看板 cov 之上保留行业中性化，
    就把 ``industry_sw1`` 一并写进清单。
    """
    from lquant.data.store.parquet import read_daily
    from lquant.factors.covariates import build_covariates

    df = read_daily(start=start).collect()
    ind = _read_industry_df()
    covs = list(covs) if covs is not None else list(DEFAULT_COVS)
    df, report = build_covariates(df, covs, industry_df=ind)
    present = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    return df, present


def prepare_segment(df, cov_cols, expr, dates, *, horizons=(1, 5), with_pre: bool = False):
    """在给定交易日子集上算因子 → 前瞻收益 → 中性化，返回分析就绪的 df。

    train/val 切分、submit 重验、``lq factor audit`` 深度校验、``lq factor robust``
    鲁棒性检验共用这一条实现 —— 口径一旦分叉，「JSON 里的 ic_mean」和
    「报告里的 ic_mean」就对不上，这种不一致最难查。

    ``with_pre=True`` 时返回 ``(分析就绪帧, 中性化之前的帧)``。后者是「IC 归因阶梯」
    的基线：阶梯要回答「中性化吃掉了多少 IC」，拿已中性化的帧当 raw 段等于没问。
    """
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    sub = df.filter(pl.col("trade_date").is_in(list(dates)))
    sub = forward_return(sub.sort(["symbol", "trade_date"]), "close", periods=list(horizons))
    d = drop_nonfinite(compute_factor_col(sub, expr, "f"), "f")
    if "fwd_ret_1" in d.columns:
        d = drop_nonfinite(d, "fwd_ret_1")
    pre = d
    if cov_cols:
        d = pipeline_run(d, "f", [
            {"op": "winsorize", "method": "mad", "n": 5},
            {"op": "standardize", "method": "zscore"},
            {"op": "neutralize", "method": "ols", "factors": cov_cols},
        ])
    d = drop_nonfinite(d, "f")
    return (d, pre) if with_pre else d


def _split_eval(df, cov_cols, expr):
    """train/val 切分 + 重算 train/val IC（含中性化）。"""
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.mining.runner import split_dates

    dates = df["trade_date"].unique().to_list()
    train_d, val_d, _ = split_dates(dates)
    return {
        label: ic_series(prepare_segment(df, cov_cols, expr, dd), "f", "fwd_ret_1")
        for label, dd in (("train", train_d), ("val", val_d))
    }


def verify_and_register(spec: dict) -> tuple[bool, dict]:
    """重验入口：G0 -> 重算 -> 对照 claimed -> 分级 -> 入库/归档。"""
    from lquant.data.store.catalog import upsert
    from lquant.factors.dsl.printer import canonical_id
    from lquant.factors.evaluate.ic import _t_stat as tstat

    expr = spec["expr"]
    name = spec.get("name") or "cand_" + canonical_id(expr)
    payload = {"name": name, "expr": expr, "agent": spec.get("agent", "manual")}
    # SKILL 铁律：无 rationale（研究动机/口径说明）不得入库 —— 缺失直接拒
    if not (spec.get("rationale") or "").strip():
        payload.update({"ok": False, "grade": "REJECTED", "stage": "G0",
                        "reason_code": "MISSING_RATIONALE",
                        "hint": "spec 缺少 rationale（研究动机/口径说明），拒绝入库"})
        return False, payload
    # G0 是毫秒级静态门禁，永远是第一步：坏表达式（未知字段/语法错）不该先触发
    # 「读全湖 + 构协变量」。此前为拿实际挂载的 cov_cols 拼白名单把面板读取提到
    # G0 之前，后果是读湖失败（锁冲突/湖缺失）时真实原因被 COMPUTE_FAIL 掩盖，
    # 用户拿到的是「lake read boom」而不是可操作的 STATIC_FAIL + 字段提示。
    # 白名单不必依赖真实挂载：spec.covs 声明了哪些 covariate，就允许哪些 cov_ 字段
    # （纯语法判断）；真实挂载与 coverage 由随后的 RECOMPUTE 阶段显式校验。
    declared_covs = {"cov_" + str(c) for c in (spec.get("covs") or [])}
    g0 = g0_static(expr, allowed_fields=_daily_fields() | declared_covs)
    if not g0.passed:
        payload.update({"ok": False, "grade": "REJECTED", "stage": "G0",
                        "reason_code": g0.reason_code, "hint": g0.hint})
        return False, payload
    # G0 之后才读面板：spec.covs 声明的 covariate 数据不可用（coverage=0，列根本没
    # 挂上）会在 RECOMPUTE 阶段最早暴露，而不是入库后第一日因子值全 null 静默失效。
    try:
        df, cov_cols = _panel_with_covs(start=spec.get("start"), covs=spec.get("covs"))
    except Exception as e:  # noqa: BLE001
        payload.update({"ok": False, "grade": "REJECTED", "stage": "RECOMPUTE",
                        "reason_code": "COMPUTE_FAIL", "hint": str(e)})
        return False, payload
    try:
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
    try:
        from lquant.factors.replication import attribute

        payload["attribution"] = attribute(
            spec.get("claimed"), payload.get("recomputed", {}), grade,
            spec.get("assumptions", []))
    except Exception:  # noqa: BLE001
        pass
    # t_val 为 nan（val 段天数不足或 IC 方差为 0）时不能放行：
    # abs(nan) < thr 恒为 False，claimed 缺省时会把没有样本外证据的因子放进库。
    if t_val is None or not math.isfinite(t_val) or abs(t_val) < thr:
        if t_val is None or not math.isfinite(t_val):
            hint = "val 段 IC 无法计算 t（天数不足或方差为 0），拒绝入库"
        else:
            hint = f"|t_val|={abs(t_val):.2f} < 校正门槛 {thr:.2f}"
        payload.update({"ok": False, "reason_code": "LOW_TSTAT", "hint": hint})
        _archive(spec, payload)
        return False, payload
    if grade in ("A", "B") or claimed is None:
        now = dt_now()
        # 把 spec.covs 一起落库：audit/report/robust/run 的复算路径要靠它读回
        # 「入库时用的中性化口径」。不落库的话，同一因子在 submit 用 spec.covs
        # （如看板另类数据）、在 audit 用 DEFAULT_COVS（市值/行业/换手），两边
        # 报的 IC 不是同一个数 —— 直接违反本模块自述的硬不变量。None 落 NULL，
        # 老行与未声明 covs 的因子行为不变（复算走 DEFAULT_COVS）。
        covs_json = json.dumps(list(spec["covs"])) if spec.get("covs") is not None else None
        try:
            upsert("factor_def", pl.DataFrame([{
            "name": name, "expression": expr,
            "description": (spec.get("rationale", "") + " claimed=" + json.dumps(spec.get("claimed") or {})).strip(),
            "source": "mined" if spec.get("agent") else "manual",
            "source_ref": spec.get("agent", "manual"),
            "factor_id": canonical_id(expr),
            "covs": covs_json,
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
    """日线表可用字段白名单（与 covariates/provider 口径一致）。

    真相源在 `factors/fields.py` —— 因子编辑画布的字段面板与这里共用一份，
    避免「画布能选到、引擎不认」的漂移。
    """
    from lquant.factors.fields import daily_fields

    return daily_fields()


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
