"""关键价位指标集（纯日线、无未来函数）。

口径来源
--------
9 类价位（:data:`LEVEL_TYPES` 里 Keltner 拆成短/中/长三个显隐开关，故共 11 个
key）的**分类与参数档位**移植自 tick-stock-panel（MIT，
``backend/app/indicators/levels.py``）：成交密集区 / 枢轴点 / 前高前低 /
布林带 / Keltner 三档 / ATR 波动通道 / 未回补缺口 / 斐波那契 / 整数关口。
代码按 lquant 的注册表约定**重写**，并补上三条参考实现没有的约束：

1. **纯函数**：没有 IO、没有缓存、没有全局状态，输入什么算什么；
2. **无未来函数**：每个价位只由「当日及之前」的 bar 决定（逐类给出理由，
   见各 ``_xxx`` 的注释）。这条不是保险，而是 ``levels`` 会进逐行指标链，
   任何一处 center 窗口都会让回测提前看到价位；
3. **结构化输出**：:class:`LevelSet` 同时给扁平列表与「按现价上下分组」
   视图，前端画线 / 信号消费都不用再解析 dict。

为什么拆成 ``compute_levels`` / ``add_levels`` 两个入口
----------------------------------------------------
注册表（:mod:`lquant.indicators.registry`）的契约是「逐行、行数不变的
DataFrame → DataFrame」，而关键价位的自然形态是**某一天的快照**。
把 9 类价位硬塞进逐行列，会在每一行重算一遍筹码分布与缺口扫描，代价 O(n²)。
因此分成两层：

* :func:`compute_levels` —— 快照 API，一次算全 9 类，返回 :class:`LevelSet`，
  给前端 K 线画线与信号/研报直接消费；
* :func:`add_levels` —— 注册进指标表的**逐行**版本，只给可向量化的核心价位
  （枢轴 + 前高前低 + 通道 + ATR 带）的「最近支撑/压力」，供因子与图表链路
  逐行消费，并必须通过前缀不变性门禁。

两层共用同一套参数常量与 :func:`_side` 判据，避免「同一个价位两个口径」。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = [
    "LEVEL_TYPES",
    "SIDES",
    "STRENGTHS",
    "LevelSet",
    "PriceLevel",
    "add_levels",
    "atr",
    "compute_levels",
]

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

#: 价位方向。``support`` = 现价下方、``resistance`` = 现价上方、
#: ``neutral`` = 与现价重合（枢轴位恰好落在现价上时会命中）。
SIDES = ("support", "resistance", "neutral")

#: 强度档位：strong / medium / weak，前端据此决定线型（实线/虚线/点线）。
STRENGTHS = ("strong", "medium", "weak")

#: 价位分组 key → 中文展示名。前端按 key 做显隐开关，key 是稳定契约。
LEVEL_TYPES: dict[str, str] = {
    "sr": "成交密集区",
    "pivot": "枢轴点",
    "extreme": "前高前低",
    "boll": "布林带",
    "keltner_s": "Keltner短期",
    "keltner_m": "Keltner中期",
    "keltner_l": "Keltner长期",
    "atr": "ATR波动通道",
    "gap": "缺口位",
    "fib": "斐波那契",
    "round": "整数关口",
}

#: 价格类数值统一保留 4 位小数：A 股报价是 2 位，但斐波那契/ATR/分箱中点
#: 并非最小报价单位整数倍，压到 2 位会把相邻价位合并成一个（点数变少）。
_PRICE_DP = 4

#: 缺列时抛错的**核心**价格列。volume 是可选列（缺了只跳过成交密集区）。
_CORE_PRICE_COLS = ("high", "low")

#: ``add_levels`` 的逐行输出列。控制在 8 列内 —— 前端价格轴最多叠 8 条线
#: （``web/src/app/security/[symbol]/page.tsx`` 的 ``MAX_OVERLAY_LINES``）。
_ADD_LEVEL_OUTPUTS = (
    "lvl_pivot",
    "lvl_pivot_r1",
    "lvl_pivot_s1",
    "lvl_support_1",
    "lvl_support_2",
    "lvl_resistance_1",
    "lvl_resistance_2",
)


# ------------------------------------------------------------------
# 输出结构
# ------------------------------------------------------------------


@dataclass(frozen=True)
class PriceLevel:
    """单个价位点。

    Attributes:
        value: 价格。
        label: 中文展示标签（如「压力位 R1」）。
        type: 分组 key，见 :data:`LEVEL_TYPES`。
        side: 相对**现价**的方向，见 :data:`SIDES`。
        strength: 强度档位，见 :data:`STRENGTHS`。
        rank: 同一 ``type`` 内按「距现价由近到远」的序号，1 = 最近。
        tier: 仅枢轴点有 —— 0 = P，1/2/3 = R1/R2/R3 与 S1/S2/S3 档位。
        detail: 人类可读的口径说明（窗口、来源），便于复核与展示。
    """

    value: float
    label: str
    type: str
    side: str
    strength: str = "medium"
    rank: int = 1
    tier: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.type not in LEVEL_TYPES:
            raise ValueError(f"未知价位分组 {self.type!r}，可选: {tuple(LEVEL_TYPES)}")
        if self.side not in SIDES:
            raise ValueError(f"未知价位方向 {self.side!r}，可选: {SIDES}")
        if self.strength not in STRENGTHS:
            raise ValueError(f"未知强度档位 {self.strength!r}，可选: {STRENGTHS}")
        if not math.isfinite(float(self.value)):
            raise ValueError(f"价位必须是有限数，得到 {self.value!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "type": self.type,
            "type_label": LEVEL_TYPES[self.type],
            "side": self.side,
            "strength": self.strength,
            "rank": self.rank,
            "tier": self.tier,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LevelSet:
    """某个 as-of 时刻的关键价位快照。

    ``supports`` / ``resistances`` 就是「按现价上下分组」的结果，且组内已按
    距现价由近到远排序（``supports[0]`` 即最近支撑），前端可直接取用。
    """

    close: float | None = None
    as_of: date | None = None
    levels: tuple[PriceLevel, ...] = ()
    notes: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", tuple(self.levels))
        object.__setattr__(self, "notes", tuple(self.notes))

    def __len__(self) -> int:
        return len(self.levels)

    @property
    def supports(self) -> tuple[PriceLevel, ...]:
        return tuple(lv for lv in self.levels if lv.side == "support")

    @property
    def resistances(self) -> tuple[PriceLevel, ...]:
        return tuple(lv for lv in self.levels if lv.side == "resistance")

    @property
    def neutral(self) -> tuple[PriceLevel, ...]:
        return tuple(lv for lv in self.levels if lv.side == "neutral")

    def by_type(self, level_type: str) -> tuple[PriceLevel, ...]:
        return tuple(lv for lv in self.levels if lv.type == level_type)

    def nearest(self, side: str, n: int = 1) -> tuple[PriceLevel, ...]:
        """某一侧的最近 n 个价位（``side`` ∈ support/resistance）。"""
        if side not in ("support", "resistance"):
            raise ValueError(f"side 只能是 support/resistance，得到 {side!r}")
        pool = self.supports if side == "support" else self.resistances
        return pool[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "close": self.close,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "levels": [lv.to_dict() for lv in self.levels],
            "supports": [lv.to_dict() for lv in self.supports],
            "resistances": [lv.to_dict() for lv in self.resistances],
            "neutral": [lv.to_dict() for lv in self.neutral],
            "by_type": {t: [lv.to_dict() for lv in self.by_type(t)] for t in LEVEL_TYPES},
            "notes": list(self.notes),
            "params": dict(self.params),
        }


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------


def _num(v: Any) -> float | None:
    """尽力转成有限 float；null/NaN/±Inf/非数值一律 None（不抛）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v: float) -> float:
    return round(float(v), _PRICE_DP)


