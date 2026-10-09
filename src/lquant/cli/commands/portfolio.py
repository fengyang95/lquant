"""lq portfolio：把 ``portfolio/`` 包接到命令行 / API（FEATURE-IDEAS C4）。

``portfolio/`` 里早已有选池（``screener``）、单标的仓位（``sizing``）、
组合权重（``weighting``）、基准相对优化（``optimizer``）四层能力，但此前
在 CLI/API 里**一处都调不到**（唯一引用是 ``strategy.py`` 导入 strategies
子包）—— 整套工具链不可达。本模块只做**编排**：数据从哪读、命令行参数
怎么映射到 portfolio 的函数、结果怎么排版；组合数学一律留在 ``portfolio/``
内部，这里不重写。

## 为什么共享核心函数放在本模块

硬约束只允许新增 ``cli/commands/portfolio.py`` 与 ``server/api/portfolio.py``，
且不得修改 ``portfolio/`` 内部。两个入口都需要「读湖 → 造因子 → 选池」
这套编排，重复实现必然漂移。方向选 CLI → API 反过来不行：
``server`` 是 optional extra（fastapi），CLI 侧导入 server 会让
``lq portfolio --help`` 强依赖 fastapi。所以纯函数（不依赖 click）放这里，
``server/api/portfolio.py`` 导入复用。click 是核心依赖，server 侧一定装得上。

## 数据口径

- 行情只读本地 Parquet 日线湖（``data.store.parquet.read_daily``），不打线上；
- ST / 次新 / 名称来自 DuckDB ``security`` 表（日线湖里没有这三列）。
  元数据不可用时**显式告警**并如实报告哪些过滤没生效，绝不假装过滤过了；
- 打分因子在湖上现算（动量 / 波动 / 流动性），全部只用 ≤ 当日的数据，
  不引入未来函数。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import click
import polars as pl

__all__ = [
    "PortfolioError",
    "PortfolioDataError",
    "screen_cross_section",
    "size_position",
    "portfolio_weights",
    "optimize_portfolio",
    "list_methods",
    "DERIVED_FACTORS",
    "portfolio",
]

# 湖上可现算的因子名（值只用 ≤ 当日数据）。mom{N}=N 日动量，
# vol{N}=N 日收益波动，amount{N}=N 日均额，turnover{N}=N 日均换手。
DERIVED_FACTORS = ("mom5", "mom20", "mom60", "vol20", "amount20", "turnover20")

# 打分默认因子：短中期动量。选池的「先过滤后打分」需要一个打分口径，
# 动量是最不需要额外数据源的一个 —— 只用 close 就能算出来。
DEFAULT_FACTORS = ("mom20",)

# 元数据缺 list_date 时的兜底：填一个远古日期 = 「上市已久，不是次新」。
# 不填 null 的原因在 screener.apply_filters：null 比较得到 null，filter 会
# **静默丢掉整行** —— 那等于「未知上市日期」被当成「次新」剔除，且不留痕迹。
_MISSING_LIST_DATE = date(1970, 1, 1)


class PortfolioError(Exception):
    """入参非法（缺参数 / 字段不存在 / 标的解析失败）—— 调用方的错。"""


class PortfolioDataError(PortfolioError):
    """本地数据不满足本次调用（湖为空 / 该日无行情 / 历史太短）—— 数据的错。

    继承自 :class:`PortfolioError` 便于 CLI 一网打尽；API 侧先单独捕获它
    映射成 404（「数据没有」），其余映射 422（「你的参数不对」）。
    """


# ---------------------------------------------------------------- 小工具


def _parse_date(raw: str | date | None, *, field: str) -> date | None:
    if raw is None or isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as e:
        raise PortfolioError(f"{field} 必须是 YYYY-MM-DD，收到 {raw!r}") from e


def _norm_symbols(raw: str | list[str] | None) -> list[str] | None:
    """归一标的列表；解析不了的显式报错（静默丢弃 = 悄悄换池子）。"""
    if raw is None:
        return None
    items = [x for x in (raw.split(",") if isinstance(raw, str) else raw) if str(x).strip()]
    if not items:
        return None
    from lquant.core.types import parse_symbol

    out: list[str] = []
    for item in items:
        try:
            out.append(str(parse_symbol(str(item))))
        except ValueError as e:
            raise PortfolioError(f"标的解析失败：{item!r}（{e}）") from e
    return out


def _parse_factor_opts(opts: tuple[str, ...] | list[str] | None) -> dict[str, float]:
    """``--factor mom20:0.6`` → ``{"mom20": 0.6}``（省略权重按 1.0）。"""
    if not opts:
        return {name: 1.0 for name in DEFAULT_FACTORS}
    out: dict[str, float] = {}
    for raw in opts:
        name, sep, weight = str(raw).partition(":")
        name = name.strip()
        if not name:
            raise PortfolioError(f"--factor 缺少因子名：{raw!r}")
        try:
            out[name] = float(weight) if sep else 1.0
        except ValueError as e:
            raise PortfolioError(f"--factor 权重不是数字：{raw!r}") from e
    return out


def _parse_scores(raw: Any) -> dict[str, float] | None:
    """接受 ``{"SYM": v}`` 或 ``"SYM=v,SYM2=v2"``；统一 key 口径。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        items = raw.items()
    else:
        parsed: list[tuple[str, str]] = []
        for chunk in str(raw).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            sym, sep, val = chunk.partition("=")
            if not sep:
                raise PortfolioError(f"--score 需要 SYM=VALUE 形式，收到 {chunk!r}")
            parsed.append((sym, val))
        items = parsed
    out: dict[str, float] = {}
    for sym, val in items:
        norm = _norm_symbols([str(sym)])
        assert norm is not None  # 非空列表一定拿到结果
        try:
            out[norm[0]] = float(val)
        except (TypeError, ValueError) as e:
            raise PortfolioError(f"分数不是数字：{sym}={val!r}") from e
    return out


