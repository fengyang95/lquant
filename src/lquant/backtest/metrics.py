"""绩效指标：年化 / 夏普 / 索提诺 / 最大回撤 / 胜率 / 换手 / 月度。

两个容易算错的地方：
1. **年化用几何而非算术**。`总收益 / 年数` 会高估，尤其波动大的策略；
   正确是 `(1 + 总收益) ^ (252 / 交易日数) - 1`。
2. **最大回撤用净值口径**。对收益率序列直接取最小值是错的，
   必须先复权成净值再算 running max。

这些指标错了不会抛异常，只会让策略看起来更好 —— 所以全部有单测兜底。
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

__all__ = ["drawdown_series", "max_drawdown", "perf_from_returns", "perf_from_nav",
           "monthly_returns", "turnover_from_trades", "summary_line"]


def _as_array(x) -> np.ndarray:
    if x is None:
        return np.array([], dtype=float)
    if hasattr(x, "to_numpy"):
        x = x.to_numpy()
    a = np.asarray(x, dtype=float)
    return a[np.isfinite(a)]


def drawdown_series(nav: np.ndarray) -> np.ndarray:
    """相对历史最高净值的回撤序列（负值或 0）。"""
    if len(nav) == 0:
        return np.array([])
    peak = np.maximum.accumulate(nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, nav / peak - 1.0, 0.0)
    return dd


def max_drawdown(nav) -> tuple[float, int | None, int | None]:
    """返回 (最大回撤, 峰值下标, 谷底下标)。"""
    a = _as_array(nav)
    if len(a) == 0:
        return 0.0, None, None
    dd = drawdown_series(a)
    i = int(np.argmin(dd))
    mdd = float(dd[i])
    peak_i = int(np.argmax(a[: i + 1])) if i > 0 else 0
    return mdd, peak_i, i


def _annualize(total: float, n: int, ppy: int = 252) -> float:
    if n <= 0 or total <= -1:
        return float("nan")
    return (1.0 + total) ** (ppy / n) - 1.0


def perf_from_returns(returns, *, dates: list[date] | None = None,
                      periods_per_year: int = 252, risk_free: float = 0.0) -> dict:
    """从日收益率序列算全套指标。

    Parameters
    ----------
    returns : 日收益率（小数，如 0.01）
    periods_per_year : 年化周期数，日频 252，月频 12
    """
    r = _as_array(returns)
    n = len(r)
    if n == 0:
        return {"n_periods": 0}

    nav = np.cumprod(1.0 + r)
    total = float(nav[-1] - 1.0)
    ann_ret = _annualize(total, n, periods_per_year)
    vol = float(np.std(r, ddof=1) * math.sqrt(periods_per_year)) if n > 1 else float("nan")

    downside = r[r < 0]
    dvol = (float(np.std(downside, ddof=1) * math.sqrt(periods_per_year))
            if len(downside) > 1 else float("nan"))

    sharpe = ((ann_ret - risk_free) / vol) if vol and vol > 1e-12 and math.isfinite(vol) else float("nan")
    sortino = ((ann_ret - risk_free) / dvol) if dvol and dvol > 1e-12 and math.isfinite(dvol) else float("nan")

    mdd, pi, ti = max_drawdown(nav)
    calmar = (ann_ret / abs(mdd)) if mdd < -1e-9 and math.isfinite(ann_ret) else float("nan")

    wins = r[r > 0]
    losses = r[r < 0]
    win_rate = float(len(wins) / n) if n else float("nan")
    payoff = (float(wins.mean() / abs(losses.mean()))
              if len(wins) and len(losses) and abs(losses.mean()) > 1e-12 else float("nan"))

    # 长回撤期：从峰值到修复的持续期，比最大回撤幅度更能反映实盘痛苦程度
    dd = drawdown_series(nav)
    underwater = int(np.sum(dd < -1e-9))
    longest_dd = _longest_underwater(dd)

    return {
        "n_periods": n,
        "total_return": total,
        "annual_return": ann_ret,
        "annual_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "max_dd_peak_idx": pi,
        "max_dd_trough_idx": ti,
        "calmar": calmar,
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "underwater_periods": underwater,
        "longest_dd_periods": longest_dd,
        "skew": float(_safe_skew(r)),
        "kurtosis": float(_safe_kurt(r)),
        "best_period": float(r.max()),
        "worst_period": float(r.min()),
        "nav": nav,
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates and len(dates) == n else None,
    }


def _longest_underwater(dd: np.ndarray) -> int:
    longest = cur = 0
    for v in dd:
        if v < -1e-9:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return longest


def _safe_skew(r: np.ndarray) -> float:
    if len(r) < 3:
        return float("nan")
    s = float(np.std(r, ddof=1))
    if s < 1e-12:
        return float("nan")
    return float(np.mean(((r - r.mean()) / s) ** 3))


def _safe_kurt(r: np.ndarray) -> float:
    if len(r) < 4:
        return float("nan")
    s = float(np.std(r, ddof=1))
    if s < 1e-12:
        return float("nan")
    return float(np.mean(((r - r.mean()) / s) ** 4) - 3.0)


def perf_from_nav(nav, *, dates: list[date] | None = None,
                  periods_per_year: int = 252, risk_free: float = 0.0) -> dict:
    """从净值序列反推收益率再算指标。净值必须为正。"""
    a = _as_array(nav)
    if len(a) < 2:
        return {"n_periods": max(len(a) - 1, 0)}
    with np.errstate(divide="ignore", invalid="ignore"):
        r = a[1:] / np.where(a[:-1] > 0, a[:-1], np.nan) - 1.0
    r = r[np.isfinite(r)]
    out = perf_from_returns(r, dates=dates[1:] if dates else None,
                            periods_per_year=periods_per_year, risk_free=risk_free)
    out["nav"] = a
    return out


def monthly_returns(dates, returns) -> dict[tuple[int, int], float]:
    """按月聚合收益（复利）。用于月度收益热力图。"""
    r = _as_array(returns)
    out: dict[tuple[int, int], float] = {}
    if dates is None or len(dates) != len(r):
        return out
    acc: dict[tuple[int, int], float] = {}
    for d, v in zip(dates, r, strict=False):
        key = (getattr(d, "year", 0), getattr(d, "month", 0))
        acc[key] = acc.get(key, 1.0) * (1.0 + v)
    return {k: v - 1.0 for k, v in acc.items()}


def turnover_from_trades(trades, *, periods: int = 0) -> dict:
    """换手率：成交金额 / 组合净值，按周期平均。

    trades : 可迭代的 (date, amount) 或 dict 列表，含 amount / nav 字段
    """
    if trades is None or len(trades) == 0:
        return {"total_amount": 0.0, "turnover_per_period": 0.0, "n_trades": 0}
    amounts = []
    for t in trades:
        if isinstance(t, dict):
            amounts.append(float(t.get("amount", 0.0)))
        else:
            amounts.append(float(t[1]))
    a = np.asarray(amounts, dtype=float)
    total = float(np.nansum(np.abs(a)))
    return {
        "n_trades": int(len(a)),
        "total_amount": total,
        "turnover_per_period": float(np.nanmean(np.abs(a))) if len(a) else 0.0,
    }


def summary_line(perf: dict) -> str:
    """一行摘要，便于日志/CLI 输出。"""
    def f(k: str, pct: bool = True) -> str:
        v = perf.get(k, float("nan"))
        if v is None or not math.isfinite(v):
            return "  n/a"
        return f"{v*100:7.2f}%" if pct else f"{v:7.2f}"

    return (f"年化 {f('annual_return')} | 波动 {f('annual_vol')} | 夏普 {f('sharpe', False)}"
            f" | 回撤 {f('max_drawdown')} | 胜率 {f('win_rate')}")