def _side(value: float, close: float, tol: float = 1e-9) -> str:
    """价位相对现价的方向。

    为什么不用「类型决定方向」（如「缺口一定是支撑」）：同一类价位在不同价位
    区间既可能是支撑也可能是压力 —— 向下缺口在现价上方就是压力。以现价为唯一
    基准，前端「现价上方全画绿线、下方全画红线」的渲染逻辑才自洽。
    """
    if abs(value - close) <= tol * max(1.0, abs(close)):
        return "neutral"
    return "resistance" if value > close else "support"


def _require_columns(df: pl.DataFrame, cols: Iterable[str], what: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{what}: 缺少必需列 {missing}；现有列 {list(df.columns)}")


def _aggregate(values: Sequence[float], pct: float) -> list[float]:
    """把相近（相差 ≤ ``pct``）的价位聚成一个，取组均值。

    为什么必须聚合：swing 高低点在震荡区会密集重复，不聚合会让同一价位占满
    「最近 N 个」的名额，把真正有区分度的价位挤出去。
    """
    if not values or pct <= 0:
        return sorted(values)
    out: list[float] = []
    group: list[float] = []
    for v in sorted(values):
        if group and v > group[0] * (1 + pct):
            out.append(sum(group) / len(group))
            group = []
        group.append(v)
    if group:
        out.append(sum(group) / len(group))
    return out


def _assign_rank(levels: Sequence[PriceLevel], close: float) -> tuple[PriceLevel, ...]:
    """组内（同一 type）按距现价由近到远编号，再整体按距离排序。"""
    buckets: dict[str, list[PriceLevel]] = {}
    for lv in levels:
        buckets.setdefault(lv.type, []).append(lv)
    ranked: list[PriceLevel] = []
    for items in buckets.values():
        items.sort(key=lambda x: abs(x.value - close))
        ranked.extend(replace(lv, rank=i) for i, lv in enumerate(items, start=1))
    ranked.sort(key=lambda x: abs(x.value - close))
    return tuple(ranked)


def _prepare(df: pl.DataFrame, price_col: str) -> tuple[pl.DataFrame, list[str]]:
    """类型归一 + 剔除无效价格行，并按 trade_date 升序。

    **fail-loudly**：核心价格列缺失直接抛 :class:`ValueError`（静默返回空集
    会让调用方以为「今天没有价位」而不是「列名写错了」）。行级脏数据（null/
    非正/Inf）只剔除并记 note —— 停牌日、除权缺口行都长这样，不该中断计算。
    """
    _require_columns(df, (*_CORE_PRICE_COLS, price_col), "compute_levels")
    notes: list[str] = []
    cast = [c for c in ("high", "low", price_col, "volume") if c in df.columns]
    clean = df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in cast])
    before = clean.height
    clean = clean.filter(
        pl.col("high").is_not_null() & pl.col("low").is_not_null()
        & pl.col(price_col).is_not_null()
        & pl.col("high").is_finite() & pl.col("low").is_finite()
        & pl.col(price_col).is_finite()
        & (pl.col("high") > 0) & (pl.col("low") > 0) & (pl.col(price_col) > 0)
    )
    dropped = before - clean.height
    if dropped:
        notes.append(f"剔除 {dropped} 行无效价格（null / 非正 / 非有限）")
    if "trade_date" in clean.columns:
        clean = clean.sort("trade_date")
    elif clean.height > 1:
        notes.append("缺少 trade_date 列：按输入行序视为时间升序")
    return clean, notes