def _lake_is_empty() -> bool:
    from lquant.data.store.parquet import lake_is_empty

    return lake_is_empty("daily")


def _read_daily(symbols: list[str] | None, start: str, end: str) -> pl.DataFrame:
    from lquant.data.store.parquet import read_daily

    return read_daily(symbols=symbols, start=start, end=end).collect()


def _security_meta() -> pl.DataFrame | None:
    """读 ``security`` 表拿 is_st / list_date / name；读不到返回 None。

    这里刻意吞异常返回 None 而不是 raise：日线湖是选池的**唯一必需**数据源，
    元数据只是把 ST/次新过滤点亮。但调用方必须把「元数据不可用」写进
    warnings 并返回给用户 —— 降级可以，装作没降级不行。
    """
    from pathlib import Path

    from lquant.core.config import get_settings

    if not Path(get_settings().duckdb_path).exists():
        return None
    try:
        from lquant.core.db import reader

        with reader() as con:
            return con.execute(
                "SELECT symbol, name, is_st, list_date FROM security"
            ).pl()
    except Exception:  # noqa: BLE001 - 表缺失/库半初始化都算「元数据不可用」
        return None


def _derive_factors(panel: pl.DataFrame) -> pl.DataFrame:
    """在日线面板上现算派生因子（只用 ≤ 当日的行）。

    ``shift/rolling`` 全部 ``over("symbol", order_by="trade_date")``：不依赖
    调用方是否已排序，也不用 ``.over("symbol")`` 的隐式行序（跨版本语义不稳）。
    """
    if panel.is_empty():
        return panel
    ret1 = pl.col("close") / pl.col("close").shift(1).over(
        "symbol", order_by="trade_date"
    ) - 1.0
    exprs: list[pl.Expr] = [
        ret1.alias("_ret1"),
        pl.col("amount").rolling_mean(window_size=20, min_samples=5)
        .over("symbol", order_by="trade_date").alias("amount20"),
    ]
    for n in (5, 20, 60):
        exprs.append(
            (
                pl.col("close")
                / pl.col("close").shift(n).over("symbol", order_by="trade_date")
                - 1.0
            ).alias(f"mom{n}")
        )
    exprs.append(
        ret1.rolling_std(window_size=20, min_samples=5)
        .over("symbol", order_by="trade_date").alias("vol20")
    )
    if "turnover_rate" in panel.columns:
        exprs.append(
            pl.col("turnover_rate").rolling_mean(window_size=20, min_samples=5)
            .over("symbol", order_by="trade_date").alias("turnover20")
        )
    return panel.with_columns(exprs)


# ---------------------------------------------------------------- screen


def _resolve_trade_date(raw: str | date | None) -> date:
    target = _parse_date(raw, field="--date")
    if target is not None:
        return target
    from lquant.data.store.parquet import latest_trade_date

    latest = latest_trade_date()
    if latest is None:
        raise PortfolioDataError(
            "本地日线湖为空 —— 先跑 `lq data demo` 生成演示数据，"
            "或 `lq data sync` 同步真实行情"
        )
    return latest


