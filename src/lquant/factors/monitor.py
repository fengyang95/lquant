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

import contextlib
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


def _latest_completed_session():
    """最近一个**已收盘**的交易日（延迟 import 同上：别把 DuckDB 连接栈拖进 CLI）。"""
    from lquant.core.sessions import latest_completed_session

    return latest_completed_session()


def _session_lag(last_date):
    from lquant.core.sessions import session_lag

    return session_lag(last_date)


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
        _ensure_ic_daily_table(con)
        con.execute(
            "DELETE FROM factor_ic_daily WHERE factor = ? AND trade_date BETWEEN ? AND ?",
            [factor, lo, hi],
        )
        con.execute("INSERT INTO factor_ic_daily SELECT * FROM d")
    return len(d)


def _ensure_ic_daily_table(con=None) -> None:
    """按需补建 factor_ic_daily（老库迁移路径，见 ``ddl.ensure_factor_ic_daily``）。

    表只进了 ``DDL_STATEMENTS``（init_db / 服务启动才执行）：老库上监控写路径
    与 ``lq factor ic-health`` 读路径都会撞裸的 CatalogException。写路径传入
    已持有的写连接，读路径不带参数（自己开写连接，遵守 db.py 的写锁约定）。
    """
    from lquant.data.store.ddl import ensure_factor_ic_daily

    if con is not None:
        # 迁移尽力而为：失败不掩盖真正的查询错误（缺表时后面的查询会照旧报错）
        with contextlib.suppress(Exception):
            ensure_factor_ic_daily(con)
        return
    with contextlib.suppress(Exception), _writer() as wcon:
        ensure_factor_ic_daily(wcon)


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

    ``end`` 缺省取**最近一个已收盘交易日**（``core.sessions``），不是裸 today：
    15:00 前跑监控时不能把当日未收盘的 bar 当成完整日线。

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
        # 收盘前不能把当日当完整 bar：日线湖里可能已经有盘中采集写入的当日行，
        # 拿它当窗口末端会把「半天行情」算出的 IC 当作最终值写进 factor_ic_daily，
        # 而且行数和数值都正常 —— 典型的静默错误。窗口末端一律取已收盘会话。
        end = _latest_completed_session()
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
    max_age_days: int | None = 10,
) -> list[dict]:
    """近 window 个交易日的因子健康度。纯只读评估（DryRun 语义）。

    verdict 原因码：``no_data``（零记录）/ ``stale``（末条 IC 太旧，见
    ``max_age_days``；或有限 IC 覆盖不足一半窗口）/ ``degraded``（窗口内 IC
    全非有限，或 mean_ic / ICIR 跌破下限）/ ``ok``。
    ICIR = mean(ic)/std(ic)，日频不做年化 —— 与 ic_summary 的口径一致。

    **时效是独立维度，且优先于统计判定**：只取「最后 N 条记录」而不看它们是哪天
    的，同步停摆（定时任务/采集挂了）后窗口里永远塞满历史 IC，mean_ic/ICIR 一切
    正常 → 恒判 ok、ic_below 永不触发。``last_date`` 落后今天超过 ``max_age_days``
    个自然日即判 stale（``data_stale=True``）—— 数据是几个月前的，谈「今天 IC
    多少」没有意义。

    **但自然日会因长假虚增**：周末/国庆里行情本就不更新，只按自然日会把「节后
    第一个交易日」的正常数据误报成停摆。所以还要求 ``lag_sessions``（按交易会话
    计龄，见 ``core.sessions.session_lag``）也超过 ``max_age_days`` 才判 stale ——
    ``lag_days`` 仍按自然日给出（保持 CLI ``--max-age-days`` 的原口径），两个口径
    都在结果里可见。``max_age_days=None`` 仍可整体关闭时效判定。

    **非有限 IC（NaN/Inf）不算「健康」**：因子在某日截面内是常数（0/1 信号
    因子常见）时 ``pl.corr`` 返回 NaN —— 那正是「因子已无区分度」的证据。
    NaN 参与 mean/std 会让全部判定（``mean_ic < 下限``）恒为 False，于是退化
    因子被判成 ok、ic_below 永不触发。这里先剔除非有限值再算统计，并对
    「窗口内一条有限值都没有」单独给 degraded + 明说原因。
    """
    if int(window) < 1:
        raise ValueError(f"window 必须 >= 1，收到 {window}")
    if max_age_days is not None and int(max_age_days) < 0:
        raise ValueError(f"max_age_days 必须 >= 0 或 None，收到 {max_age_days}")
    today = _today()
    con_ds = _health_rows(name, int(window))
    if not con_ds and name is not None:
        # 显式点名查某因子但零记录：不能让查询悄悄消失 → no_data 判定
        return [
            {
                "factor": name,
                "n_days": 0,
                "n_valid": 0,
                "mean_ic": None,
                "icir": None,
                "last_date": None,
                "lag_days": None,
                "lag_sessions": None,
                "data_stale": False,
                "verdict": "no_data",
                "detail": "factor_ic_daily 无记录",
            }
        ]
    out = []
    for factor, rows in con_ds:
        n = len(rows["ic"])  # 窗口内记录数（含非有限值）：staleness 的可见性口径
        valid = _finite_ic(rows)
        n_valid = len(valid)
        mean_ic = float(valid["ic"].mean()) if n_valid else None
        std_ic = float(valid["ic"].std()) if n_valid > 1 else None
        icir = (mean_ic / std_ic) if (mean_ic is not None and std_ic and std_ic > 1e-12) else None
        last_date = rows["trade_date"].max() if n else None
        last_d = last_date.date() if hasattr(last_date, "date") else last_date
        lag_days = (today - last_d).days if last_d is not None else None
        # 会话级计龄：长假里自然日膨胀而交易会话不增，只按自然日判 stale 会把
        # 「节后第一个交易日」误报成同步停摆（本函数 docstring 原先承认的误报）。
        # 两个口径都超过阈值才判 stale —— max_age_days 的 CLI 口径（自然日）不变，
        # 只是去掉长假误报，不会让任何原本不 stale 的数据变 stale。
        lag_sessions = _session_lag(last_d) if last_d is not None else None
        data_stale = (
            max_age_days is not None
            and lag_days is not None and lag_days > int(max_age_days)
            and lag_sessions is not None and lag_sessions > int(max_age_days)
        )
        if n == 0:
            verdict, detail = "no_data", "factor_ic_daily 无记录"
        elif data_stale:
            verdict, detail = (
                "stale",
                f"末条 IC {last_d} 落后今天 {lag_days} 个自然日 / {lag_sessions} 个交易日"
                f"（都 > {max_age_days}）——"
                "同步停摆？基于陈旧数据的 mean_ic/ICIR 不可信",
            )
        elif n_valid == 0:
            verdict, detail = (
                "degraded",
                f"近 {window} 日 {n} 条 IC 全为非有限值（NaN/Inf）—— "
                "因子在该窗口无截面区分度（退化成常数？）或样本不足",
            )
        elif n_valid < max(2, int(window) // 2):
            verdict, detail = "stale", f"近 {window} 日仅 {n_valid} 日有有效 IC（同步断了？）"
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
                "n_valid": n_valid,
                "mean_ic": mean_ic,
                "icir": icir,
                "last_date": str(last_d) if n else None,
                "lag_days": lag_days,
                "lag_sessions": lag_sessions,
                "data_stale": data_stale,
                "verdict": verdict,
                "detail": detail,
            }
        )
    return out