# ------------------------------------------------------------------
# 各价位类型
# ------------------------------------------------------------------


def _volume_profile(
    clean: pl.DataFrame,
    close: float,
    *,
    price_col: str,
    bins: int,
    top_n: int,
    half_life: float | None,
) -> tuple[list[PriceLevel], list[str]]:
    """成交密集区 / Volume Profile。

    口径来源：tick-stock-panel ``levels.py::_support_resistance`` 的「筹码分布」
    思路 —— 以 ``[low, high]`` 区间的**分箱成交量**找持仓成本密集带，POC（控制点）
    是成交量最大的桶，即最多人持仓的成本区。

    与参考实现的**两点差异**（都是显式取舍，不是遗漏）：

    1. 参考实现用逐日换手率做**顺序衰减**（``chips *= (1-turnover)``），必须
       一格一格迭代，O(n×bins) 且强依赖 ``turnover_rate`` 列。这里改成纯
       group_by 的向量化分箱；需要「近期成交更重要」时用 ``half_life`` 施加
       可向量化的时间半衰期权重。lquant 日线里 ``turnover_rate`` 是可缺列，
       依赖它会让同一函数在不同数据源上给出不同点位。
    2. 每根 bar 的成交量按 ``[low, high]`` 覆盖到的**所有桶**均摊（而不是全塞
       中点），与参考实现一致 —— 振幅大的 K 线不该污染中点价。
    """
    notes: list[str] = []
    if "volume" not in clean.columns:
        return [], ["成交密集区跳过：缺少 volume 列"]
    if clean.height < 20:
        return [], [f"成交密集区跳过：有效样本 {clean.height} < 20 根"]

    sub = clean.filter(pl.col("volume") > 0)
    if sub.height < 20:
        return [], [f"成交密集区跳过：成交量 > 0 的样本仅 {sub.height} 根"]

    lo = float(sub["low"].min())
    hi = float(sub["high"].max())
    if not (hi > lo > 0):
        return [], ["成交密集区跳过：价格区间退化（high ≤ low）"]

    step = (hi - lo) / bins
    sub = sub.with_columns(pl.int_range(pl.len()).alias("_i"))
    if half_life and half_life > 0:
        # 越近的 bar 权重越大；半衰期用「bar 数」而非日历天，停牌不额外衰减。
        weight = pl.col("volume") * pl.lit(0.5).pow(
            (sub.height - 1 - pl.col("_i")) / float(half_life)
        )
    else:
        weight = pl.col("volume")

    expanded = (
        sub.with_columns(
            ((pl.col("low") - lo) / step).floor().clip(0, bins - 1)
            .cast(pl.Int32).alias("_kl"),
            ((pl.col("high") - lo) / step).floor().clip(0, bins - 1)
            .cast(pl.Int32).alias("_kh"),
            weight.alias("_w"),
        )
        .with_columns(
            (pl.col("_w") / (pl.col("_kh") - pl.col("_kl") + 1)).alias("_share")
        )
        .with_columns(pl.int_ranges(pl.col("_kl"), pl.col("_kh") + 1).alias("_b"))
        .explode("_b")
    )
    agg = expanded.group_by("_b").agg(pl.col("_share").sum().alias("chips")).sort("_b")
    ids = [int(k) for k in agg["_b"].to_list()]
    chips = [float(v) for v in agg["chips"].to_list()]
    if not ids:
        return [], ["成交密集区跳过：分箱后无成交"]

    mean_chips = sum(chips) / len(chips)
    order = sorted(range(len(ids)), key=lambda i: chips[i], reverse=True)

    def mid(bin_id: int) -> float:
        return lo + (bin_id + 0.5) * step

    out: list[PriceLevel] = []
    poc = order[0]
    out.append(PriceLevel(
        value=_r(mid(ids[poc])), label="成交密集区(POC)", type="sr",
        side=_side(mid(ids[poc]), close), strength="strong",
        detail=f"{bins} 分箱 POC，区间 [{_r(lo)}, {_r(hi)}]",
    ))
    for i in order[1:]:
        if len(out) >= top_n:
            break
        if chips[i] > mean_chips:
            out.append(PriceLevel(
                value=_r(mid(ids[i])), label="成交密集区", type="sr",
                side=_side(mid(ids[i]), close), strength="medium",
                detail=f"成交高于分箱均值 {_r(mean_chips)}",
            ))
    return out, notes