def _pick_columns(snapshot: pl.DataFrame, factors: dict[str, float]) -> list[str]:
    """输出列：标的 + 分数 + 关键行情 + 本次用到的因子（存在才带）。"""
    cols = ["symbol", "name", "close", "amount", "score"]
    cols += list(factors)
    seen: list[str] = []
    for c in cols:
        if c in snapshot.columns and c not in seen:
            seen.append(c)
    return seen


def screen_cross_section(
    *,
    trade_date: str | date | None = None,
    top_n: int = 30,
    factors: dict[str, float] | list[str] | None = None,
    cfg: Any = None,
    lookback_days: int = 180,
    symbols: str | list[str] | None = None,
) -> dict:
    """全市场横截面选股：先过滤，再打分，取 Top-N。

    返回结构化 dict（CLI 的 JSON 与 API 响应同源）。任何数据缺口都
    raise :class:`PortfolioDataError`，不返回「看起来正常」的空结果。
    """
    from lquant.portfolio.screener import FilterConfig, filter_report, screen

    if cfg is None:
        cfg = FilterConfig()
    if top_n <= 0:
        raise PortfolioError(f"--top-n 必须为正，收到 {top_n}")
    if lookback_days <= 0:
        raise PortfolioError(f"--lookback-days 必须为正，收到 {lookback_days}")

    factor_map = (
        factors if isinstance(factors, dict) else _parse_factor_opts(factors)
    )
    if not factor_map:
        raise PortfolioError("至少要有一个打分因子（--factor NAME[:权重]）")

    syms = _norm_symbols(symbols)
    target = _resolve_trade_date(trade_date)
    if _lake_is_empty():
        raise PortfolioDataError(
            "本地日线湖为空 —— 先跑 `lq data demo` 生成演示数据，"
            "或 `lq data sync` 同步真实行情"
        )

    start = target - timedelta(days=int(lookback_days))
    panel = _read_daily(syms, start.isoformat(), target.isoformat())
    if panel.is_empty():
        hint = "（--symbols 限定的标的可能不在湖里）" if syms else ""
        raise PortfolioDataError(
            f"{target} 及之前 {lookback_days} 天内无日线{hint} —— "
            f"该日可能休市，或还没同步。可用 `lq data check` 查看湖的覆盖"
        )

    panel = _derive_factors(panel)
    snapshot = panel.filter(pl.col("trade_date") == target)
    if snapshot.is_empty():
        raise PortfolioDataError(
            f"{target} 当日无日线（可能休市或未同步）—— 请换一个 --date，"
            f"或先 `lq data sync`"
        )

    # 缺失因子显式报错，不靠 score() 的 fill_null(0) 把「算不出来」当成「0 分」
    missing = [f for f in factor_map if f not in snapshot.columns]
    if missing:
        available = sorted(set(DERIVED_FACTORS) & set(snapshot.columns))
        raise PortfolioError(
            f"因子列不存在: {missing}；可用的现算因子: {available}；"
            f"也可用日线原始列（close/amount/float_mv 等）"
        )

    warnings: list[str] = []
    meta = _security_meta()
    if meta is None:
        warnings.append(
            "security 元数据不可用（DuckDB 未初始化或 security 表缺失）："
            "ST / 次新过滤本次未生效，结果可能含 ST 与次新股"
        )
    else:
        snapshot = snapshot.join(
            meta.select(["symbol", "name", "is_st", "list_date"]),
            on="symbol",
            how="left",
        )
        unknown = int(snapshot["list_date"].is_null().sum())
        if unknown:
            warnings.append(
                f"{unknown}/{len(snapshot)} 只标的不在 security 表："
                f"其 list_date 按 {_MISSING_LIST_DATE} 处理（视为非次新）"
            )
        snapshot = snapshot.with_columns(
            pl.col("list_date").fill_null(_MISSING_LIST_DATE),
            pl.col("is_st").fill_null(False),
        )

    report = filter_report(snapshot, cfg)
    picked = screen(snapshot, factor_map, cfg=cfg, top_n=top_n)
    picks = picked.select(_pick_columns(picked, factor_map)).to_dicts()

    return {
        "trade_date": target.isoformat(),
        "top_n": int(top_n),
        "factors": factor_map,
        "filters": report["config"],
        "filter_report": report,
        "meta_available": meta is not None,
        "warnings": warnings,
        "n_picks": len(picks),
        "picks": picks,
    }


# ---------------------------------------------------------------- size


