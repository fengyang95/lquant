"""市场环境（regime）+ 情绪阶段（phase）—— 纯函数，只用日线。

## 这一层解决什么

lquant 有市场日报、有板块因子，但没有「当前市场是什么状态 / 情绪走到周期哪
一段」这一层。而「同一个动量因子，在主升里赚钱、在退潮里被反复打脸」这件事，
只有靠**环境元判断**才能提前过滤 —— 因子该不该上线、策略该不该开仓，判据在
这里，不在因子本身。

本模块把这条元判断做成**可审的规则**（阈值集中在两处 dataclass、每一条都写得
出分位依据），而不是一个拍出来的分数。

## 口径来源（MIT，可移植）

设计借鉴 Tick-Stock-Panel（MIT）：
``backend/app/services/regime_builder.py:56-152`` 与
``backend/app/services/market_phase.py:45-86,150-255``。取的是四件事：

1. 5 档状态由 4 个**独立**子分加权（而不是一堆指标平铺相加）；
2. 阈值用**真实分位数**标定，而不是拍整数；
3. inf/nan → 中性 50（既不伪造看多也不伪造看空）；
4. 情绪阶段切换要**平滑 + 确认**，并被大盘弱档一票否决。

**注意**：参考实现内部写死的具体阈值（p15/p85 等）是它自己 universe 的标定
结果，直接搬到 lquant 只会得到「看起来在跑、判据其实是别人的」的静默错误。
所以本模块把它们全部收进 ``RegimeThresholds`` / ``PhaseThresholds``，默认值
标注了来源与含义，**要求调用方用自己的数据重标**。

## 怎么标定（不要跳过这一步）

::

    from dataclasses import replace
    from lquant.market.regime import Component, quantile_band, DEFAULT_REGIME_THRESHOLDS

    band = quantile_band(daily_df["up_ratio"], q_low=0.15, q_high=0.85)
    th = replace(
        DEFAULT_REGIME_THRESHOLDS,
        profit=(Component("up_ratio", band.low, band.high, 0.55), ...),
    )

做法与理由：
- 每个分量取 **p15/p85** 作为 low/high。这样约 70% 的日子落在子分 15~85 之间，
  综合分呈健康的钟形；若用 min/max，一次极端行情（2015 股灾、2024-09 逼空）
  会把全年都压到区间一端，状态档位就失去分辨力。
- **标定窗口要跨牛熊**（参考实现用 2020-08~2026-08，含 2021 结构牛、2022-04
  / 2022-10 两轮底、2024-01 微盘崩、2024-09 逼空）。只用近一年标出来的
  分位数会把「最近这一段」当成常态。
- 标定脚本**一次性运行、不提交**（分位数是数据的函数，不是代码常量）；
  把结果填进调用方的配置，模块默认值只作为兜底起点。
- 5 档切点（70/55/45/30）与分位标定是**两层**：子分区间决定「分数的形状」，
  切点决定「分数落到哪一档」。切点也应按综合分的历史分位定（例如
  strong ≈ p85、weak ≈ p15），本模块给的是参考实现的起点。

## 只用日线

本模块不碰分钟线、不碰盘口：regime 的输入是**逐日截面聚合量**（涨跌家数占比、
中位涨幅、涨停数等），phase 的输入是涨停池派生出的**连板梯队**。两者都只依赖
当日及更早的日线，天然满足 ``as_of`` 语义。

## 防未来函数

一切递推（EMA、连续确认、连板计数）只向**过去**看；``as_of`` 只做「丢掉该日
之后的行」。因此对任意 ``d``，在完整序列上算出的 ≤d 部分，必须与「先把 >d 的
行截掉再算」逐元素一致（前缀不变性，见 tests/unit/test_market_regime.py）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import NamedTuple

import polars as pl

from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "DEFAULT_PHASE_THRESHOLDS",
    "DEFAULT_REGIME_THRESHOLDS",
    "PHASE_CLIMAX",
    "PHASE_EBB",
    "PHASE_ICE",
    "PHASE_IGNITE",
    "PHASE_LABELS",
    "PHASE_PRIORITY",
    "PHASE_RALLY",
    "PHASE_REPAIR",
    "PHASES",
    "POSITIVE_PHASES",
    "REGIME_LABELS",
    "REGIME_STATES",
    "VETO_FALLBACK",
    "VETO_STATES",
    "Band",
    "Component",
    "PhaseThresholds",
    "RegimeResult",
    "RegimeThresholds",
    "classify_phase_series",
    "classify_regime",
    "classify_regime_series",
    "consecutive_limit_ups",
    "ladder_daily",
    "phase_history",
    "quantile_band",
    "resolve_as_of",
    "score_component",
    "score_subscores",
    "valid_state_set",
]

# ───────────────────────────── 通用工具 ─────────────────────────────


class Band(NamedTuple):
    """一个分量的标定区间（分位数）。``invert`` 的分量 high 仍是数值上界。"""

    low: float
    high: float


def _finite(value: object) -> float | None:
    """把输入规约成有限 float；缺失/非有限 → ``None``。

    - ``None`` / ``NaN`` / ``±inf`` 一律视为「不可计算」。停牌补零会让均涨幅
      算出 inf（除以 0 家数），历史上真的因此中断过整段 regime 序列 —— 所以
      这里必须把非有限值当缺失，而不是让它一路冒到 round() 抛 OverflowError。
    - ``0.0`` 是**真值**（0% 涨幅、0 家涨停都可能是事实），绝不能和缺失混为一谈
      （把缺失当 0 会伪造出一个"极度看空"的结论）。
    - 非数值类型（字符串等）是接线错误，fail loudly，不静默吞掉。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，混进来必是接线错误
        raise TypeError(f"regime 分量不接受布尔值：{value!r}")
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise TypeError(f"regime 分量必须是数值或 None，收到 {value!r}") from e
    return v if math.isfinite(v) else None


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 ``linear`` 同口径），零依赖。"""
    if not sorted_xs:
        raise ValueError("分位数需要至少一个样本")
    if q <= 0:
        return sorted_xs[0]
    if q >= 1:
        return sorted_xs[-1]
    pos = q * (len(sorted_xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def quantile_band(
    values: Iterable[object], *, q_low: float = 0.15, q_high: float = 0.85
) -> Band:
    """从历史样本标定一个分量的 ``low/high``（默认 p15/p85）。

    这是「分位标定而非拍阈值」的唯一入口。``None``/``NaN``/``inf`` 直接丢弃
    （它们本来就不该进标定样本）；样本为空 → fail loudly，返回一个编造的区间
    比报错危险得多。
    """
    if not 0 <= q_low < q_high <= 1:
        raise ValueError(f"分位区间非法：q_low={q_low} q_high={q_high}")
    xs = sorted(v for x in values if (v := _finite(x)) is not None)
    if len(xs) < 2:
        raise ValueError(f"标定样本不足（{len(xs)} 个）——分位数不可靠，先补数据")
    return Band(_quantile(xs, q_low), _quantile(xs, q_high))


def resolve_as_of(as_of: date | None) -> date:
    """``as_of`` 的默认值 = 「最新已完成会话」。

    未显式给定日期时用 ``core.sessions.latest_completed_session``：它处理了
    「15:00 前当日 bar 尚未定格」与「周末/长假按会话而非自然日」两件事。
    这里**不自己判断**日期，避免和会话口径分叉。
    """
    if as_of is not None:
        return as_of
    from lquant.core.sessions import latest_completed_session

    return latest_completed_session()


def _require_cols(df: pl.DataFrame, cols: Sequence[str], who: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{who} 缺列 {missing}（现有列：{sorted(df.columns)}）——"
            "缺列时静默按 0/中性计算会产出看似合理但口径错误的结论，故直接报错"
        )


# ───────────────────────────── 一、5 档市场状态 ─────────────────────────────

#: 5 档状态的取值与中文标签。顺序 = 强 → 弱，调用方按 index 比较强弱。
REGIME_STATES: tuple[str, ...] = ("strong", "lean_strong", "range", "lean_weak", "weak")
REGIME_LABELS: dict[str, str] = {
    "strong": "强势",
    "lean_strong": "偏强",
    "range": "震荡",
    "lean_weak": "偏弱",
    "weak": "弱势",
}

def valid_state_set() -> frozenset[str]:
    """本模块认可的大盘档位集合（供调用方校验共享口径）。"""
    return frozenset(REGIME_STATES)


@dataclass(frozen=True)
class Component:
    """一个子分分量：指标 key + 标定区间 + 权重（同一子分内权重和为 1）。

    ``invert=True`` 表示「值越大越差」（如大跌股占比）：先线性映射再取
    ``100 - score``。invert 不等于把 low/high 反过来写 —— 那样在值缺失时
    语义会跟着翻转，缺失必须是恒定的中性 50。
    """

    key: str
    low: float
    high: float
    weight: float
    invert: bool = False


# 分量定义（阈值来源见下方 RegimeThresholds docstring）。
# 权重在同一子分内归一；子分之间另有权重。
_PROFIT: tuple[Component, ...] = (
    Component("up_ratio", 0.21, 0.75, 0.55),
    Component("median_change", -1.2, 1.3, 0.45),
)
_SPECULATION: tuple[Component, ...] = (
    Component("limit_up_count", 35, 97, 0.30),
    Component("seal_ratio", 0.57, 0.75, 0.40),
    Component("max_consecutive", 4, 9, 0.30),
)
_RESILIENCE: tuple[Component, ...] = (
    # 只用「大跌股占比」这一个独立信号，不用跌家数占比：
    # 跌家数占比 = 1 - 涨家数占比，与 profit 的 up_ratio 是**同一信息的正反面**，
    # 两个都进模型等于给「涨跌家数」这条信息双倍权重，后果是分数两极化 ——
    # 好日子更容易冲上 strong、差日子更容易砸到 weak，中间那档（震荡）被挤没。
    # 而震荡区间恰恰是「不该开新仓也不该慌」的常态，丢了它环境层就没用了。
    # 大跌股占比（≤-3%）是独立的尾部信号：普跌但都是小跌 ≠ 恐慌抛售。
    Component("strong_down_ratio", 0.02, 0.18, 1.0, invert=True),
)
_TREND: tuple[Component, ...] = (
    Component("index_change", -2.5, 2.5, 0.50),
    Component("above_ma20_ratio", 0.22, 0.76, 0.50),
)

#: 子分权重。赚钱效应是主导（决定大多数人当天是否赚钱），投机情绪次之
#: （A 股的情绪弹性主要在涨停生态），抗跌与趋势各占两成。
#: 参考实现同款权重；调权重会同时移动 5 档分布，改之前先跑一遍分位统计。
_SUBSCORE_WEIGHTS: dict[str, float] = {
    "profit": 0.35,
    "speculation": 0.25,
    "resilience": 0.20,
    "trend": 0.20,
}


@dataclass(frozen=True)
class RegimeThresholds:
    """5 档状态的全部可配置项（阈值集中在这一处，改这里即可，不散落代码）。

    默认值的**来源与含义**（这是参考实现 2022–2026 A 股真实 p15/p85 的取值，
    搬到 lquant 只是同量纲起点，**必须用 ``quantile_band`` 在自家 universe
    重标**，做法见模块 docstring）：

    - ``up_ratio`` 上涨家数占比（0~1）：p15≈0.21、p85≈0.75。
    - ``median_change`` 全市场中位涨跌幅（百分数，-1.2 表示 -1.2%）：p15/p85。
      这里**只用中位数**，不用均涨幅：两者都在描述「截面中枢」，相关性接近
      0.95，叠加等于给中枢双倍权重；中位数对一字板 / 停复牌等长尾更稳健。
    - ``limit_up_count`` 涨停家数：p15≈35、p85≈97。
    - ``seal_ratio`` 封板率（0~1）：p15≈0.57、p85≈0.75。
    - ``max_consecutive`` 最高连板数：p15≈4、p85≈9。
    - ``strong_down_ratio`` 跌幅 ≤-3% 家数占比（0~1），invert：p15≈0.02、
      p85≈0.18 —— 正常日（<2%）子分满分，大跌日（>18%）子分趋 0。
    - ``index_change`` 基准指数当日涨跌幅（百分数）：对称 ±2.5。
    - ``above_ma20_ratio`` 收盘价在 MA20 上方占比（0~1）：p15≈0.22、p85≈0.76。

    ``strong_cut`` 等 4 个切点按综合分划档：>=70 强势、>=55 偏强、>=45 震荡、
    >=30 偏弱、其余弱势。切点建议同样按综合分历史分位校准（strong≈p85、
    lean_strong≈p70、range≈p50 两侧、weak≈p15）。
    """

    profit: tuple[Component, ...] = _PROFIT
    speculation: tuple[Component, ...] = _SPECULATION
    resilience: tuple[Component, ...] = _RESILIENCE
    trend: tuple[Component, ...] = _TREND
    weights: Mapping[str, float] = field(default_factory=lambda: dict(_SUBSCORE_WEIGHTS))
    strong_cut: float = 70.0
    lean_strong_cut: float = 55.0
    range_cut: float = 45.0
    lean_weak_cut: float = 30.0

    def __post_init__(self) -> None:
        cuts = (self.strong_cut, self.lean_strong_cut, self.range_cut, self.lean_weak_cut)
        if any(cuts[i] <= cuts[i + 1] for i in range(len(cuts) - 1)):
            raise ValueError(f"档位切点必须严格递减，收到 {cuts}")
        for name in ("profit", "speculation", "resilience", "trend"):
            comps = getattr(self, name)
            if not comps:
                raise ValueError(f"子分 {name} 没有任何分量")
            total = sum(c.weight for c in comps)
            if abs(total - 1.0) > 1e-9:
                raise ValueError(f"子分 {name} 的分量权重和为 {total}，必须为 1")
            for c in comps:
                # high <= low 的区间会把该分量永远钉在中性 50（"这一维从不投票"），
                # 是标定失败而不是有效配置 —— 在配置处拦下，别让它静默生效。
                if c.high <= c.low:
                    raise ValueError(f"分量 {c.key} 的标定区间非法：low={c.low} high={c.high}")
                if c.weight <= 0:
                    raise ValueError(f"分量 {c.key} 的权重必须为正，收到 {c.weight}")
        if set(self.weights) != {"profit", "speculation", "resilience", "trend"}:
            raise ValueError(f"子分权重键必须是四个子分名，收到 {sorted(self.weights)}")
        wsum = sum(self.weights.values())
        if abs(wsum - 1.0) > 1e-9:
            raise ValueError(f"子分权重和为 {wsum}，必须为 1")


DEFAULT_REGIME_THRESHOLDS = RegimeThresholds()


def score_component(value: object, low: float, high: float, *, invert: bool = False) -> float:
    """把一个分量线性映射到 0~100 并钳制；缺失/非有限 → **中性 50**。

    - ``high <= low`` 是标定错误，返回 50（退回中性，不炸整段序列），
      但这种配置一定是错的，调用方应通过 ``RegimeThresholds`` 校验拦住它。
    - 缺失取 50 而不是 0：0 分是「最看空」的断言，用它表示缺失等于**伪造
      看空**；50 是「这一维今天没有意见」。
    """
    v = _finite(value)
    if v is None or high <= low:
        return 50.0
    raw = (v - low) / (high - low) * 100.0
    raw = max(0.0, min(100.0, raw))
    return 100.0 - raw if invert else raw


def score_subscores(
    metrics: Mapping[str, object], thresholds: RegimeThresholds = DEFAULT_REGIME_THRESHOLDS
) -> tuple[dict[str, float], tuple[str, ...]]:
    """4 个子分（0~100）+ 落入中性 50 的分量 key 列表。

    缺失分量**不重新归一化权重**：重新归一等于让剩下的维度替缺失维度表态，
    会随「今天缺哪个字段」而改变分数的含义；保留 50 的中性票，结果可解释
    —— 分数被动地向 50 收敛，而且这一点通过返回的 ``neutral`` 是可见的。
    """
    subs: dict[str, float] = {}
    neutral: list[str] = []
    for name in ("profit", "speculation", "resilience", "trend"):
        comps = getattr(thresholds, name)
        acc = 0.0
        for c in comps:
            raw = metrics.get(c.key)
            if _finite(raw) is None:
                neutral.append(c.key)
            acc += score_component(raw, c.low, c.high, invert=c.invert) * c.weight
        subs[name] = acc
    return subs, tuple(neutral)


@dataclass(frozen=True)
class RegimeResult:
    """单日 regime 判定结果（子分保留小数，便于归因"今天分是哪个维度给的"）。"""

    state: str
    label: str
    score: float
    profit: float
    speculation: float
    resilience: float
    trend: float
    neutral: tuple[str, ...]

    @property
    def subscores(self) -> dict[str, float]:
        return {
            "profit": self.profit,
            "speculation": self.speculation,
            "resilience": self.resilience,
            "trend": self.trend,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "label": self.label,
            "score": self.score,
            **self.subscores,
            "neutral": list(self.neutral),
        }


def classify_regime(
    metrics: Mapping[str, object], thresholds: RegimeThresholds = DEFAULT_REGIME_THRESHOLDS
) -> RegimeResult:
    """4 维日线聚合量 → 5 档状态 + 综合分（未取整）。

    ``metrics`` 的字段口径见 ``RegimeThresholds`` docstring；**缺字段即视为
    该分量不可计算**（中性 50），并在结果 ``neutral`` 里列出 —— 缺失是被显式
    记账的，不会静默消失。
    """
    subs, neutral = score_subscores(metrics, thresholds)
    score = sum(subs[k] * thresholds.weights[k] for k in thresholds.weights)
    score = max(0.0, min(100.0, score))
    if score >= thresholds.strong_cut:
        state = "strong"
    elif score >= thresholds.lean_strong_cut:
        state = "lean_strong"
    elif score >= thresholds.range_cut:
        state = "range"
    elif score >= thresholds.lean_weak_cut:
        state = "lean_weak"
    else:
        state = "weak"
    return RegimeResult(
        state=state,
        label=REGIME_LABELS[state],
        score=score,
        profit=subs["profit"],
        speculation=subs["speculation"],
        resilience=subs["resilience"],
        trend=subs["trend"],
        neutral=neutral,
    )


def classify_regime_series(
    daily: pl.DataFrame,
    *,
    thresholds: RegimeThresholds = DEFAULT_REGIME_THRESHOLDS,
    as_of: date | None = None,
) -> pl.DataFrame:
    """逐日 regime 序列。输入列：``trade_date`` + ``RegimeThresholds`` 里的分量列。

    分量列**允许缺失/为空**（该维当天记中性 50，计入 ``neutral_count``）；
    但 ``trade_date`` 缺失即报错 —— 没有日期的序列无法谈 ``as_of``。

    ``as_of`` 语义：只保留 ``trade_date <= as_of`` 的行。默认取
    ``latest_completed_session()``，保证「今天收盘前跑」不会把未定格的当日
    bar 当成完整日线用掉。纯函数本身不做任何时钟判断。
    """
    _require_cols(daily, ("trade_date",), "classify_regime_series")
    cut = resolve_as_of(as_of)
    rows = daily.filter(pl.col("trade_date") <= cut).sort("trade_date")
    if not len(rows):
        return rows.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(c) for c in
             ("profit", "speculation", "resilience", "trend", "score")]
            + [pl.lit(None, dtype=pl.Utf8).alias("state"), pl.lit(None, dtype=pl.Utf8).alias("label"),
               pl.lit(0, dtype=pl.Int32).alias("neutral_count")]
        )
    out = []
    for r in rows.to_dicts():
        res = classify_regime(r, thresholds)
        out.append(
            {
                "trade_date": r["trade_date"],
                "profit": res.profit,
                "speculation": res.speculation,
                "resilience": res.resilience,
                "trend": res.trend,
                "score": res.score,
                "state": res.state,
                "label": res.label,
                "neutral_count": len(res.neutral),
            }
        )
    return pl.DataFrame(out, schema={
        "trade_date": rows.schema["trade_date"],
        "profit": pl.Float64, "speculation": pl.Float64, "resilience": pl.Float64,
        "trend": pl.Float64, "score": pl.Float64,
        "state": pl.Utf8, "label": pl.Utf8, "neutral_count": pl.Int32,
    })


# ───────────────────────────── 二、6 阶段情绪周期 ─────────────────────────────

PHASE_ICE = "ice"
PHASE_IGNITE = "ignite"
PHASE_RALLY = "rally"
PHASE_CLIMAX = "climax"
PHASE_EBB = "ebb"
PHASE_REPAIR = "repair"

PHASE_LABELS: dict[str, str] = {
    PHASE_ICE: "冰点",
    PHASE_IGNITE: "启动",
    PHASE_RALLY: "主升",
    PHASE_CLIMAX: "高潮",
    PHASE_EBB: "退潮",
    PHASE_REPAIR: "修复",
}

#: 日线可得的全部阶段。
PHASES: tuple[str, ...] = (
    PHASE_ICE, PHASE_IGNITE, PHASE_RALLY, PHASE_CLIMAX, PHASE_EBB, PHASE_REPAIR,
)

#: 规则判定优先级（先命中先返回）。顺序是有理由的，不是随手排的：
#: 1. 高潮最极端，先判，避免被"宽度也很大"的主升分支截胡；
#: 2. 冰点必须排在退潮**之前**：长期死寂的市场，梯度确实在低位，但那不是
#:    "自高位退潮"；退潮规则里有一条只看"晋级率+封板率双弱"，顺序反了会把
#:    冰点误标成退潮（参考实现踩过这个坑，注释里记了）；
#: 3. 修复是兜底：所有"想不出更好标签"的日子都归它，宁可模糊也不乱贴。
PHASE_PRIORITY: tuple[str, ...] = (
    PHASE_CLIMAX, PHASE_RALLY, PHASE_ICE, PHASE_EBB, PHASE_IGNITE, PHASE_REPAIR,
)

#: 正面阶段（会被大盘弱档否决的集合）。
POSITIVE_PHASES: frozenset[str] = frozenset({PHASE_CLIMAX, PHASE_RALLY, PHASE_IGNITE})
#: 触发否决的大盘档位。
VETO_STATES: frozenset[str] = frozenset({"weak", "lean_weak"})
#: 被否决后落到哪个标签：修复是"暂不下结论"的兜底档，不可能是正面结论。
VETO_FALLBACK = PHASE_REPAIR


@dataclass(frozen=True)
class PhaseThresholds:
    """6 阶段规则的全部阈值（**集中在这一处**，改这里即可）。

    默认值来源：参考实现用 2020-08~2026-08 全市场约 1454 个交易日的
    **p10 / p20 / p60 / p85 / p90** 分位标定，并对 2024-09/10 逼空→高潮→退潮、
    2024-01/02 微盘退潮两段做过人工抽查。搬到 lquant 后**必须用自家涨停池
    重标**（``ladder_daily`` 产出的列直接喂 ``quantile_band``）：

    - ``climax_*``：高潮 = 情绪极端宣泄，取 p90 的 2~2.5 倍（历史上 <2% 的日子）。
    - ``rally_*``：主升 = 高度/宽度/晋级率同时高于中位（p60），或晋级率极强（p85+）。
    - ``ebb_*``：退潮 = 自近期高位回落且晋级率坍塌（≤p20），或晋级率/封板率双弱。
    - ``ignite_*``：启动 = 宽度/高度自低位扩张（相对 5 日前）且晋级率恢复（≥p55）。
    - ``ice_*``：冰点 = 高度/宽度/首板同时贴地（p10 附近）。
    - ``promo_min_pool``：晋级率的**最小分母**。池子 <10 家时晋级率是纯噪声
      （3 家里 1 家晋级 = 33%），必须记 ``None`` 而不是 0/比率 —— 见
      ``ladder_daily``。
    - ``ema_alpha≈1/3``：等效窗口约 5 个交易日，压掉单日抖动；
    - ``confirm_days=2``：标签切换需要连续 2 日同向，1 日翻转不算数。
    """

    # 高潮
    climax_ge2: float = 50
    climax_first_board: float = 220
    # 主升
    rally_height: float = 7
    rally_ge2: float = 15
    rally_promo: float = 0.23
    rally_promo_alt: float = 0.30
    rally_ge2_alt: float = 12
    rally_height_alt: float = 5
    # 退潮
    ebb_promo: float = 0.15
    ebb_promo_strict: float = 0.13
    ebb_seal: float = 0.57
    ebb_recent_ge2: float = 12
    ebb_recent_height: float = 6
    # 启动
    ignite_ge2_delta: float = 3
    ignite_ge2: float = 8
    ignite_promo: float = 0.20
    ignite_height_delta: float = 1
    ignite_height: float = 5
    ignite_promo_soft: float = 0.19
    # 冰点
    ice_height: float = 4
    ice_ge2: float = 6
    ice_first_board: float = 24
    # 平滑与确认
    ema_alpha: float = 1.0 / 3.0
    confirm_days: int = 2
    #: 判定"自高位回落"时回看的交易日数。
    lookback: int = 5
    #: 晋级率最小分母（家）。
    promo_min_pool: int = 10

    def __post_init__(self) -> None:
        if not 0.0 < self.ema_alpha <= 1.0:
            raise ValueError(f"ema_alpha 必须落在 (0, 1]，收到 {self.ema_alpha}")
        if self.confirm_days < 1:
            raise ValueError(f"confirm_days 至少为 1，收到 {self.confirm_days}")
        if self.promo_min_pool < 1:
            raise ValueError(f"promo_min_pool 至少为 1，收到 {self.promo_min_pool}")


DEFAULT_PHASE_THRESHOLDS = PhaseThresholds()

#: classify_phase_series 必需的梯队列。seal_rate 允许整列缺失（见
#: ladder_daily 的说明），rest 必须给出。
_REQUIRED_LADDER_COLS = (
    "trade_date", "max_consecutive", "first_board", "ge2_count", "promo_rate",
)

_LADDER_SCHEMA: dict[str, pl.DataType] = {
    "trade_date": pl.Date,
    "max_consecutive": pl.Int64,
    "first_board": pl.Int64,
    "ge2_count": pl.Int64,
    "ge3_count": pl.Int64,
    "ge5_count": pl.Int64,
    "rungs_filled": pl.Int64,
    "ladder_completeness": pl.Float64,
    "promo_pool": pl.Int64,
    "promo_ok": pl.Int64,
    "promo_rate": pl.Float64,
    "pool_size": pl.Int64,
    "seal_rate": pl.Float64,
}


def _empty_ladder() -> pl.DataFrame:
    return pl.DataFrame(schema=_LADDER_SCHEMA)


def _prev_session_map(days: pl.DataFrame) -> pl.DataFrame:
    """交易日 → 上一会话（用池表自身的日期集合当会话序列的代理）。

    **为什么可以这么做**：``limit_up_pool`` 每个交易日收盘后采一次，它的
    日期集合就是"有采集的交易日"序列。拿它当会话代理，不需要另接交易日历
    （本模块要保持零外部依赖的纯函数）。

    **代价（已知偏差）**：某天整池漏采 → 那天不出现在序列里 → 跨过它的连板
    会被判为"断档"，连板高度被**低估**。方向是保守的（不会凭空造出高情绪），
    但会让那几天的梯队偏冷。要修就得传入真实交易日历；日线平台上整池漏采
    本身就是需要告警的事故（``limit_up_pool`` 采集是 ``critical=True``）。
    """
    return (
        days.select("trade_date")
        .unique(maintain_order=True)
        .sort("trade_date")
        .with_columns(pl.col("trade_date").shift(1).alias("_prev_session"))
    )


def consecutive_limit_ups(pool: pl.DataFrame) -> pl.DataFrame:
    """从 ``limit_up_pool`` 派生 ``(trade_date, symbol, consecutive_limit_ups)``。

    ``limit_up_pool`` 同一张表里装了**两份**采集：涨停池与炸板池
    （``market/collectors/__init__.py`` 把 ``broken_pool`` 也落到这张表）。
    炸板行没有 ``limit_up_type``（``fetch_broken_pool`` 不产出该列 → 入库
    为 NULL），所以**按 ``limit_up_type IS NOT NULL`` 过滤**才是"真涨停"。
    不滤的话「盘中涨停但收盘没封住」会被算成涨停日，连板数系统性虚高。

    连板判定必须**相邻会话**，不能只看日期差：周一和周五对同一只票不算连板。
    因此这里用池表日期集合当会话序列（见 ``_prev_session_map``）。

    返回空输入 → 返回空表（上层据此得到空序列）；非空输入却过滤不出任何真
    涨停行 → 报错（那是"涨停池没采到"，与"今天没有涨停"是相反的结论）。
    """
    _require_cols(pool, ("trade_date", "symbol", "limit_up_type"), "consecutive_limit_ups")
    if not len(pool):
        return pl.DataFrame(schema={
            "trade_date": pool.schema["trade_date"], "symbol": pl.Utf8,
            "consecutive_limit_ups": pl.Int64,
        })
    real = pool.filter(pl.col("limit_up_type").is_not_null()).select(
        "trade_date", "symbol"
    ).unique()
    if not len(real):
        raise ValueError(
            "limit_up_pool 里全是炸板行（limit_up_type 全为 NULL）——"
            "涨停池采集缺失。此时按 0 家涨停计算会得到'冰点'这种完全相反的结论，"
            "故直接报错；请先补齐涨停池采集"
        )
    d = real.sort(["symbol", "trade_date"]).with_columns(
        pl.col("trade_date").shift(1).over("symbol").alias("_prev_hit")
    )
    d = d.join(_prev_session_map(real), on="trade_date", how="left")
    # 与前一次上榜相邻会话才算连续；否则开新的一段。
    d = d.with_columns(
        (pl.col("_prev_hit").is_not_null()
         & pl.col("_prev_hit").eq(pl.col("_prev_session"))).alias("_cont")
    )
    # 段号：每条断档处 +1，然后组内行号就是连板数。
    d = d.with_columns(
        (~pl.col("_cont")).cast(pl.Int32).cum_sum().over("symbol").alias("_run")
    )
    d = d.with_columns(
        pl.col("trade_date").cum_count().over(["symbol", "_run"]).cast(pl.Int64)
        .alias("consecutive_limit_ups")
    )
    return d.select("trade_date", "symbol", "consecutive_limit_ups").sort(
        ["trade_date", "symbol"]
    )


def ladder_daily(
    pool: pl.DataFrame,
    *,
    thresholds: PhaseThresholds = DEFAULT_PHASE_THRESHOLDS,
    as_of: date | None = None,
) -> pl.DataFrame:
    """涨停池 → 逐日连板梯队（阶段规则的唯一输入）。

    产出列（全部**纯日线可得**）：

    ==================  ====================================================
    max_consecutive     连板高度：当日最高连板数
    first_board         首板宽度：1 连板家数
    ge2_count / ge3 / ge5  N 板以上家数
    rungs_filled        已填档位数（2..height 中真实存在连板股的档位个数）
    ladder_completeness 梯队完整度 = rungs_filled / (height-1)，height<3 记 0
    promo_pool/promo_ok 晋级率分子分母（分母 = 上一会话涨停家数）
    promo_rate          晋级率；分母 < promo_min_pool → **None**（不是 0）
    seal_rate           封板率 = 真涨停行数 / 当日池内总行数
    ==================  ====================================================

    ``seal_rate`` 的**已知口径缺陷**（必须知道）：池表里"当日无炸板"和"炸板池
    漏采"都会得到 1.0，无法区分。所以它只作为退潮规则 B 的辅助条件（还必须
    晋级率同时低于阈值），**不单独驱动**阶段切换。

    ``promo_rate`` 在分母过小时记 ``None``：3 家里 1 家晋级 = 33%，这种数字
    进 EMA 会把整个阶段判定带偏；``None`` 会被 EMA 前向沿用（视为"没观察到
    新信息"），而不是当成 0（那是"全部炸板"的断言）。
    """
    cut = resolve_as_of(as_of)
    _require_cols(pool, ("trade_date", "symbol", "limit_up_type"), "ladder_daily")
    pool = pool.filter(pl.col("trade_date") <= cut)
    streak = consecutive_limit_ups(pool)
    if not len(streak):
        return _empty_ladder()

    # ── 梯队计数 ──
    agg = streak.group_by("trade_date").agg(
        pl.col("consecutive_limit_ups").max().alias("max_consecutive"),
        pl.col("consecutive_limit_ups").eq(1).sum().alias("first_board"),
        pl.col("consecutive_limit_ups").ge(2).sum().alias("ge2_count"),
        pl.col("consecutive_limit_ups").ge(3).sum().alias("ge3_count"),
        pl.col("consecutive_limit_ups").ge(5).sum().alias("ge5_count"),
        pl.col("consecutive_limit_ups").filter(pl.col("consecutive_limit_ups") >= 2)
        .n_unique().alias("rungs_filled"),
        pl.len().alias("pool_size"),
    ).sort("trade_date")

    # ── 晋级率：昨日在池 & 今日仍在池且板数 +1 ──
    s = streak.sort(["symbol", "trade_date"]).with_columns(
        pl.col("trade_date").shift(1).over("symbol").alias("_prev_hit"),
        pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("_prev_consec"),
    ).join(_prev_session_map(streak), on="trade_date", how="left")
    s = s.with_columns(
        (pl.col("_prev_hit").is_not_null()
         & pl.col("_prev_hit").eq(pl.col("_prev_session"))
         & pl.col("_prev_consec").add(1).eq(pl.col("consecutive_limit_ups"))).alias("_ok")
    )
    promo = s.group_by("trade_date").agg(pl.col("_ok").sum().alias("promo_ok"))
    # 分母 = **上一会话**的涨停家数：先给当天挂上"上一会话是哪天"，再按那一天
    # 去取 pool_size。顺序反了（直接 join 自己的 trade_date）会取到当天的池子
    # 家数，晋级率算出 24/32 这种既不解释得通、又会静默压低启动判定的数字。
    prev_size = agg.select(pl.col("trade_date"), pl.col("pool_size").alias("promo_pool"))
    promo = (
        promo.join(_prev_session_map(streak), on="trade_date", how="left")
        .join(prev_size, left_on="_prev_session", right_on="trade_date", how="left")
        .select("trade_date", "promo_pool", "promo_ok")
    )

    out = agg.join(promo, on="trade_date", how="left")

    # ── 封板率（含炸板行；口径缺陷见 docstring）──
    total = pool.group_by("trade_date").agg(pl.len().alias("_n"))
    real_n = streak.group_by("trade_date").agg(pl.len().alias("_real"))
    seal = total.join(real_n, on="trade_date", how="left").with_columns(
        pl.when(pl.col("_n") > 0)
        .then(pl.col("_real").cast(pl.Float64) / pl.col("_n"))
        .otherwise(None)
        .alias("seal_rate")
    ).select("trade_date", "seal_rate")
    out = out.join(seal, on="trade_date", how="left")

    out = out.with_columns(
        # 小样本晋级率 → None。这是本模块最重要的"缺失 ≠ 0"的口径之一。
        pl.when(pl.col("promo_pool") >= thresholds.promo_min_pool)
        .then(pl.col("promo_ok").cast(pl.Float64) / pl.col("promo_pool"))
        .otherwise(None)
        .alias("promo_rate"),
        pl.when(pl.col("max_consecutive") >= 3)
        .then(
            pl.col("rungs_filled").cast(pl.Float64)
            / (pl.col("max_consecutive") - 1)
        )
        .otherwise(0.0)
        .alias("ladder_completeness"),
    )
    return out.select(list(_LADDER_SCHEMA)).cast(_LADDER_SCHEMA)


def _ema(values: Sequence[float | None], alpha: float) -> list[float | None]:
    """因果 EMA（α≈1/3 → 等效窗口约 5 日）。

    缺失沿用上一平滑值（"没有新信息"），而不是插值或当 0；序列**开头**的缺失
    用首个有效平滑值前向回填 —— 那些位置的 None 无法参与阈值比较，会让规则
    整体退化成兜底阶段。回填值本身仍只来自序列内部，不引入未来信息。
    全为缺失 → 全 None（不伪造 0）。
    """
    out: list[float | None] = []
    cur: float | None = None
    for v in values:
        if v is None or not math.isfinite(v):
            out.append(cur)
            continue
        cur = v if cur is None else cur + alpha * (v - cur)
        out.append(cur)
    first = next((i for i, x in enumerate(out) if x is not None), None)
    if first is None:
        return out
    return [out[first] if x is None else x for x in out]


def _ge(v: float | None, t: float) -> bool:
    return v is not None and v >= t


def _le(v: float | None, t: float) -> bool:
    return v is not None and v <= t


def _raw_phase(
    i: int,
    height: Sequence[float | None],
    first: Sequence[float | None],
    ge2: Sequence[float | None],
    promo: Sequence[float | None],
    seal: Sequence[float | None],
    th: PhaseThresholds,
) -> str:
    """单日原始阶段标签（未平滑确认、未否决），按 ``PHASE_PRIORITY`` 顺序命中。

    传入的是**平滑后**的驱动量；"自 X 日前回落/扩张"用 ``lookback`` 相对比较。
    """
    h, fb, g2 = height[i], first[i], ge2[i]
    pr, sr = promo[i], seal[i]
    j = max(0, i - th.lookback)
    g2_prev, h_prev = ge2[j], height[j]

    # 1. 高潮：极端宣泄（p90 的 2 倍以上），历史上 <2% 的日子。
    if _ge(g2, th.climax_ge2) or _ge(fb, th.climax_first_board):
        return PHASE_CLIMAX
    # 2. 主升：高度/宽度/晋级率同时站上中位；或晋级率极强（p85+）时的替代路径。
    if _ge(h, th.rally_height) and _ge(g2, th.rally_ge2) and _ge(pr, th.rally_promo):
        return PHASE_RALLY
    if (_ge(pr, th.rally_promo_alt) and _ge(g2, th.rally_ge2_alt)
            and _ge(h, th.rally_height_alt)):
        return PHASE_RALLY
    # 3. 冰点：高度/宽度/首板同时贴地。必须早于退潮（见 PHASE_PRIORITY 注释）。
    if _le(h, th.ice_height) and _le(g2, th.ice_ge2) and _le(fb, th.ice_first_board):
        return PHASE_ICE
    # 4. 退潮：(a) 自近期高位回落且晋级率坍塌；(b) 晋级率/封板率双弱。
    from_high = _ge(g2_prev, th.ebb_recent_ge2) or _ge(h_prev, th.ebb_recent_height)
    if from_high and _le(pr, th.ebb_promo) and (g2 is not None and g2_prev is not None
                                                and g2 < g2_prev):
        return PHASE_EBB
    if _le(pr, th.ebb_promo_strict) and _le(sr, th.ebb_seal):
        return PHASE_EBB
    # 5. 启动：自低位扩张（相对 lookback 日前）且晋级率恢复。
    if (g2 is not None and g2_prev is not None and g2 - g2_prev >= th.ignite_ge2_delta
            and _ge(g2, th.ignite_ge2) and _ge(pr, th.ignite_promo)):
        return PHASE_IGNITE
    if (h is not None and h_prev is not None and h - h_prev >= th.ignite_height_delta
            and _ge(h, th.ignite_height) and _ge(pr, th.ignite_promo_soft)):
        return PHASE_IGNITE
    # 6. 修复：兜底。所有"说不清"的日子归这里，宁可模糊也不乱贴。
    return PHASE_REPAIR


def classify_phase_series(
    daily: pl.DataFrame,
    *,
    states: Mapping[date, str] | None = None,
    thresholds: PhaseThresholds = DEFAULT_PHASE_THRESHOLDS,
    as_of: date | None = None,
) -> pl.DataFrame:
    """逐日情绪阶段（6 档），含平滑、连续确认与大盘弱档否决。

    输入：``ladder_daily`` 的输出（``trade_date`` / ``max_consecutive`` /
    ``first_board`` / ``ge2_count`` / ``promo_rate``，``seal_rate`` 可选）。

    处理链（**顺序即语义**）：

    1. 各驱动量做 EMA 平滑（α≈1/3）；
    2. 按优先级出**原始**标签；
    3. 大盘弱档：原始标签是正面阶段且当日 state ∈ {weak, lean_weak} → 降级为
       修复（连态机不往正面锁存）；
    4. 连续 ``confirm_days`` 日同标签才切换（1 日噪声不算数）；
    5. **否决终审**：确认后的标签若仍是正面阶段而当日大盘弱，硬降为修复。
       第 3 步已经拦了一道，这里再兜一道是因为滞后确认可能让昨天的正面标签
       多活一天 —— 而"正面阶段不允许出现在弱档日"是**硬不变式**，优先级高于
       平滑带来的迟滞。

    **平滑挡不住的唯一情形（已知边界，别当它不存在）**：``confirm_days`` 挡的
    是"原始标签只持续 1 天"。而 EMA 有尾巴 —— 一个**远离基线**的单日尖峰会把
    平滑后的驱动量拖高约 ``lookback`` 天，"启动"的 ``g2 - g2_prev >= 3`` 因此
    连续为真，原始标签本身就持续了多天，确认机制无从拦截。要收紧这条路，调小
    ``ema_alpha``（1/3 → 1/5）或抬高 ``ignite_ge2_delta``。当前行为已在
    ``tests/unit/test_market_regime.py::test_ema_tail_is_a_known_single_day_spike_channel``
    锁定。

    ``states``：``date -> 5 档 state`` 的映射（``classify_regime_series`` 的
    ``state`` 列即可）。**不给就不启用否决**（并记账在 ``state_known`` 列）：
    在没有大盘信息时硬判"弱"是伪造结论。给了映射但某日不在其中，同样按
    "未知"处理并记账 —— 需要这条不变式的调用方必须覆盖整个窗口。

    输出列：``phase`` / ``phase_label`` / ``state`` / ``state_known`` 之外，
    ``vetoed`` 标出"这一天原本被判为正面阶段、但被大盘弱档打掉"的日子
    （判定同时看原始未否决标签与确认后标签，两条被否决的路径都算），
    便于调用方审计否决到底改写了哪几天。
    """
    _require_cols(daily, _REQUIRED_LADDER_COLS, "classify_phase_series")
    cut = resolve_as_of(as_of)
    rows = daily.filter(pl.col("trade_date") <= cut).sort("trade_date")
    if "seal_rate" not in rows.columns:
        rows = rows.with_columns(pl.lit(None, dtype=pl.Float64).alias("seal_rate"))
    if "ladder_completeness" not in rows.columns:
        rows = rows.with_columns(pl.lit(None, dtype=pl.Float64).alias("ladder_completeness"))
    if not len(rows):
        return rows.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("phase"),
            pl.lit(None, dtype=pl.Utf8).alias("phase_label"),
            pl.lit(None, dtype=pl.Utf8).alias("state"),
            pl.lit(False).alias("vetoed"),
            pl.lit(False).alias("state_known"),
        )

    dates = rows["trade_date"].to_list()
    height = _ema([_finite(v) for v in rows["max_consecutive"].to_list()], thresholds.ema_alpha)
    first = _ema([_finite(v) for v in rows["first_board"].to_list()], thresholds.ema_alpha)
    ge2 = _ema([_finite(v) for v in rows["ge2_count"].to_list()], thresholds.ema_alpha)
    promo = _ema([_finite(v) for v in rows["promo_rate"].to_list()], thresholds.ema_alpha)
    seal = _ema([_finite(v) for v in rows["seal_rate"].to_list()], thresholds.ema_alpha)

    row_states = [states.get(d) if states else None for d in dates]
    known = [states is not None and d in states for d in dates]

    def is_positive(i: int, label: str | None) -> bool:
        return label is not None and label in POSITIVE_PHASES and row_states[i] in VETO_STATES

    # 未否决的原始标签（留着记账：没有它就无法区分"本来就不正面"和"被否决了"）
    raw_open = [_raw_phase(i, height, first, ge2, promo, seal, thresholds)
                for i in range(len(dates))]
    # 第 3 步：预防性否决 —— 让连态机压根不往正面锁存
    raw = [VETO_FALLBACK if is_positive(i, lab) else lab
           for i, lab in enumerate(raw_open)]

    labels: list[str] = []
    current: str | None = None
    pending: str | None = None
    run = 0
    for lab in raw:
        if current is None:
            current = lab
            labels.append(current)
            continue
        if lab == current:
            labels.append(current)
            pending, run = None, 0
            continue
        if lab == pending:
            run += 1
        else:
            pending, run = lab, 1
        if run >= thresholds.confirm_days:
            current = lab
            labels.append(current)
            pending, run = None, 0
        else:
            labels.append(current)

    # 否决终审：把确认滞后保留下来的正面标签也打掉（硬不变式）。
    # vetoed 记账同时看"原始未否决标签"与"确认后标签"，两种被否决的路径都算数。
    veto_flags = [
        is_positive(i, raw_open[i]) or is_positive(i, labels[i])
        for i in range(len(dates))
    ]
    final = [VETO_FALLBACK if veto_flags[i] else labels[i] for i in range(len(dates))]
    return rows.with_columns(
        pl.Series("phase", final, dtype=pl.Utf8),
        pl.Series("phase_label", [PHASE_LABELS[p] for p in final], dtype=pl.Utf8),
        pl.Series("state", row_states, dtype=pl.Utf8),
        pl.Series("vetoed", veto_flags, dtype=pl.Boolean),
        pl.Series("state_known", known, dtype=pl.Boolean),
    ).select(
        "trade_date", "phase", "phase_label", "state", "vetoed", "state_known",
        "max_consecutive", "first_board", "ge2_count", "promo_rate", "seal_rate",
        "ladder_completeness",
    )


def phase_history(
    pool: pl.DataFrame,
    *,
    states: Mapping[date, str] | None = None,
    thresholds: PhaseThresholds = DEFAULT_PHASE_THRESHOLDS,
    as_of: date | None = None,
) -> pl.DataFrame:
    """``limit_up_pool`` 一表直达阶段序列（``ladder_daily`` + ``classify_phase_series``）。

    拆开两步的理由是让调用方能单独看梯队原始量（归因"为什么今天被标成退潮"），
    这里只是最常用的组合。
    """
    ladder = ladder_daily(pool, thresholds=thresholds, as_of=as_of)
    return classify_phase_series(ladder, states=states, thresholds=thresholds, as_of=as_of)