def _pivot_points(clean: pl.DataFrame, close: float, *, price_col: str
                  ) -> tuple[list[PriceLevel], list[str]]:
    """经典枢轴点：P = (H+L+C)/3，R1~R3 / S1~S3。

    **口径**：以 as-of **当日**的 HLC 计算（与参考实现一致），给下一交易日参考。
    经典定义里也有「用前一日 HLC 定今日枢轴」的用法，那等价于调用方先 ``shift(1)``
    再传进来 —— 不在这里做隐含 shift，否则快照口径与 ``add_levels`` 会分叉。
    """
    if clean.height < 1:
        return [], ["枢轴点跳过：无有效 bar"]
    h = float(clean["high"][-1])
    lo = float(clean["low"][-1])
    c = _num(clean[price_col][-1])
    if c is None:
        return [], ["枢轴点跳过：当日收盘价无效"]
    p = (h + lo + c) / 3
    raw = [
        (p, "枢轴位 P", "neutral", "strong", 0),
        (2 * p - lo, "压力位 R1", "resistance", "medium", 1),
        (p + (h - lo), "压力位 R2", "resistance", "medium", 2),
        (h + 2 * (p - lo), "压力位 R3", "resistance", "weak", 3),
        (2 * p - h, "支撑位 S1", "support", "medium", 1),
        (p - (h - lo), "支撑位 S2", "support", "medium", 2),
        (lo - 2 * (h - p), "支撑位 S3", "support", "weak", 3),
    ]
    # side 一律相对现价：跳空高开时 R1 会落到现价下方、真的在起支撑作用，
    # 此时把它归到 support 才与前端「现价上方绿线 / 下方红线」的分组自洽；
    # label 仍保留 R/S 的公式语义，两者不冲突。
    return [
        PriceLevel(value=_r(v), label=label, type="pivot", side=_side(v, close),
                   strength=strength, tier=tier, detail="经典 Pivot，(H+L+C)/3 基准")
        for v, label, _side_hint, strength, tier in raw
    ], []


def _extremes(
    clean: pl.DataFrame,
    close: float,
    *,
    windows: Sequence[int],
    swing_win: int,
    swing_top: int,
) -> tuple[list[PriceLevel], list[str]]:
    """前高 / 前低：n 日极值 + 已确认的 swing 转折点。

    **无未来函数**：swing 点用居中窗口找，但只保留「右侧已有 ``swing_win`` 根
    bar」的点 —— 转折点必须被后续走势确认才算成立。窗口右端那 ``swing_win`` 根
    里可能藏着尚未确认的转折点，必须剔除，否则等于用未来数据画支撑压力。
    """
    notes: list[str] = []
    out: list[PriceLevel] = []
    for n in windows:
        if clean.height < n:
            notes.append(f"前高前低跳过 {n} 日极值：有效样本 {clean.height} < {n}")
            continue
        sub = clean.tail(n)
        hi = _num(sub["high"].max())
        lo = _num(sub["low"].min())
        if hi is not None:
            out.append(PriceLevel(value=_r(hi), label=f"{n}日新高", type="extreme",
                                  side=_side(hi, close), strength="strong",
                                  detail=f"近 {n} 个交易日最高价"))
        if lo is not None:
            out.append(PriceLevel(value=_r(lo), label=f"{n}日新低", type="extreme",
                                  side=_side(lo, close), strength="strong",
                                  detail=f"近 {n} 个交易日最低价"))

    if clean.height <= 2 * swing_win + 1:
        notes.append(f"前高前低跳过 swing：有效样本 {clean.height} ≤ {2 * swing_win + 1}")
        return out, notes

    n = clean.height
    span = 2 * swing_win + 1
    idx = pl.int_range(pl.len())
    confirmed = idx <= (n - 1 - swing_win)
    mask = clean.select(
        ((pl.col("high") == pl.col("high").rolling_max(span, center=True,
                                                        min_samples=span))
         & confirmed).alias("is_hi"),
        ((pl.col("low") == pl.col("low").rolling_min(span, center=True,
                                                      min_samples=span))
         & confirmed).alias("is_lo"),
    )
    is_hi = mask["is_hi"].to_list()
    is_lo = mask["is_lo"].to_list()

    for vals, flags, label, side_name, cmp in (
        (clean["high"].to_list(), is_hi, "前高", "resistance", "above"),
        (clean["low"].to_list(), is_lo, "前低", "support", "below"),
    ):
        pts = [float(v) for v, ok in zip(vals, flags, strict=True) if ok and _num(v)]
        pts = _aggregate(pts, 0.01)
        if cmp == "above":
            pts = [v for v in pts if v > close * 1.001]
        else:
            pts = [v for v in pts if v < close * 0.999]
        pts.sort(key=lambda v: abs(v - close))
        for v in pts[:swing_top]:
            out.append(PriceLevel(value=_r(v), label=label, type="extreme",
                                  side=side_name, strength="medium",
                                  detail=f"±{swing_win} 根窗口确认的 swing 转折点"))
    return out, notes


def _band_levels(
    clean: pl.DataFrame,
    close: float,
    *,
    price_col: str,
    ma_window: int,
    mult: float,
    level_type: str,
    name: str,
    atr_period: int,
) -> tuple[list[PriceLevel], list[str]]:
    """单档通道：均线 ± mult × ATR（复合型 Keltner 通道）。"""
    if clean.height < max(ma_window, atr_period) + 1:
        return [], [f"{name}跳过：有效样本 {clean.height} < {max(ma_window, atr_period) + 1}"]
    ma = _num(clean[price_col].rolling_mean(ma_window)[-1])
    atr_v = _num(atr(clean, atr_period, price_col=price_col)[f"atr{atr_period}"][-1])
    if ma is None or atr_v is None:
        return [], [f"{name}跳过：均线或 ATR 为 null"]
    upper, lower = ma + mult * atr_v, ma - mult * atr_v
    detail = f"MA{ma_window} ± {mult}×ATR{atr_period}"
    return [
        PriceLevel(value=_r(upper), label=f"{name}通道上轨", type=level_type,
                   side=_side(upper, close), detail=detail),
        PriceLevel(value=_r(lower), label=f"{name}通道下轨", type=level_type,
                   side=_side(lower, close), detail=detail),
    ], []