def size_position(
    *,
    model: str = "atr",
    symbol: str | None = None,
    close: float | None = None,
    atr: float | None = None,
    daily_risk: float = 0.01,
    max_weight: float = 1.0,
    win_rate: float | None = None,
    win_loss_ratio: float | None = None,
    kelly_fraction: float = 0.5,
) -> dict:
    """单标的仓位：ATR 风险预算或半 Kelly。映射到 ``portfolio/sizing.py``。"""
    from lquant.portfolio import sizing

    sym = None
    if symbol:
        sym = (_norm_symbols([symbol]) or [None])[0]

    if model == "atr":
        if close is None or atr is None:
            raise PortfolioError("--model atr 需要 --close 与 --atr（同价格单位）")
        try:
            weight = sizing.atr_weight(
                float(close), float(atr), daily_risk=float(daily_risk),
                max_weight=float(max_weight),
            )
        except ValueError as e:
            raise PortfolioError(f"ATR 仓位参数非法：{e}") from e
        return {
            "model": "atr", "symbol": sym, "weight": weight, "note": None,
            "inputs": {"close": float(close), "atr": float(atr),
                       "daily_risk": float(daily_risk), "max_weight": float(max_weight)},
        }

    if model == "kelly":
        if win_rate is None or win_loss_ratio is None:
            raise PortfolioError(
                "--model kelly 需要 --win-rate 与 --win-loss-ratio（盈亏比）")
        try:
            weight = sizing.kelly_fraction(
                float(win_rate), float(win_loss_ratio), fraction=float(kelly_fraction)
            )
        except ValueError as e:
            raise PortfolioError(f"Kelly 仓位参数非法：{e}") from e
        note = None
        if weight <= 0.0:
            note = "期望劣势（f* ≤ 0）→ 0 仓位是决策，不是数据缺失"
        return {
            "model": "kelly", "symbol": sym, "weight": weight, "note": note,
            "inputs": {"win_rate": float(win_rate),
                       "win_loss_ratio": float(win_loss_ratio),
                       "fraction": float(kelly_fraction)},
        }

    raise PortfolioError(f"未知仓位模型 {model!r}（可选 atr / kelly）")


# ---------------------------------------------------------------- weights


def _returns_wide(
    symbols: list[str], start: date, end: date
) -> tuple[pl.DataFrame, list[str], list[str]]:
    """日收益宽表（index=trade_date，列=标的）。返回 (wide, present, missing)。"""
    panel = _read_daily(symbols, start.isoformat(), end.isoformat())
    if panel.is_empty():
        raise PortfolioDataError(
            f"[{start} ~ {end}] 无 {symbols} 的日线 —— 标的可能不在湖里，"
            f"或区间内还没同步（`lq data sync`）"
        )
    panel = panel.sort(["symbol", "trade_date"]).with_columns(
        (
            pl.col("close") / pl.col("close").shift(1).over(
                "symbol", order_by="trade_date") - 1.0
        ).alias("_ret1")
    )
    wide = panel.pivot(index="trade_date", on="symbol", values="_ret1").sort("trade_date")
    present = [s for s in symbols if s in wide.columns]
    missing = [s for s in symbols if s not in wide.columns]
    if not present:
        raise PortfolioDataError(f"标的 {symbols} 在 [{start} ~ {end}] 内都没有收益序列")
    if len(wide) < 2:
        raise PortfolioDataError(
            f"只有 {len(wide)} 个交易日的收益 —— 协方差至少需要 2 行，"
            f"请放宽 --start/--end 或 --lookback-days"
        )
    return wide.select(["trade_date", *present]), present, missing


def _weight_metrics(w: dict[str, float]) -> dict:
    """非 METHODS 方法（score_weight / market_cap_weight）的简易报告。"""
    import numpy as np

    arr = np.array(list(w.values()), dtype=float) if w else np.zeros(0)
    hhi = float((arr**2).sum()) if len(arr) else 0.0
    return {
        "effective_n": float(1.0 / hhi) if hhi > 0 else 0.0,
        "max_weight": float(arr.max()) if len(arr) else 0.0,
        "n_holdings": int((arr > 1e-6).sum()),
        "note": "该方法是分数/市值直接归一化，不依赖收益协方差，故无组合波动指标",
    }


