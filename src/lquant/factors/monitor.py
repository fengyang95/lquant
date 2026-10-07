"""因子在线监控闭环：每日滚动 IC 落表 → 健康评估 → ic_below 告警。

借鉴 qlib Online Serving「上线后持续评估」：因子入库只是起点，真正的风险
在上线后衰减。IC 计算（evaluate/ic）、8 渠道通知（notify）、规则引擎
（notify/rules）三件套都在，这里只做胶水层：

- **factor_ic_daily 表**：逐日 IC / RankIC 序列（``factor_ic`` 是每因子
  一行的汇总缓存，撑不起「衰减趋势」）；按窗口快照替换，每日增量重跑幂等。
- **评估纯函数**：``factor_health`` 只读表算健康度，不发通知不落库
  （与 notify/rules.evaluate 的 DryRun 语义同款）；编排层
  ``run_daily_check`` 才做「同步 → 评估 → 告警」。
- **失败不静默**：单因子同步失败记入结果继续跑下一个（一只因子的表达式
  问题不该让整轮监控哑火），但失败原因逐条可见；同步函数本体 fail-loudly。
- **口径**：raw IC（无协变量中性化），与 ``lq factor eval --raw`` 对照
  可比；监控关心的是因子本身的衰减，不是剥离风格后的残余。
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl

__all__ = [
    "sync_factor_ic",
    "factor_health",
    "run_daily_check",
    "upsert_ic_daily",
]


def _writer():
    """延迟 import：避免 CLI --help 之外的场景拖起 DuckDB 连接栈。"""
    from lquant.core.db import writer

    return writer()


def _reader():
    from lquant.core.db import reader

    return reader()


def _today():
    from lquant.core.types import today_cn

    return today_cn()


def _panel(start=None, end=None) -> pl.DataFrame:
    """日线面板读取。测试替身注入点；生产口径与评价主路径同为 read_daily。"""
    from lquant.data.store.parquet import read_daily

    return read_daily(start=start, end=end).collect()


def upsert_ic_daily(rows: pl.DataFrame, *, factor: str) -> int:
    """按因子快照替换 (factor, trade_date) 序列：先删本次窗口再插，幂等。

    rows 列：trade_date / ic / rank_ic / n。窗口内全量重写（而不是逐行
    upsert）——每日任务重跑同窗口时结果与首跑一致，不存在半新半旧行。
    """
    need = {"trade_date", "ic", "rank_ic", "n"}
    miss = need - set(rows.columns)
    if miss:
        raise KeyError(f"rows 缺列 {sorted(miss)}")
    if not len(rows):
        return 0
    d = rows.select(
        pl.lit(factor).alias("factor"),
        pl.col("trade_date").cast(pl.Date),
        pl.col("ic").cast(pl.Float64),
        pl.col("rank_ic").cast(pl.Float64),
        pl.col("n").cast(pl.Int64),
        pl.lit(_now_ts()).alias("updated_at"),
    )
    lo, hi = d["trade_date"].min(), d["trade_date"].max()
    with _writer() as con:
        con.execute(
            "DELETE FROM factor_ic_daily WHERE factor = ? AND trade_date BETWEEN ? AND ?",
            [factor, lo, hi],
        )
        con.execute("INSERT INTO factor_ic_daily SELECT * FROM d")
    return len(d)


def _now_ts():
    from lquant.core.types import now_cn_naive

    return now_cn_naive()


def sync_factor_ic(
    name: str,
    *,
    expression: str | None = None,
    start=None,
    end=None,
    lookback_days: int = 120,
    min_obs: int = 5,
) -> dict:
    """单因子近窗口逐日 IC：读日线 → 算因子 → 前瞻收益 → ic_series → 落表。

    expression 缺省从 factor_def 读（未注册的因子名 KeyError fail-loudly，
    监控一个不存在的表达式没有意义）。返回落表摘要。
    """
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.ic import ic_series

    if expression is None:
        with _reader() as con:
            row = con.execute("SELECT expression FROM factor_def WHERE name = ?", [name]).fetchone()
        if row is None:
            raise KeyError(f"因子 {name!r} 未注册（factor_def 无记录），先 lq factor add")
        expression = row[0]

    if end is None:
        end = _today()
    if start is None:
        start = end - timedelta(days=int(lookback_days))

    df = _panel(start=start, end=end)
    if not len(df):
        return {"factor": name, "n_days": 0, "detail": "日线数据为空"}
    d = forward_return(df.sort(["symbol", "trade_date"]), "close", periods=[1])
    d = compute_factor_col(d, expression, "f")
    s = ic_series(d, "f", "fwd_ret_1", min_obs=min_obs)
    n = upsert_ic_daily(s.select("trade_date", "ic", "rank_ic", "n"), factor=name)
    return {
        "factor": name,
        "expression": expression,
        "n_days": n,
        "from": str(s["trade_date"].min()) if n else None,
        "to": str(s["trade_date"].max()) if n else None,
    }


def factor_health(
    name: str | None = None,
    *,
    window: int = 20,
    min_ic: float = 0.0,
    min_icir: float = 0.0,
) -> list[dict]:
    """近 window 个交易日的因子健康度。纯只读评估（DryRun 语义）。

    verdict 原因码：``no_data``（零记录）/ ``stale``（覆盖不足一半窗口，
    同步断了）/ ``degraded``（mean_ic 或 ICIR 跌破下限）/ ``ok``。
    ICIR = mean(ic)/std(ic)，日频不做年化 —— 与 ic_summary 的口径一致。
    """
    if int(window) < 1:
        raise ValueError(f"window 必须 >= 1，收到 {window}")
    con_ds = _health_rows(name, int(window))
    if not con_ds and name is not None:
        # 显式点名查某因子但零记录：不能让查询悄悄消失 → no_data 判定
        return [
            {
                "factor": name,
                "n_days": 0,
                "mean_ic": None,
                "icir": None,
                "last_date": None,
                "verdict": "no_data",
                "detail": "factor_ic_daily 无记录",
            }
        ]
    out = []
    for factor, rows in con_ds:
        n = len(rows["ic"])
        mean_ic = float(rows["ic"].mean()) if n else None
        std_ic = float(rows["ic"].std()) if n > 1 else None
        icir = (mean_ic / std_ic) if (mean_ic is not None and std_ic and std_ic > 1e-12) else None
        if n == 0:
            verdict, detail = "no_data", "factor_ic_daily 无记录"
        elif n < max(2, int(window) // 2):
            verdict, detail = "stale", f"近 {window} 日仅 {n} 日有 IC（同步断了？）"
        elif mean_ic < float(min_ic):
            verdict, detail = "degraded", f"mean_ic {mean_ic:.4f} < 下限 {min_ic}"
        elif icir is not None and icir < float(min_icir):
            verdict, detail = "degraded", f"ICIR {icir:.3f} < 下限 {min_icir}"
        else:
            verdict, detail = "ok", ""
        out.append(
            {
                "factor": factor,
                "n_days": n,
                "mean_ic": mean_ic,
                "icir": icir,
                "last_date": str(rows["trade_date"].max()) if n else None,
                "verdict": verdict,
                "detail": detail,
            }
        )
    return out


def _health_rows(name, window):
    """近 window 个交易日 per 因子的 IC 行。返回 [(factor, df)]。"""
    with _reader() as con:
        where, params = "", []
        if name is not None:
            where = "WHERE factor = ?"
            params.append(name)
        rows = con.execute(
            f"""
            SELECT factor, trade_date, ic
            FROM (
                SELECT factor, trade_date, ic,
                       row_number() OVER (PARTITION BY factor ORDER BY trade_date DESC) AS _rn
                FROM factor_ic_daily {where}
            ) WHERE _rn <= {int(window)}
            ORDER BY factor, trade_date
            """,
            params,
        ).pl()
    if not len(rows):
        return []
    return [(f, g) for (f,), g in rows.group_by(["factor"], maintain_order=True)]


def run_daily_check(
    *,
    factors: list[str] | None = None,
    lookback_days: int = 120,
    window: int = 20,
    min_ic: float = 0.0,
    min_icir: float = 0.0,
    notify_fn=None,
) -> dict:
    """编排：同步 → 健康评估 → 喂 notify/rules（ic_below 规则消费）。

    factors 缺省 = factor_def 里全部启用因子。单因子同步失败不阻断
    （结果里 error 逐条可见）。ctx 的 symbol 字段放因子名，ic_below
    规则按 target=因子名（single_symbol）或 scope=market 吃全部因子。
    """
    if factors is None:
        with _reader() as con:
            factors = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM factor_def WHERE enabled ORDER BY name"
                ).fetchall()
            ]
    synced, errors = [], []
    for name in factors:
        try:
            synced.append(sync_factor_ic(name, start=None, lookback_days=lookback_days))
        except Exception as e:  # noqa: BLE001 - 单因子失败继续跑（原因可见）
            from lquant.core.logging import get_logger

            get_logger(__name__).warning(f"因子 IC 同步失败 {name}: {e}")
            errors.append({"factor": name, "error": f"{type(e).__name__}: {e}"})
    health = factor_health(window=window, min_ic=min_ic, min_icir=min_icir)
    ctxs = [{"symbol": h["factor"], "ic": h["mean_ic"]} for h in health if h["mean_ic"] is not None]
    from lquant.notify.rules import run_rules

    alerts = run_rules(ctxs, notify_fn=notify_fn)
    return {"synced": synced, "errors": errors, "health": health, "alerts": alerts}