def _atr_expr(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """ATR 表达式（供 :func:`atr` 与 :func:`add_levels` 共用同一口径）。"""
    if n < 1:
        raise ValueError(f"ATR 窗口必须是正整数，得到 {n}")
    prev_close = close.shift(1)
    tr = pl.max_horizontal(high - low, (high - prev_close).abs(),
                           (low - prev_close).abs())
    return tr.ewm_mean(alpha=1.0 / n, adjust=False, min_samples=n)


def atr(df: pl.DataFrame, n: int = 14, *, price_col: str = "close") -> pl.DataFrame:
    """Wilder ATR 列（列名 ``atr{n}``）。

    ``TR = max(H-L, |H-prevC|, |L-prevC|)``，再取 ``alpha = 1/n`` 的
    ``ewm_mean(adjust=False)``。为什么用 Wilder 递归而不是简单均值：通达信/
    同花顺的 ATR 都是递归平滑，用 SMA 会让通道宽度与软件显示的系统性不一致。

    已知偏差：严格 Wilder 用前 n 个 TR 的 SMA 做种子，这里直接用全序列
    ``adjust=False`` 递归，差异随 n 根之后迅速衰减（阈值/通道用途下可忽略）。
    """
    _require_columns(df, ("high", "low", price_col), "atr")
    expr = _atr_expr(pl.col("high"), pl.col("low"), pl.col(price_col), n)
    return df.with_columns(expr.alias(f"atr{n}"))


def _atr_band(
    clean: pl.DataFrame, close: float, *, price_col: str, mults: Sequence[float],
    period: int,
) -> tuple[list[PriceLevel], list[str]]:
    """close ± n×ATR 的动态波动带（中性「轨道」措辞，不含操作指令）。"""
    if clean.height < period + 1:
        return [], [f"ATR 波动通道跳过：有效样本 {clean.height} < {period + 1}"]
    atr_v = _num(atr(clean, period, price_col=price_col)[f"atr{period}"][-1])
    if atr_v is None:
        return [], ["ATR 波动通道跳过：ATR 为 null"]
    out: list[PriceLevel] = []
    for m in sorted({float(x) for x in mults}, reverse=True):
        strength = "medium" if float(m).is_integer() else "weak"
        out.append(PriceLevel(
            value=_r(close + m * atr_v), label=f"ATR 上轨(+{m:g})", type="atr",
            side="resistance", strength=strength,
            detail=f"close + {m:g}×ATR{period}"))
        out.append(PriceLevel(
            value=_r(close - m * atr_v), label=f"ATR 下轨(-{m:g})", type="atr",
            side="support", strength=strength,
            detail=f"close - {m:g}×ATR{period}"))
    return out, []


def _gap_levels(
    clean: pl.DataFrame, close: float, *, lookback: int, top_n: int, agg_pct: float,
) -> tuple[list[PriceLevel], list[str]]:
    """未回补的跳空缺口。

    定义（与参考实现代码一致）：
      - 向上缺口：``low[i] > high[i-1]``，缺口区间 ``(high[i-1], low[i])``；
      - 向下缺口：``high[i] < low[i-1]``，缺口区间 ``(high[i], low[i-1])``。

    **回补判定**：缺口形成后，只要有任何一根后续 K 线的价格区间与缺口真空带
    有重叠（``low ≤ 缺口上沿 且 high ≥ 缺口下沿``），即视为已回补 —— 价格已经
    进过这个真空区，它不再是「空白」。参考实现 docstring 写的是「完全覆盖」，
    但代码就是重叠判定；两者对「K 线落在缺口内部」这一情形结论不同，这里按
    代码语义实现，并在测试里锁死。

    只扫描 ``lookback`` 根以内的缺口，且回补检查也只看这段窗口 —— 更早的缺口
    即使未回补，对当下交易的参考价值也已被更近的价位覆盖。
    """
    if clean.height < 5:
        return [], [f"缺口位跳过：有效样本 {clean.height} < 5"]
    sub = clean.tail(lookback) if clean.height > lookback else clean
    highs = sub["high"].to_list()
    lows = sub["low"].to_list()
    up: list[tuple[int, float, float]] = []
    down: list[tuple[int, float, float]] = []
    for i in range(1, len(highs)):
        h_prev, l_prev = _num(highs[i - 1]), _num(lows[i - 1])
        h, l = _num(highs[i]), _num(lows[i])
        if None in (h_prev, l_prev, h, l):
            continue
        if l > h_prev:
            up.append((i, h_prev, l))
        elif h < l_prev:
            down.append((i, h, l_prev))

    def unfilled(gaps: list[tuple[int, float, float]]) -> list[float]:
        mids: list[float] = []
        for i, g_lo, g_hi in gaps:
            hit = any(lows[j] <= g_hi and highs[j] >= g_lo
                      for j in range(i + 1, len(highs)))
            if not hit:
                mids.append((g_lo + g_hi) / 2)
        return _aggregate(mids, agg_pct)

    out: list[PriceLevel] = []
    for mids, label, note in ((unfilled(up), "向上缺口", "跳空高开且未被回补"),
                              (unfilled(down), "向下缺口", "跳空低开且未被回补")):
        mids.sort(key=lambda v: abs(v - close))
        for v in mids[:top_n]:
            out.append(PriceLevel(value=_r(v), label=label, type="gap",
                                  side=_side(v, close), detail=note))
    if not out:
        out_note = [f"缺口位：近 {min(lookback, clean.height)} 根内无未回补缺口"]
        return [], out_note
    return out, []


def _fib_levels(
    clean: pl.DataFrame, close: float, *, window: int, ratios: Sequence[float],
) -> tuple[list[PriceLevel], list[str]]:
    """斐波那契回撤：近 ``window`` 根波段的高低点之间插回撤位。

    方向由「高低点谁更晚出现」决定：高点更晚 = 上涨波段（从高点向下回撤），
    低点更晚 = 下跌波段（从低点向上反弹）。用 ``arg_max/arg_min`` 的位置而不是
    「现价离谁近」来判方向 —— 后者会把「上涨后回落但仍在高位」误判成下跌波段。
    """
    if clean.height < 10:
        return [], [f"斐波那契跳过：有效样本 {clean.height} < 10"]
    sub = clean.tail(window) if clean.height > window else clean
    hi_pos = sub["high"].arg_max()
    lo_pos = sub["low"].arg_min()
    if hi_pos is None or lo_pos is None:
        return [], ["斐波那契跳过：波段高低点为 null"]
    hi = _num(sub["high"][hi_pos])
    lo = _num(sub["low"][lo_pos])
    if hi is None or lo is None or hi <= lo:
        return [], ["斐波那契跳过：波段高低点退化"]

    rng = hi - lo
    up_trend = hi_pos > lo_pos
    out: list[PriceLevel] = []
    for r in ratios:
        v = hi - rng * r if up_trend else lo + rng * r
        out.append(PriceLevel(
            value=_r(v), label=f"Fib {r * 100:g}%", type="fib", side=_side(v, close),
            detail=f"近 {sub.height} 根{'上涨' if up_trend else '下跌'}波段 "
                   f"[{_r(lo)}, {_r(hi)}] 回撤"))
    return out, []


def _round_levels(
    close: float, *, pct: float, max_count: int,
) -> tuple[list[PriceLevel], list[str]]:
    """现价附近的整数关口（心理支撑/压力）。

    步长按价格量级自适应（低价股 0.5 元一格、高价股 50 元一格）；距现价 < 1%
    的整数位过滤掉 —— 太近的点位没有区分度，只会把「最近压力」占满。
    """
    if close <= 0:
        return [], ["整数关口跳过：现价非正"]
    if close < 10:
        step = 0.5
    elif close < 20:
        step = 1.0
    elif close < 100:
        step = 5.0
    elif close < 500:
        step = 10.0
    else:
        step = 50.0
    lo_b, hi_b = close * (1 - pct), close * (1 + pct)
    start = math.ceil(lo_b / step) * step
    cands: list[float] = []
    v = start
    while v <= hi_b:
        if v > 0:
            cands.append(v)
        v += step
    cands.sort(key=lambda x: abs(x - close))
    out: list[PriceLevel] = []
    for v in cands:
        if len(out) >= max_count:
            break
        if abs(v - close) / close < 0.01:
            continue
        out.append(PriceLevel(value=_r(v), label=f"整数关口 {v:g}", type="round",
                              side=_side(v, close), strength="weak",
                              detail=f"{step:g} 元为一格"))
    return out, []


def _boll_levels(clean: pl.DataFrame, close: float, *, price_col: str, n: int, k: float
                 ) -> tuple[list[PriceLevel], list[str]]:
    """布林带（MA_n ± kσ）—— 统计波动边界，不是真实成交支撑压力。"""
    if clean.height < n:
        return [], [f"布林带跳过：有效样本 {clean.height} < {n}"]
    mid = _num(clean[price_col].rolling_mean(n)[-1])
    std = _num(clean[price_col].rolling_std(n)[-1])
    if mid is None or std is None:
        return [], ["布林带跳过：中轨或标准差为 null"]
    detail = f"MA{n} ± {k}×σ{n}"
    return [
        PriceLevel(value=_r(mid + k * std), label="布林上轨", type="boll",
                   side=_side(mid + k * std, close), detail=detail),
        PriceLevel(value=_r(mid), label="布林中轨", type="boll",
                   side=_side(mid, close), detail=f"MA{n} 多空平衡线"),
        PriceLevel(value=_r(mid - k * std), label="布林下轨", type="boll",
                   side=_side(mid - k * std, close), detail=detail),
    ], []


# ------------------------------------------------------------------
# 快照 API
# ------------------------------------------------------------------


def compute_levels(
    df: pl.DataFrame,
    *,
    price_col: str = "close",
    bins: int = 40,
    vp_top_n: int = 3,
    vp_half_life: float | None = None,
    extreme_windows: Sequence[int] = (60, 250),
    swing_win: int = 5,
    swing_top: int = 2,
    gap_lookback: int = 120,
    gap_top: int = 3,
    fib_window: int = 120,
    fib_ratios: Sequence[float] = (0.236, 0.382, 0.5, 0.618, 0.786),
    boll_n: int = 20,
    boll_k: float = 2.0,
    keltner_bands: Sequence[tuple[str, str, int, float]] = (
        ("keltner_s", "短期", 20, 2.0),
        ("keltner_m", "中期", 60, 2.5),
        ("keltner_l", "长期", 120, 3.0),
    ),
    atr_period: int = 14,
    atr_mults: Sequence[float] = (1.5, 2.0),
    round_pct: float = 0.10,
    round_max: int = 8,
) -> LevelSet:
    """计算单标的日线在某一天（= 最后一根 bar）的关键价位快照。

    Args:
        df: 单标的日线，升序列（``trade_date/open/high/low/close/volume``）。
            只用到当日及之前的 bar；缺 ``volume`` 只跳过成交密集区。
        price_col: 现价列，默认 ``close``。
        bins / vp_top_n / vp_half_life: 成交密集区分箱数 / 最多产出点数 /
            时间半衰期（None = 按成交量纯累加）。
        extreme_windows: 前高前低的极值窗口（根）。
        swing_win / swing_top: swing 转折点确认窗口 / 每侧最多点数。
        gap_lookback / gap_top: 缺口扫描回看根数 / 每方向最多点数。
        fib_window / fib_ratios: 斐波那契波段窗口 / 回撤比率。
        boll_n / boll_k: 布林带窗口 / 倍数。
        keltner_bands: ``(type_key, 中文名, 均线窗口, ATR 倍数)`` 列表。
        atr_period / atr_mults: ATR 周期 / 波动通道倍数。
        round_pct / round_max: 整数关口现价 ± 比例 / 最多点数。

    Returns:
        :class:`LevelSet`。数据不足时**不抛异常**：对应类型被跳过，原因写进
        ``notes``；核心价格列缺失则抛 :class:`ValueError`（见 :func:`_prepare`）。
    """
    params: dict[str, Any] = {
        "price_col": price_col, "bins": bins, "vp_top_n": vp_top_n,
        "vp_half_life": vp_half_life, "extreme_windows": tuple(extreme_windows),
        "swing_win": swing_win, "swing_top": swing_top,
        "gap_lookback": gap_lookback, "gap_top": gap_top,
        "fib_window": fib_window, "fib_ratios": tuple(fib_ratios),
        "boll_n": boll_n, "boll_k": boll_k,
        "keltner_bands": tuple(keltner_bands), "atr_period": atr_period,
        "atr_mults": tuple(atr_mults), "round_pct": round_pct, "round_max": round_max,
    }
    clean, notes = _prepare(df, price_col)
    if clean.is_empty():
        notes.append("有效价格行为 0（可能全为 null / 非正价），返回空价位集")
        return LevelSet(close=None, as_of=None, levels=(), notes=tuple(notes),
                        params=params)

    close = _num(clean[price_col][-1])
    if close is None:
        notes.append("末行收盘价无效，返回空价位集")
        return LevelSet(close=None, as_of=None, levels=(), notes=tuple(notes),
                        params=params)
    # 先按输出精度定死现价：side / rank / 点位都基于同一个 close，
    # 否则「未取整的现价」与「取整后暴露给调用方的 close」会让边界价位
    # 出现「显示在现价下方却标 resistance」这类自相矛盾。
    close = _r(close)
    as_of = clean["trade_date"][-1] if "trade_date" in clean.columns else None

    collectors = (
        lambda: _volume_profile(clean, close, price_col=price_col, bins=bins,
                                top_n=vp_top_n, half_life=vp_half_life),
        lambda: _pivot_points(clean, close, price_col=price_col),
        lambda: _extremes(clean, close, windows=extreme_windows,
                          swing_win=swing_win, swing_top=swing_top),
        lambda: _boll_levels(clean, close, price_col=price_col, n=boll_n, k=boll_k),
        lambda: _atr_band(clean, close, price_col=price_col, mults=atr_mults,
                          period=atr_period),
        lambda: _gap_levels(clean, close, lookback=gap_lookback, top_n=gap_top,
                            agg_pct=0.005),
        lambda: _fib_levels(clean, close, window=fib_window, ratios=fib_ratios),
        lambda: _round_levels(close, pct=round_pct, max_count=round_max),
    )
    raw: list[PriceLevel] = []
    for collect in collectors:
        got, why = collect()
        raw.extend(got)
        notes.extend(why)
    for _key, name, ma_window, mult in keltner_bands:
        got, why = _band_levels(clean, close, price_col=price_col,
                                ma_window=ma_window, mult=mult, level_type=_key,
                                name=name, atr_period=atr_period)
        raw.extend(got)
        notes.extend(why)

    # 同一价位可能被多类口径命中（如 ATR 上轨恰好是整数关口）——按数值去重，
    # 保留先出现的（先出现 = 口径优先级更高：筹码/枢轴 > 通道 > 心理位）。
    seen: set[tuple[str, float]] = set()
    deduped: list[PriceLevel] = []
    for lv in raw:
        key = (lv.type, lv.value)
        if key not in seen:
            seen.add(key)
            deduped.append(lv)

    # side 统一用「取整后的价位 vs 取整后的现价」重算：各 builder 内部可能拿未取整的
    # 中间量判方向，浮点噪声会让 side 与最终暴露的 value 自相矛盾（value 明明等于
    # close 却标成 resistance）。在这里收口，保证 supports/resistances 的分组与
    # value 严格一致 —— 前端「现价上方绿线、下方红线」的渲染依赖这条不变量。
    deduped = [replace(lv, side=_side(lv.value, close)) for lv in deduped]

    return LevelSet(close=close, as_of=as_of,
                    levels=_assign_rank(deduped, close),
                    notes=tuple(notes), params=params)


# ------------------------------------------------------------------
# 逐行注册指标
# ------------------------------------------------------------------


def _candidate_pool(
    hi: pl.Expr, lo: pl.Expr, c: pl.Expr, atr_col: pl.Expr,
    *, bands: Sequence[int], ma_window: int, boll_k: float, atr_mult: float,
) -> list[pl.Expr]:
    """逐行「可能成为支撑/压力」的价位候选池（全部只用当日及之前的数据）。"""
    p = (hi + lo + c) / 3
    pool: list[pl.Expr] = [
        p, 2 * p - lo, 2 * p - hi,                     # 枢轴 P / R1 / S1
        p + (hi - lo), p - (hi - lo),                  # R2 / S2
        hi + 2 * (p - lo), lo - 2 * (hi - p),          # R3 / S3
    ]
    for n in bands:
        pool.append(hi.rolling_max(n))                 # n 日新高
        pool.append(lo.rolling_min(n))                 # n 日新低
    ma = c.rolling_mean(ma_window)
    std = c.rolling_std(ma_window)
    pool.extend([ma + boll_k * std, ma - boll_k * std,
                 ma + 2.0 * atr_col, ma - 2.0 * atr_col,
                 c + atr_mult * atr_col, c - atr_mult * atr_col])
    return pool


def _hmin(exprs: Sequence[pl.Expr]) -> pl.Expr:
    """行内最小值（忽略 null）。

    **为什么不用 ``pl.min_horizontal``**：polars 1.44 在同一次横向归约里所有
    参数都恒为 null 时（例如全 null 价、或短到所有 rolling 窗口都不满足的帧），
    会把结果折叠成长度 1 的标量，``with_columns`` 随即抛
    ``Series ... doesn't match the DataFrame height``。``concat_list`` + ``list.min``
    语义完全相同（同样忽略 null、全 null 给 null）且不会退化。
    """
    return pl.concat_list(list(exprs)).list.min()


def _hmax(exprs: Sequence[pl.Expr]) -> pl.Expr:
    """行内最大值（忽略 null）。理由见 :func:`_hmin`。"""
    return pl.concat_list(list(exprs)).list.max()


@register_indicator("levels", label="关键价位", category="channel", pane="price",
                    min_window=60, inputs=("high", "low", "close", "volume"),
                    outputs=_ADD_LEVEL_OUTPUTS)
def add_levels(
    df: pl.DataFrame,
    *,
    price_col: str = "close",
    bands: Sequence[int] = (20, 60, 250),
    ma_window: int = 20,
    boll_k: float = 2.0,
    atr_period: int = 14,
    atr_mult: float = 2.0,
) -> pl.DataFrame:
    """逐行关键价位：枢轴 P/R1/S1 + 最近支撑/压力的前两档。

    为什么只给这几列而不是全部 9 类：注册表契约是**逐行**的，而成交密集区
    （需要筹码分箱）、缺口回补（需要向前扫描）、swing 确认（需要右侧窗口）
    都不是逐行可向量化的形态 —— 硬塞进来会让每一行重算一遍全表，退化成 O(n²)。
    全量分类请用 :func:`compute_levels` 快照。这里的候选池已覆盖枢轴、20/60/250
    日前高前低、布林带与 ATR 带，够支撑「最近压力/支撑」的信号消费。

    **无未来函数**：滚动窗口（``rolling_max/min/mean/std``）、``shift``、
    ``ewm_mean(adjust=False)`` 与横向 min/max 都只依赖当日及之前，前缀不变性
    由 ``tests/unit/test_indicators_levels.py`` 门禁锁死。

    ``price_col`` 之后的关键字参数在注册表里不体现，前端按默认档调用。
    """
    _require_columns(df, ("high", "low", price_col), "add_levels")
    hi = pl.col("high").cast(pl.Float64, strict=False)
    lo = pl.col("low").cast(pl.Float64, strict=False)
    c = pl.col(price_col).cast(pl.Float64, strict=False)
    atr_col = _atr_expr(hi, lo, c, atr_period)

    pool = _candidate_pool(hi, lo, c, atr_col, bands=bands, ma_window=ma_window,
                           boll_k=boll_k, atr_mult=atr_mult)
    up = [pl.when(e > c).then(e) for e in pool]
    down = [pl.when(e < c).then(e) for e in pool]
    p = (hi + lo + c) / 3

    out = df.with_columns([
        p.alias("lvl_pivot"),
        (2 * p - lo).alias("lvl_pivot_r1"),
        (2 * p - hi).alias("lvl_pivot_s1"),
        _hmin(up).alias("lvl_resistance_1"),
        _hmax(down).alias("lvl_support_1"),
    ])
    # 第二档要排除第一档：条件里引用刚算出的列，等价于「次近」。
    r1 = pl.col("lvl_resistance_1")
    s1 = pl.col("lvl_support_1")
    return out.with_columns([
        _hmin([pl.when((e > c) & (e > r1)).then(e) for e in pool])
        .alias("lvl_resistance_2"),
        _hmax([pl.when((e < c) & (e < s1)).then(e) for e in pool])
        .alias("lvl_support_2"),
    ])