def _finite_ic(rows: pl.DataFrame) -> pl.DataFrame:
    """剔除非有限 IC 的行（NaN/Inf 不是「IC = 0」）。

    ``is_finite`` 对 null 也返回 null，所以必须同时判 ``is_not_null``；
    非数值列（理论上不会出现）时退回原帧，让上层照旧看到 NaN。
    """
    if "ic" not in rows.columns:
        return rows
    try:
        return rows.filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
    except Exception:  # noqa: BLE001 - 非浮点列（旧库 Int？）时不做过滤
        return rows


def _health_rows(name, window):
    """近 window 个交易日 per 因子的 IC 行。返回 [(factor, df)]。

    表缺失（老库没跑过 init_db / 服务未重启）先按需补建再查一次 ——
    ``lq factor ic-health`` 在那种库上原本是裸 CatalogException。
    """
    def _query(con):
        where, params = "", []
        if name is not None:
            where = "WHERE factor = ?"
            params.append(name)
        return con.execute(
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

    from duckdb import CatalogException

    with _reader() as con:
        try:
            rows = _query(con)
        except CatalogException:
            _ensure_ic_daily_table()
            rows = _query(con)
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
    max_age_days: int | None = 10,
    notify_fn=None,
) -> dict:
    """编排：同步 → 健康评估 → 喂 notify/rules（ic_below 规则消费）。

    factors 缺省 = factor_def 里全部启用因子。单因子同步失败不阻断
    （结果里 error 逐条可见）。ctx 的 symbol 字段放因子名，ic_below
    规则按 target=因子名（single_symbol）或 scope=market 吃全部因子；
    ctx 另带 verdict / last_date，通知里能直接看出是「失效」还是「没数据」。
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
    health = factor_health(
        window=window, min_ic=min_ic, min_icir=min_icir, max_age_days=max_age_days
    )
    # ctx 带上 verdict / last_date：ic=None 的 degraded（窗口内 IC 全非有限）也要进
    # 规则引擎，否则「因子退化成常数」这条最该告警的路径会被过滤掉（ic_below 静默漏报）。
    # 时效 stale（data_stale）同样必须进，且 ic 一律置 None：数值是几个月前的，
    # 让 ic_below 在陈旧 mean_ic 上做今天的判断等于漏报「同步停摆」——
    # 统一走 rules.evaluate 的「IC 缺失/非有限」命中路径，last_date 说明原因。
    # 反过来 no_data / 覆盖度不足的 stale 不进：那是「没数据」，不是「因子失效」。
    ctxs = []
    for h in health:
        if h["data_stale"]:
            ctxs.append({
                "symbol": h["factor"], "ic": None, "verdict": h["verdict"],
                "last_date": h["last_date"], "lag_days": h["lag_days"],
                "lag_sessions": h["lag_sessions"],
            })
        elif h["mean_ic"] is not None or h["verdict"] == "degraded":
            ctxs.append({
                "symbol": h["factor"], "ic": h["mean_ic"], "verdict": h["verdict"],
                "last_date": h["last_date"],
            })
    from lquant.notify.rules import run_rules

    alerts = run_rules(ctxs, notify_fn=notify_fn)
    return {"synced": synced, "errors": errors, "health": health, "alerts": alerts}
