"""因子评级：把一堆数字收敛成「Strong / Moderate / Weak + 为什么」。

IC、ICIR、单调性、多空夏普各有各的话说；单独看任何一个都会误判 ——
ICIR 高但分组不单调，往往是几只极端股撑起来的；
单调性好但多空夏普低，可能只是收益全被交易成本吃掉了。
评级把这几条**同时**摆上台面，并且把「差在哪一条」显式说出来。

判定逻辑（阈值可配置，见 ``config/factors/rating.yaml``）：

- **strong**：|ICIR| ≥ 0.5 且 分组单调 |ρ| ≥ 0.8 且 多空夏普 ≥ 1.0
  且在多重检验校正后仍显著（n_trials 越大，显著线越高）。
- **moderate**：|ICIR| ≥ 0.3 或 多空夏普 ≥ 0.5。
- **weak**：以上都不满足，或 |IC| 低于噪声线（< 0.02）。

一次搜索里评过 N 个候选，显著性标准就该按 ``sqrt(2·ln N)`` 抬高 ——
「前 100 次试验里的显著」和「第 500 次之后的显著」不是一回事。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

__all__ = ["RatingThresholds", "load_thresholds", "factor_rating"]

RATING_ORDER = {"weak": 0, "moderate": 1, "strong": 2}


@dataclass(frozen=True)
class RatingThresholds:
    """评级阈值。改这里 = 改全平台口径，所以宁可显式写在配置文件里。"""

    ic_strong: float = 0.03          # |IC| 强信号线
    ic_moderate: float = 0.02        # |IC| 有效线（A 股日频，别拿 0.05 当及格线）
    icir_strong: float = 0.5
    icir_moderate: float = 0.3
    ls_sharpe_strong: float = 1.0
    ls_sharpe_moderate: float = 0.5
    monotonicity_strong: float = 0.8
    monotonicity_moderate: float = 0.5
    t_stat_strong: float = 0.0       # >0 时作为附加的 t 门槛（与校正门槛取较大者）

    @classmethod
    def from_dict(cls, d: dict | None) -> RatingThresholds:
        if not d:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in d.items() if k in known})


def load_thresholds(path: str | None = None) -> RatingThresholds:
    """从 ``config/factors/rating.yaml`` 读阈值，缺文件/缺字段回退默认值。

    配置缺失不是错误 —— 评级该照跑，只是用默认口径。所以这里不抛异常。
    """
    try:
        if path is None:
            from lquant.core.config import load_yaml

            raw = load_yaml("factors/rating.yaml")
        else:
            from pathlib import Path

            import yaml

            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return RatingThresholds()
    return RatingThresholds.from_dict((raw or {}).get("rating") or raw)


def _f(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if math.isfinite(x) else float("nan")


def factor_rating(ic_summary: dict, quantile: dict | None = None, *,
                  thresholds: RatingThresholds | None = None,
                  n_trials: int | None = None) -> dict:
    """按 IC/单调性/多空夏普/显著性给一个评级。

    Parameters
    ----------
    ic_summary : ``evaluate.ic.ic_summary(...)`` 的输出（含 ``ic`` / ``rank_ic``）
    quantile : ``evaluate.quantile.quantile_summary(...)`` 的输出，缺省时只按 IC 评级
    thresholds : 阈值，缺省读配置
    n_trials : 该因子的候选试验次数 —— 用于抬高显著性门槛（多重检验校正）

    Notes
    -----
    优先用 **RankIC** 口径：A 股极端值多，Pearson IC 容易被少数妖股抬高。
    """
    th = thresholds or load_thresholds()
    ic = (ic_summary or {}).get("ic") or {}
    rank = (ic_summary or {}).get("rank_ic") or {}
    src = "rank_ic" if rank else "ic"
    base = rank or ic

    icir = _f(base.get("ir"))
    ic_mean = _f(base.get("mean"))
    # IC 序列零方差（完美因子）时 IR 在数学上无定义（除以 0），但它的含义是
    # 「稳定到没有波动」，不是「没数据」。这种情况按无穷大处理 ——
    # 否则最强的那类因子反而会卡在 moderate，评级就反向了。
    # 零方差判定用 STD_EPS：polars 常数序列的 std 是 ~7e-18 而非 0。
    from lquant.factors.evaluate.ic import STD_EPS

    if (not math.isfinite(icir) and abs(_f(base.get("std"))) < STD_EPS
            and math.isfinite(ic_mean) and ic_mean != 0.0):
        icir = math.copysign(math.inf, ic_mean)
    t_nw = _f(base.get("t_stat_nw"))
    if not math.isfinite(t_nw):
        t_nw = _f(base.get("t_stat"))

    ls = (quantile or {}).get("long_short") or {}
    ls_sharpe = _f(ls.get("sharpe"))
    mono = _f((quantile or {}).get("monotonicity"))

    # 多重检验校正门槛：n_trials 越大越难「显著」
    t_thr = 0.0
    if n_trials and n_trials >= 2:
        from lquant.factors.mining.fitness import corrected_threshold

        t_thr = corrected_threshold(n_trials)
    t_thr = max(t_thr, th.t_stat_strong)
    significant = True if t_thr <= 0 else bool(math.isfinite(t_nw) and abs(t_nw) >= t_thr)

    reasons: list[str] = []
    blockers: list[str] = []

    # ── strong 的每一条都必须过 ──
    strong_ok = True
    # NaN 的比较天然为 False（IEEE 语义），所以这里不需要额外的 isfinite 前置 ——
    # 加了反而会把「IR 取到 ∞ 的完美因子」判成不达标。
    if not (abs(icir) >= th.icir_strong):
        strong_ok = False
        blockers.append(f"|ICIR|={_p(icir)} < {th.icir_strong}（稳定性不够）")
    if quantile is not None and not (abs(mono) >= th.monotonicity_strong):
        strong_ok = False
        blockers.append(f"分组单调性 {_p(mono)} < {th.monotonicity_strong}（可能是尾部筛选而非排序）")
    if quantile is not None and not (abs(ls_sharpe) >= th.ls_sharpe_strong):
        strong_ok = False
        blockers.append(f"多空夏普 {_p(ls_sharpe)} < {th.ls_sharpe_strong}")
    if not significant:
        strong_ok = False
        blockers.append(f"|t|={_p(t_nw)} < 校正门槛 {t_thr:.2f}（n_trials={n_trials}）")
    if not (abs(ic_mean) >= th.ic_strong):
        strong_ok = False
        blockers.append(f"|IC|={_p(ic_mean)} < 强信号线 {th.ic_strong}")

    # ── 满分必要条件：IC 不能是噪声 ──
    noise = not (abs(ic_mean) >= th.ic_moderate)

    if strong_ok:
        rating = "strong"
        reasons.append(f"|ICIR|={_p(icir)} ≥ {th.icir_strong}，"
                       f"|IC|={_p(ic_mean)} ≥ {th.ic_strong}，稳定性与强度同时达标")
        if quantile is not None:
            reasons.append(f"分组单调 {_p(mono)}，多空夏普 {_p(ls_sharpe)}")
    elif not noise and (abs(icir) >= th.icir_moderate
                        or abs(ls_sharpe) >= th.ls_sharpe_moderate):
        rating = "moderate"
        if abs(icir) >= th.icir_moderate:
            reasons.append(f"|ICIR|={_p(icir)} ≥ {th.icir_moderate}")
        if abs(ls_sharpe) >= th.ls_sharpe_moderate:
            reasons.append(f"|多空夏普| {_p(ls_sharpe)} ≥ {th.ls_sharpe_moderate}")
        if not significant:
            reasons.append("显著性未过校正门槛 —— 最多只能给到 moderate")
    else:
        rating = "weak"
        if noise:
            reasons.append(f"|IC|={_p(ic_mean)} 低于有效线 {th.ic_moderate} —— 噪声区间")
        elif math.isfinite(icir):
            reasons.append(f"|ICIR|={_p(icir)} 与 |多空夏普| {_p(ls_sharpe)} 都不够")

    return {
        "rating": rating,
        "score": RATING_ORDER[rating],
        "source": src,
        "ic_mean": ic_mean,
        "icir": icir,
        "t_stat_nw": t_nw,
        "t_threshold": t_thr,
        "significant": significant,
        "n_trials": n_trials,
        "monotonicity": mono,
        "ls_sharpe": ls_sharpe,
        "reasons": reasons,
        "blockers": blockers,
        "thresholds": asdict(th),
    }


def _p(v: float) -> str:
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    return f"{v:.4f}" if isinstance(v, float) and math.isfinite(v) else "n/a"