def portfolio_weights(
    *,
    method: str = "equal",
    symbols: str | list[str] | None = None,
    scores: Any = None,
    start: str | date | None = None,
    end: str | date | None = None,
    lookback_days: int = 120,
    band: float | None = None,
    prev_weights: Any = None,
    benchmark: Any = None,
    cov_method: str | None = None,
    sqrt_cap: bool = False,
) -> dict:
    """组合权重：``weighting.weights()`` 统一入口 + ``weight_report`` 诊断。

    ``weighting.METHODS`` 是权威方法表；``score_weight`` / ``market_cap_weight``
    是模块里的独立函数、**不在** METHODS 注册表（``weights()`` 收到未注册名会
    静默退回等权），所以这里为这两个方法开显式分支，避免「列得出、调不对」。
    """
    from lquant.portfolio import weighting

    method = str(method)
    known = set(weighting.METHODS) | {"score_weight", "market_cap_weight"}
    if method not in known:
        raise PortfolioError(
            f"未知方法 {method!r}；可用: {sorted(known)}（见 `lq portfolio methods`）")

    score_map = _parse_scores(scores)

    if method == "score_weight":
        if not score_map:
            raise PortfolioError("score_weight 需要 --score SYM=分数")
        w = weighting.score_weight(
            pl.DataFrame({"symbol": list(score_map), "score": list(score_map.values())})
        )
        return {"method": method, "weights": w, "n_symbols": len(w),
                "report": [_weight_metrics(w)], "returns_rows": None,
                "warnings": []}

    syms = _norm_symbols(symbols)
    if not syms:
        raise PortfolioError(f"--method {method} 需要 --symbols（逗号分隔的标的）")

    if method == "market_cap_weight":
        target = _parse_date(end, field="--end") or _resolve_trade_date(None)
        start_d = _parse_date(start, field="--start") or (target - timedelta(days=7))
        panel = _read_daily(syms, start_d.isoformat(), target.isoformat())
        snap = panel.filter(pl.col("trade_date") == target)
        if snap.is_empty():
            raise PortfolioDataError(f"{target} 无日线，拿不到市值列")
        if "float_mv" not in snap.columns:
            raise PortfolioDataError("日线湖缺 float_mv 列，market_cap_weight 无输入")
        w = weighting.market_cap_weight(
            snap.select(["symbol", "float_mv"]).rename({"float_mv": "market_cap"}),
            sqrt=bool(sqrt_cap),
        )
        return {"method": method, "weights": w, "n_symbols": len(w),
                "report": [_weight_metrics(w)], "returns_rows": int(len(snap)),
                "warnings": []}

    target = _parse_date(end, field="--end") or _resolve_trade_date(None)
    start_d = _parse_date(start, field="--start") or (target - timedelta(days=lookback_days))
    if start_d >= target:
        raise PortfolioError(f"--start({start_d}) 必须早于 --end({target})")
    wide, present, missing = _returns_wide(syms, start_d, target)
    warnings = [f"标的 {missing} 在区间内无收益序列，已从权重里剔除"] if missing else []

    kw: dict[str, Any] = {}
    if score_map is not None:
        # METHODS 里只有 enhanced_indexing 消费 scores，其余方法经 **kw 忽略；
        # 不在这里按方法白名单裁剪，是为了让「给了分数但方法不吃」也可见
        # （报告里的 note 会说明），而不是被静默吞掉。
        kw["scores"] = score_map
    if cov_method:
        kw["cov_method"] = cov_method
    if band is not None:
        if prev_weights is None:
            raise PortfolioError("--band 必须配 --prev（没有上期权重就没有「不调仓」的参照）")
        kw["band"] = float(band)
        kw["prev_weights"] = _parse_scores(prev_weights) or {}
    elif prev_weights is not None and method != "enhanced_indexing":
        # enhanced_indexing 自己收 prev_weights（换手参照点）；其余方法只给
        # prev 而不给 band 是「装样子」，weights() 会 raise —— 提前换成可操作提示。
        raise PortfolioError("--prev 只在 --band 或 --method enhanced_indexing 时有意义")
    if benchmark is not None:
        kw["benchmark_weights"] = _parse_scores(benchmark) or {}

    try:
        w = weighting.weights(wide, method, present, **kw)
    except (PortfolioError, ValueError, KeyError) as e:
        raise PortfolioError(f"{method} 权重计算失败：{e}") from e
    except Exception as e:  # noqa: BLE001 - OptimizerError/RiskModelError 同族
        raise PortfolioError(f"{method} 权重计算失败：{e}") from e

    try:
        report = weighting.weight_report(wide, present, methods=[method], **kw).to_dicts()
    except Exception as e:  # noqa: BLE001 - 报告失败不该吞掉权重结果，但要可见
        warnings.append(f"weight_report 失败：{e}")
        report = []

    return {
        "method": method,
        "weights": {k: float(v) for k, v in w.items()},
        "n_symbols": len(w),
        "returns_rows": int(len(wide)),
        "report": report,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- optimize


def optimize_portfolio(
    *,
    symbols: str | list[str] | None = None,
    scores: Any = None,
    benchmark: Any = None,
    prev_weights: Any = None,
    te_target: float = 0.05,
    max_weight: float = 0.10,
    max_active: float | None = None,
    cov_method: str = "shrink_lw",
    max_turnover: float | None = None,
    band: float | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    lookback_days: int = 180,
) -> dict:
    """基准相对优化：包装 ``optimizer.enhanced_indexing_weight``（TE/换手/band）。"""
    from lquant.portfolio.optimizer import enhanced_indexing_weight
    from lquant.portfolio.weighting import apply_no_trade_band

    syms = _norm_symbols(symbols)
    if not syms:
        raise PortfolioError("optimize 需要 --symbols（逗号分隔的标的）")
    score_map = _parse_scores(scores)
    prev_map = _parse_scores(prev_weights)
    bench_map = _parse_scores(benchmark)

    target = _parse_date(end, field="--end") or _resolve_trade_date(None)
    start_d = _parse_date(start, field="--start") or (target - timedelta(days=lookback_days))
    wide, present, missing = _returns_wide(syms, start_d, target)
    warnings = [f"标的 {missing} 在区间内无收益序列，已剔除"] if missing else []

    try:
        res = enhanced_indexing_weight(
            wide, present,
            benchmark_weights=bench_map,
            scores=score_map,
            te_target=float(te_target),
            max_weight=float(max_weight),
            max_active=max_active,
            cov_method=cov_method,
            max_turnover=max_turnover,
            prev_weights=prev_map,
        )
    except Exception as e:  # noqa: BLE001 - OptimizerError 等统一转可操作错误
        raise PortfolioError(f"优化失败：{e}") from e

    weights = res.weights()
    n_changed = None
    if band is not None:
        if prev_map is None:
            raise PortfolioError("--band 必须配 --prev")
        weights, n_changed = apply_no_trade_band(weights, prev_map, band=float(band))

    diag = {k.lstrip("_"): v for k, v in res.diagnostics.items()}
    if diag.get("fallback"):
        warnings.append(f"优化器不可行，已退回显式候选：{diag.get('fallback_reason')}")
    if diag.get("constraint_violations"):
        warnings.append(f"候选解违反声明约束：{diag['constraint_violations']}")

    return {
        "method": "enhanced_indexing",
        "weights": {k: float(v) for k, v in weights.items()},
        "diagnostics": diag,
        "band": band,
        "n_changed": n_changed,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- methods


def list_methods() -> dict:
    """``weighting.METHODS``（权威注册表）+ 两个未注册但可用的独立函数。"""
    from lquant.portfolio.weighting import METHODS

    registered = [
        {"name": n, "source": "weighting.METHODS",
         "needs": "scores" if n == "enhanced_indexing" else "returns"}
        for n in sorted(METHODS)
    ]
    extra = [
        {"name": "score_weight", "source": "weighting.score_weight",
         "needs": "scores（--score SYM=分数）"},
        {"name": "market_cap_weight", "source": "weighting.market_cap_weight",
         "needs": "float_mv（从日线湖当日截面取）"},
    ]
    return {
        "registered": registered,
        "unregistered": extra,
        "note": "weights() 收到未注册方法名会静默退回等权，故这两个走显式分支",
    }


# ---------------------------------------------------------------- CLI


def _emit(payload: dict, *, as_json: bool, title: str) -> None:
    """stdout 只放结果：JSON 模式保证 stdout 是**纯净**可解析的 JSON。"""
    warnings = payload.get("warnings") or []
    for w in warnings:
        click.echo(f"[warn] {w}", err=True)
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        return
    click.echo(f"# {title}")
    if "picks" in payload:
        for row in payload["picks"]:
            click.echo(
                f"{row.get('symbol', '?'):>12}  score={row.get('score', 0.0): .4f}  "
                f"close={row.get('close', float('nan'))}  amount={row.get('amount', float('nan'))}"
            )
    elif "weights" in payload:
        for sym, w in sorted(payload["weights"].items(), key=lambda kv: -kv[1]):
            click.echo(f"{sym:>12}  {w:.6f}")


@click.group()
def portfolio() -> None:
    """组合工具：横截面选池 / 单标的仓位 / 组合权重 / 基准相对优化。

    这些能力来自 ``lquant.portfolio`` 包（此前只被策略库间接引用，CLI 不可达）。
    """


@portfolio.command("methods")
@click.option("--json/--no-json", "as_json", default=True, show_default=True,
              help="JSON 输出（默认）；--no-json 走人类可读")
def methods_cmd(as_json: bool) -> None:
    """列出可用的权重方法（``weighting.METHODS`` + 两个未注册的独立函数）。"""
    payload = list_methods()
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        for m in payload["registered"]:
            click.echo(f"{m['name']:<20} {m['source']}")
        for m in payload["unregistered"]:
            click.echo(f"{m['name']:<20} {m['source']}  (未注册)")


@portfolio.command("screen")
@click.option("--date", "trade_date", default=None,
              help="交易日 YYYY-MM-DD；缺省取湖内最新交易日")
@click.option("--top-n", type=int, default=30, show_default=True, help="取前 N 只")
@click.option("--factor", "factor_opts", multiple=True,
              help="打分因子 NAME[:权重]，可重复；默认 mom20。"
                   "现算因子: mom5/mom20/mom60/vol20/amount20/turnover20")
@click.option("--exclude-st/--keep-st", "exclude_st", default=True, show_default=True,
              help="剔除 ST / 退市整理（需 security 元数据）")
@click.option("--min-list-days", type=int, default=60, show_default=True,
              help="上市天数下限，剔除次新（需 security.list_date）")
@click.option("--min-amount", type=float, default=5_000_000.0, show_default=True,
              help="当日成交额下限（元）")
@click.option("--min-price", type=float, default=1.0, show_default=True, help="价格下限（元）")
@click.option("--exclude-limit-up/--keep-limit-up", "exclude_limit_up", default=True,
              show_default=True, help="剔除一字涨停（买不进）")
@click.option("--exclude-limit-down", is_flag=True, default=False,
              help="剔除一字跌停（选股时通常不排除）")
@click.option("--exclude-suspended/--keep-suspended", "exclude_suspended", default=True,
              show_default=True, help="剔除停牌（volume<=0）")
@click.option("--lookback-days", type=int, default=180, show_default=True,
              help="往前读多少**日历日**算因子（mom60 至少要 90 个交易日）")
@click.option("--symbols", default=None, help="限定标的（逗号分隔）；缺省全市场")
@click.option("--json/--no-json", "as_json", default=True, show_default=True,
              help="JSON 输出（默认，供 Agent 消费）")
def screen_cmd(trade_date, top_n, factor_opts, exclude_st, min_list_days, min_amount,
               min_price, exclude_limit_up, exclude_limit_down, exclude_suspended,
               lookback_days, symbols, as_json) -> None:
    """全市场横截面选股：先过滤（ST/次新/停牌/一字板/流动性/价格）后打分取 Top-N。"""
    from lquant.portfolio.screener import FilterConfig

    cfg = FilterConfig(
        exclude_st=exclude_st,
        min_list_days=min_list_days,
        min_amount=min_amount,
        min_price=min_price,
        exclude_suspended=exclude_suspended,
        exclude_limit_up=exclude_limit_up,
        exclude_limit_down=exclude_limit_down,
    )
    try:
        payload = screen_cross_section(
            trade_date=trade_date, top_n=top_n,
            factors=list(factor_opts) or None, cfg=cfg,
            lookback_days=lookback_days, symbols=symbols,
        )
    except PortfolioError as e:
        raise click.ClickException(str(e)) from e
    _emit(payload, as_json=as_json, title=f"portfolio screen {payload['trade_date']}")


@portfolio.command("size")
@click.option("--model", type=click.Choice(["atr", "kelly"]), default="atr",
              show_default=True)
@click.option("--symbol", default=None, help="仅用于回显，不影响计算")
@click.option("--close", type=float, default=None, help="现价（元）")
@click.option("--atr", type=float, default=None, help="平均真实波幅（与 close 同单位）")
@click.option("--daily-risk", type=float, default=0.01, show_default=True,
              help="单日风险预算占比（0.01 = 1%）")
@click.option("--max-weight", type=float, default=1.0, show_default=True,
              help="权重上限，须在 (0,1]")
@click.option("--win-rate", type=float, default=None, help="Kelly 胜率 p∈(0,1)")
@click.option("--win-loss-ratio", type=float, default=None, help="Kelly 盈亏比 b>0")
@click.option("--kelly-fraction", type=float, default=0.5, show_default=True,
              help="Kelly 折扣（0.5 = half-Kelly）")
@click.option("--json/--no-json", "as_json", default=True, show_default=True)
def size_cmd(model, symbol, close, atr, daily_risk, max_weight, win_rate,
             win_loss_ratio, kelly_fraction, as_json) -> None:
    """单标的仓位：ATR 风险预算 / 半 Kelly。"""
    try:
        payload = size_position(
            model=model, symbol=symbol, close=close, atr=atr,
            daily_risk=daily_risk, max_weight=max_weight, win_rate=win_rate,
            win_loss_ratio=win_loss_ratio, kelly_fraction=kelly_fraction,
        )
    except PortfolioError as e:
        raise click.ClickException(str(e)) from e
    _emit(payload, as_json=as_json, title=f"portfolio size {model}")


@portfolio.command("weights")
@click.option("--method", default="equal", show_default=True,
              help="权重方法；见 `lq portfolio methods`")
@click.option("--symbols", default=None, help="标的（逗号分隔）；协方差类方法必填")
@click.option("--score", "scores", default=None,
              help="分数 SYM=值,SYM2=值2（enhanced_indexing / score_weight 用）")
@click.option("--start", default=None, help="收益区间起点 YYYY-MM-DD")
@click.option("--end", default=None, help="收益区间终点；缺省湖内最新交易日")
@click.option("--lookback-days", type=int, default=120, show_default=True,
              help="未给 --start 时往前取多少日历日")
@click.option("--benchmark", default=None, help="基准权重 SYM=w,...（enhanced_indexing）")
@click.option("--prev", "prev_weights", default=None, help="上期权重 SYM=w,...（band）")
@click.option("--band", type=float, default=None, help="no-trade band（须配 --prev）")
@click.option("--cov-method", default=None,
              help="协方差口径：sample/shrink_lw/structured_* /poet 等")
@click.option("--sqrt-cap", is_flag=True, default=False, help="市值加权用 sqrt(市值)")
@click.option("--json/--no-json", "as_json", default=True, show_default=True)
def weights_cmd(method, symbols, scores, start, end, lookback_days, benchmark,
                prev_weights, band, cov_method, sqrt_cap, as_json) -> None:
    """组合权重：调 weighting.weights()，输出归一化权重 + weight_report 关键指标。"""
    try:
        payload = portfolio_weights(
            method=method, symbols=symbols, scores=scores, start=start, end=end,
            lookback_days=lookback_days, band=band, prev_weights=prev_weights,
            benchmark=benchmark, cov_method=cov_method, sqrt_cap=sqrt_cap,
        )
    except PortfolioError as e:
        raise click.ClickException(str(e)) from e
    _emit(payload, as_json=as_json, title=f"portfolio weights {payload['method']}")


@portfolio.command("optimize")
@click.option("--symbols", default=None, help="标的（逗号分隔），必填")
@click.option("--score", "scores", default=None, help="α 分数 SYM=值,...（必填）")
@click.option("--benchmark", default=None, help="基准权重 SYM=w,...；缺省等权基准")
@click.option("--prev", "prev_weights", default=None, help="上期权重 SYM=w,...")
@click.option("--te", "te_target", type=float, default=0.05, show_default=True,
              help="年化跟踪误差上限")
@click.option("--max-weight", type=float, default=0.10, show_default=True)
@click.option("--max-active", type=float, default=None, help="单只主动权重上限")
@click.option("--cov-method", default="shrink_lw", show_default=True)
@click.option("--max-turnover", type=float, default=None, help="换手上限（须配 --prev）")
@click.option("--band", type=float, default=None, help="no-trade band（须配 --prev）")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--lookback-days", type=int, default=180, show_default=True)
@click.option("--json/--no-json", "as_json", default=True, show_default=True)
def optimize_cmd(symbols, scores, benchmark, prev_weights, te_target, max_weight,
                 max_active, cov_method, max_turnover, band, start, end,
                 lookback_days, as_json) -> None:
    """基准相对优化：TE 约束下最大化预期超额（含换手上限 / no-trade band）。"""
    try:
        payload = optimize_portfolio(
            symbols=symbols, scores=scores, benchmark=benchmark,
            prev_weights=prev_weights, te_target=te_target, max_weight=max_weight,
            max_active=max_active, cov_method=cov_method, max_turnover=max_turnover,
            band=band, start=start, end=end, lookback_days=lookback_days,
        )
    except PortfolioError as e:
        raise click.ClickException(str(e)) from e
    _emit(payload, as_json=as_json, title="portfolio optimize")
