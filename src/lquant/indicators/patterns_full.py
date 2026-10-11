"""K 线形态全集扩展 —— 对照 InStock（myhhub/stock）``core/tablestructure.py``
的 61 形态清单（TA-Lib ``CDL*`` 全集），补齐 ``patterns.py`` 首批 6 种之外的
全部形态。

实现纪律与 ``patterns.py`` 一致（见其模块注释）：

1. 信号记在**确认日**，全部只用 ``shift >= 0`` 的历史参照，接入时统一过
   ``assert_no_lookahead`` 前缀不变性门禁；
2. 一字板（高=低）与窄幅 K 线的比例判据一律不成立，不误报；
3. 阈值参数化，缺省取教科书宽松档（宁漏勿滥）；参数越界抛 ``ValueError``；
4. 输出列 Int8：当根 OHLC 缺失 → null（数据缺失 ≠ 形态未命中），预热行 → 0；
5. **零新依赖**：不引 TA-Lib（InStock 因此要求用户编译 C 库），全部 Polars
   表达式；InStock 的中文形态名映射表直接作为命名与 label 的对照来源。

命名对齐 InStock ``tablestructure.py`` 的键（``tow_crows`` 等），注册名统一
``pattern_`` 前缀；bull/bear 双向形态按 InStock 同样拆成两个注册项。

TA-Lib 缺省口径 vs 本实现：TA-Lib 用「实体内跳空/严格比例」等内部参数，
这里取经典教科书宽松档并在各函数 docstring 写明与 TA-Lib 的差异点——
形态信号本来就该宁漏勿滥，回测门禁（G0-G3）会过滤无信息量信号。
"""

from __future__ import annotations

import math

import polars as pl

from lquant.indicators.patterns import _body, _lower_shadow, _rng, _signal, _upper_shadow
from lquant.indicators.registry import register_indicator

__all__: list[str] = []  # 新增形态由下方 add_* 装饰器注册进注册表

_OHLC = ("open", "high", "low", "close")


# ---------------- 共享参数校验 ----------------


def _check_unit_interval(name: str, value: float) -> None:
    if not (isinstance(value, (int, float)) and math.isfinite(value) and 0.0 < value <= 1.0):
        raise ValueError(f"{name} 必须落在 (0, 1]，收到 {value!r}")


def _check_positive(name: str, value: float) -> None:
    if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0.0):
        raise ValueError(f"{name} 必须 > 0，收到 {value!r}")


def _check_trend_n(trend_n: int) -> None:
    if int(trend_n) < 1:
        raise ValueError(f"trend_n 必须 >= 1，收到 {trend_n}（负数会引用未来数据）")


def _check_tolerance(name: str, value: float) -> None:
    """价格相等容差（相对当根收盘的比例），[0, 0.05]。"""
    if not (isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= value <= 0.05):
        raise ValueError(f"{name} 必须落在 [0, 0.05]，收到 {value!r}")


# ---------------- 共享条件原语（历史参照，无未来） ----------------


def _base(min_range_pct: float) -> list[pl.Expr]:
    """窄幅/脏价防线：振幅下界 + 一字板排除。所有比例判据形态共用。"""
    return [
        (_rng() > 0),
        (pl.col("close") > 0),
        (_rng() >= pl.col("close") * min_range_pct),
    ]


def _white() -> pl.Expr:
    return pl.col("close") > pl.col("open")


def _black() -> pl.Expr:
    return pl.col("close") < pl.col("open")


def _inside(inner: int = 1, outer: int = 2) -> pl.Expr:
    """inside bar：inner 根完全被 outer 根包住（高低点）。"""
    return (pl.col("high").shift(inner) < pl.col("high").shift(outer)) & (
        pl.col("low").shift(inner) > pl.col("low").shift(outer)
    )


def _newer_high(k: int) -> pl.Expr:
    """第 k 根收盘创其前一根新高（close_k > close_{k+1}，k 向过去数）。"""
    return pl.col("close").shift(k) > pl.col("close").shift(k + 1)


def _newer_low(k: int) -> pl.Expr:
    return pl.col("close").shift(k) < pl.col("close").shift(k + 1)


def _body_within(k: int, j: int) -> pl.Expr:
    """第 k 根实体完全落在第 j 根实体内（j 更早）。"""
    return (pl.max_horizontal("open", "close").shift(k) <= pl.max_horizontal("open", "close").shift(j)) & (
        pl.min_horizontal("open", "close").shift(k) >= pl.min_horizontal("open", "close").shift(j)
    )


def _eq(a: pl.Expr, b: pl.Expr, tol_pct: float) -> pl.Expr:
    """价格近似相等（相对 b 的比例容差）。"""
    return (a - b).abs() <= b.abs() * tol_pct


# ---------------- 单根形态 ----------------


@register_indicator(
    "pattern_gravestone_doji",
    label="墓碑十字",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_gravestone_doji(df: pl.DataFrame, *, body_ratio: float = 0.1,
                        shadow_ratio: float = 0.1, min_range_pct: float = 0.004) -> pl.DataFrame:
    """墓碑十字：实体与下影都极小，长上影（TA-Lib CDLGRAVESTONEDOJI）。"""
    _check_unit_interval("body_ratio", body_ratio)
    _check_unit_interval("shadow_ratio", shadow_ratio)
    cond = (*_base(min_range_pct),
            _body() <= body_ratio * _rng(),
            _lower_shadow() <= shadow_ratio * _rng(),
            _upper_shadow() >= 0.6 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_gravestone_doji"))


@register_indicator(
    "pattern_dragonfly_doji",
    label="蜻蜓十字",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_dragonfly_doji(df: pl.DataFrame, *, body_ratio: float = 0.1,
                       shadow_ratio: float = 0.1, min_range_pct: float = 0.004) -> pl.DataFrame:
    """蜻蜓十字/T形十字：实体与上影都极小，长下影。"""
    _check_unit_interval("body_ratio", body_ratio)
    _check_unit_interval("shadow_ratio", shadow_ratio)
    cond = (*_base(min_range_pct),
            _body() <= body_ratio * _rng(),
            _upper_shadow() <= shadow_ratio * _rng(),
            _lower_shadow() >= 0.6 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_dragonfly_doji"))


@register_indicator(
    "pattern_long_legged_doji",
    label="长脚十字",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_long_legged_doji(df: pl.DataFrame, *, body_ratio: float = 0.1,
                         min_range_pct: float = 0.006) -> pl.DataFrame:
    """长脚十字：实体极小、上下影均长（TA-Lib CDLLONGLEGGEDDOJI）。"""
    _check_unit_interval("body_ratio", body_ratio)
    cond = (*_base(min_range_pct),
            _body() <= body_ratio * _rng(),
            _upper_shadow() >= 0.3 * _rng(),
            _lower_shadow() >= 0.3 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_long_legged_doji"))


@register_indicator(
    "pattern_hanging_man",
    label="上吊线",
    category="pattern",
    pane="sub",
    min_window=6,
    inputs=_OHLC,
)
def add_hanging_man(df: pl.DataFrame, *, body_max: float = 0.35, shadow_min: float = 2.0,
                    upper_max: float = 0.25, trend_n: int = 5,
                    min_range_pct: float = 0.004) -> pl.DataFrame:
    """上吊线：上涨之后出现锤头形状（CDLHAMMER 的涨势镜像）。"""
    _check_trend_n(trend_n)
    _check_unit_interval("body_max", body_max)
    _check_positive("shadow_min", shadow_min)
    _check_unit_interval("upper_max", upper_max)
    cond = (*_base(min_range_pct),
            (pl.col("close").shift(trend_n) < pl.col("close")),
            _lower_shadow() >= shadow_min * _body(),
            _upper_shadow() <= upper_max * _rng(),
            _body() <= body_max * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_hanging_man"))


@register_indicator(
    "pattern_takuri",
    label="探水竿",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_takuri(df: pl.DataFrame, *, body_max: float = 0.2, upper_max: float = 0.1,
               min_range_pct: float = 0.004) -> pl.DataFrame:
    """探水竿：带极小实体与极小上影的长下影线（TA-Lib CDLTAKURI = 小实体锤头）。"""
    _check_unit_interval("body_max", body_max)
    _check_unit_interval("upper_max", upper_max)
    cond = (*_base(min_range_pct),
            _body() <= body_max * _rng(),
            _upper_shadow() <= upper_max * _rng(),
            _lower_shadow() >= 0.6 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_takuri"))


@register_indicator(
    "pattern_inverted_hammer",
    label="倒锤头",
    category="pattern",
    pane="sub",
    min_window=6,
    inputs=_OHLC,
)
def add_inverted_hammer(df: pl.DataFrame, *, body_max: float = 0.35, shadow_min: float = 2.0,
                        lower_max: float = 0.25, trend_n: int = 5,
                        min_range_pct: float = 0.004) -> pl.DataFrame:
    """倒锤头：下跌之后出现，长上影、小实体、短下影（CDLINVERTEDHAMMER）。"""
    _check_trend_n(trend_n)
    _check_unit_interval("body_max", body_max)
    _check_positive("shadow_min", shadow_min)
    _check_unit_interval("lower_max", lower_max)
    cond = (*_base(min_range_pct),
            (pl.col("close").shift(trend_n) > pl.col("close")),
            _upper_shadow() >= shadow_min * _body(),
            _lower_shadow() <= lower_max * _rng(),
            _body() <= body_max * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_inverted_hammer"))


@register_indicator(
    "pattern_marubozu",
    label="光头光脚",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_marubozu(df: pl.DataFrame, *, shadow_ratio: float = 0.05,
                 min_range_pct: float = 0.004) -> pl.DataFrame:
    """光头光脚/缺影线：上下影都近乎没有（TA-Lib 4 个方向变体合一）。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    cond = (*_base(min_range_pct),
            _upper_shadow() <= shadow_ratio * _rng(),
            _lower_shadow() <= shadow_ratio * _rng(),
            _body() >= 0.7 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_marubozu"))


@register_indicator(
    "pattern_closing_marubozu",
    label="收盘缺影线",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_closing_marubozu(df: pl.DataFrame, *, shadow_ratio: float = 0.1,
                         min_range_pct: float = 0.004) -> pl.DataFrame:
    """收盘缺影线：收盘端无影线（阳线无上影或阴线无下影），实体占绝对主体。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    bull = _white() & (_upper_shadow() <= shadow_ratio * _rng())
    bear = _black() & (_lower_shadow() <= shadow_ratio * _rng())
    cond = (*_base(min_range_pct),
            (bull | bear),
            _body() >= 0.7 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_closing_marubozu"))


@register_indicator(
    "pattern_spinning_top",
    label="纺锤",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_spinning_top(df: pl.DataFrame, *, body_max: float = 0.3, shadow_min: float = 0.1,
                     min_range_pct: float = 0.004) -> pl.DataFrame:
    """纺锤：小实体 + 上下影均存在（CDLSPINNINGTOP）。"""
    _check_unit_interval("body_max", body_max)
    _check_unit_interval("shadow_min", shadow_min)
    cond = (*_base(min_range_pct),
            _body() <= body_max * _rng(),
            _upper_shadow() >= shadow_min * _rng(),
            _lower_shadow() >= shadow_min * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_spinning_top"))


@register_indicator(
    "pattern_high_wave",
    label="风高浪大线",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_high_wave(df: pl.DataFrame, *, body_max: float = 0.25, shadow_min: float = 0.3,
                  min_range_pct: float = 0.008) -> pl.DataFrame:
    """风高浪大线：比纺锤实体更小、影线更长（CDLHIGHWAVE）。"""
    _check_unit_interval("body_max", body_max)
    _check_unit_interval("shadow_min", shadow_min)
    cond = (*_base(min_range_pct),
            _body() <= body_max * _rng(),
            _upper_shadow() >= shadow_min * _rng(),
            _lower_shadow() >= shadow_min * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_high_wave"))


@register_indicator(
    "pattern_long_line",
    label="长蜡烛",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_long_line(df: pl.DataFrame, *, body_min: float = 0.8,
                  min_range_pct: float = 0.004) -> pl.DataFrame:
    """长蜡烛：实体占据整根绝对主体（CDLLONGLINE，双向合一）。"""
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct), _body() >= body_min * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_long_line"))


@register_indicator(
    "pattern_short_line",
    label="短蜡烛",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_short_line(df: pl.DataFrame, *, body_max: float = 0.3,
                   min_range_pct: float = 0.004) -> pl.DataFrame:
    """短蜡烛：小实体（CDLSHORTLINE，双向合一）。"""
    _check_unit_interval("body_max", body_max)
    cond = (*_base(min_range_pct), _body() <= body_max * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_short_line"))


@register_indicator(
    "pattern_belt_hold_bull",
    label="看涨捉腰带线",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_belt_hold_bull(df: pl.DataFrame, *, shadow_ratio: float = 0.05,
                       body_min: float = 0.6, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看涨捉腰带线：阳线以最低价开盘（无下影）且实体占主体（CDLBELTHOLD）。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct),
            _white(),
            _lower_shadow() <= shadow_ratio * _rng(),
            _body() >= body_min * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_belt_hold_bull"))


@register_indicator(
    "pattern_belt_hold_bear",
    label="看跌捉腰带线",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_belt_hold_bear(df: pl.DataFrame, *, shadow_ratio: float = 0.05,
                       body_min: float = 0.6, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看跌捉腰带线：阴线以最高价开盘（无上影）且实体占主体。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct),
            _black(),
            _upper_shadow() <= shadow_ratio * _rng(),
            _body() >= body_min * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_belt_hold_bear"))


def _register_counterattack(direction: str) -> None:
    name = f"pattern_counterattack_{direction}"
    bull = direction == "bull"

    @register_indicator(name, label="看涨反击线" if bull else "看跌反击线",
                        category="pattern", pane="sub", min_window=2, inputs=_OHLC)
    def _fn(df: pl.DataFrame, *, equal_tol_pct: float = 0.003,
            body_min: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
        """反击线：前大实体 K 线，当根反向开盘但收盘回到前根收盘
        （CDLCOUNTERATTACK）。容差 ``equal_tol_pct`` 相对前根收盘。"""
        _check_tolerance("equal_tol_pct", equal_tol_pct)
        _check_unit_interval("body_min", body_min)
        # 看涨反击：前大阴，当根低开但收盘拉回前根收盘；看跌镜像。
        prev = (_black().shift(1) if bull else _white().shift(1)) & (
            _body().shift(1) >= body_min * _rng().shift(1))
        cur = _white() if bull else _black()
        cond = (*_base(min_range_pct),
                prev,
                cur,
                _eq(pl.col("close"), pl.col("close").shift(1), equal_tol_pct))
        return df.with_columns(_signal(pl.all_horizontal(cond), name))

    _fn.__name__ = f"add_{name}"


_register_counterattack("bull")
_register_counterattack("bear")


def _register_separating_lines(direction: str) -> None:
    name = f"pattern_separating_lines_{direction}"
    bull = direction == "bull"

    @register_indicator(name, label="看涨分离线" if bull else "看跌分离线",
                        category="pattern", pane="sub", min_window=2, inputs=_OHLC)
    def _fn(df: pl.DataFrame, *, equal_tol_pct: float = 0.003,
            body_min: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
        """分离线：反向开盘同价、收盘同向延续（CDLSEPARATINGLINES）。
        看涨：前阴今阳且当开≈前开；看跌镜像。"""
        _check_tolerance("equal_tol_pct", equal_tol_pct)
        _check_unit_interval("body_min", body_min)
        prev = _black().shift(1) if bull else _white().shift(1)
        cur_body = _body() >= body_min * _rng()
        cond = (*_base(min_range_pct),
                prev,
                _white() if bull else _black(),
                _eq(pl.col("open"), pl.col("open").shift(1), equal_tol_pct),
                cur_body)
        return df.with_columns(_signal(pl.all_horizontal(cond), name))

    _fn.__name__ = f"add_{name}"


_register_separating_lines("bull")
_register_separating_lines("bear")


@register_indicator(
    "pattern_matching_low",
    label="相同低价",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_matching_low(df: pl.DataFrame, *, equal_tol_pct: float = 0.003,
                     body_min: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
    """相同低价：前大阴，当根阴线收盘与前收几乎相同（CDLMATCHINGLOW，
    后续反弹确认由使用方结合次日阳线判断）。"""
    _check_tolerance("equal_tol_pct", equal_tol_pct)
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct),
            _black().shift(1),
            (_body().shift(1) >= body_min * _rng().shift(1)),
            _black(),
            _eq(pl.col("close"), pl.col("close").shift(1), equal_tol_pct))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_matching_low"))


# ---------------- 两根形态 ----------------


@register_indicator(
    "pattern_dark_cloud_cover",
    label="乌云盖顶",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_dark_cloud_cover(df: pl.DataFrame, *, open_gap: bool = False,
                         min_range_pct: float = 0.004) -> pl.DataFrame:
    """乌云盖顶：前大阳，当根阴线高开并收进前根实体上半以下
    （CDLDARKCLOUDCOVER）。

    ``open_gap=False``（缺省）用宽松口径「当根开盘高于前根收盘」（A 股
    高开常见平开观察）；``open_gap=True`` 用 TA-Lib 严格口径「开盘高于
    前根最高价」。收盘须低于前根实体中点且高于前根开盘。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    gap = (pl.col("open") > pl.col("high").shift(1)) if open_gap else (
        pl.col("open") > pl.col("close").shift(1))
    mid = pl.min_horizontal("open", "close").shift(1) + 0.5 * _body().shift(1)
    cond = (*_base(min_range_pct),
            _white().shift(1),
            _body().shift(1) >= 0.5 * _rng().shift(1),
            _black(),
            gap,
            (pl.col("close") < mid),
            (pl.col("close") > pl.col("open").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_dark_cloud_cover"))


@register_indicator(
    "pattern_piercing",
    label="刺透形态",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_piercing(df: pl.DataFrame, *, open_gap: bool = False,
                 min_range_pct: float = 0.004) -> pl.DataFrame:
    """刺透形态：乌云盖顶的看涨镜像（CDLPIERCING）。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    gap = (pl.col("open") < pl.col("low").shift(1)) if open_gap else (
        pl.col("open") < pl.col("close").shift(1))
    mid = pl.max_horizontal("open", "close").shift(1) - 0.5 * _body().shift(1)
    cond = (*_base(min_range_pct),
            _black().shift(1),
            _body().shift(1) >= 0.5 * _rng().shift(1),
            _white(),
            gap,
            (pl.col("close") > mid),
            (pl.col("close") < pl.col("open").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_piercing"))


def _register_neck_family(kind: str) -> None:
    """颈上线 / 颈内线 / 插入形态三兄弟（CDLONNECK / CDLINNECK / CDLTHRUSTING）。

    共同点：前根大阴线，当根阳线低开（低于前根最低价）。
    差异在收盘位置：
      - on_neck：收盘≈前根收盘；
      - in_neck：收盘略高于前根收盘（≤ 前根实体 10% 高度处）；
      - thrusting：收盘升入前根实体但低于其中点。
    """
    label = {"on": "颈上线", "in": "颈内线", "thrusting": "插入形态"}[kind]

    @register_indicator(f"pattern_{kind}_neck", label=label,
                        category="pattern", pane="sub", min_window=2, inputs=_OHLC)
    def _fn(df: pl.DataFrame, *, equal_tol_pct: float = 0.003, frac: float = 0.1,
            body_min: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
        _check_tolerance("equal_tol_pct", equal_tol_pct)
        _check_unit_interval("frac", frac)
        _check_unit_interval("body_min", body_min)
        prev = (_black().shift(1) & (_body().shift(1) >= body_min * _rng().shift(1)))
        cur_open_low = pl.col("open") < pl.col("low").shift(1)
        prev_close = pl.col("close").shift(1)
        prev_body_top = pl.col("open").shift(1)
        prev_mid = prev_close + 0.5 * (prev_body_top - prev_close)
        upper_band = prev_close + frac * (prev_body_top - prev_close)
        if kind == "on":
            close_cond = _eq(pl.col("close"), prev_close, equal_tol_pct)
        elif kind == "in":
            close_cond = (pl.col("close") > prev_close) & (pl.col("close") <= upper_band)
        else:
            close_cond = (pl.col("close") > prev_close) & (pl.col("close") < prev_mid)
        cond = (*_base(min_range_pct), prev, _white(), cur_open_low, close_cond)
        return df.with_columns(_signal(pl.all_horizontal(cond), f"pattern_{kind}_neck"))

    _fn.__name__ = f"add_{kind}_neck"


for _k in ("on", "in", "thrusting"):
    _register_neck_family(_k)
del _k


@register_indicator(
    "pattern_harami",
    label="孕线",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_harami(df: pl.DataFrame, *, prev_body_min: float = 0.5,
               cur_body_max: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
    """孕线：前根大实体，当根实体完全被前根实体包含（CDLHARAMI，双向合一）。"""
    _check_unit_interval("prev_body_min", prev_body_min)
    _check_unit_interval("cur_body_max", cur_body_max)
    prev_big = _body().shift(1) >= prev_body_min * _rng().shift(1)
    cond = (*_base(min_range_pct),
            prev_big,
            _body() <= cur_body_max * _rng(),
            _body_within(0, 1))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_harami"))


@register_indicator(
    "pattern_harami_cross",
    label="十字孕线",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_harami_cross(df: pl.DataFrame, *, prev_body_min: float = 0.5,
                     body_ratio: float = 0.1, min_range_pct: float = 0.004) -> pl.DataFrame:
    """十字孕线：孕线 + 当根为十字（CDLHARAMICROSS）。"""
    _check_unit_interval("prev_body_min", prev_body_min)
    _check_unit_interval("body_ratio", body_ratio)
    prev_big = _body().shift(1) >= prev_body_min * _rng().shift(1)
    cond = (*_base(min_range_pct),
            prev_big,
            _body() <= body_ratio * _rng(),
            _body_within(0, 1))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_harami_cross"))


@register_indicator(
    "pattern_homing_pigeon",
    label="家鸽",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_homing_pigeon(df: pl.DataFrame, *, prev_body_min: float = 0.5,
                      cur_body_max: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
    """家鸽：双阴孕线（CDLHOMINGPIGEON），看涨反转的候选。"""
    _check_unit_interval("prev_body_min", prev_body_min)
    _check_unit_interval("cur_body_max", cur_body_max)
    cond = (*_base(min_range_pct),
            _black().shift(1),
            _body().shift(1) >= prev_body_min * _rng().shift(1),
            _black(),
            _body() <= cur_body_max * _rng(),
            _body_within(0, 1))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_homing_pigeon"))


@register_indicator(
    "pattern_gap_side_side_white_up",
    label="向上跳空并列阳线",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_gap_side_side_white_up(df: pl.DataFrame, *, body_min: float = 0.4,
                               min_range_pct: float = 0.004) -> pl.DataFrame:
    """向上跳空并列阳线：两根阳线向上跳空，第三根阳线回探前根实体但
    收更高、缺口未回补（CDLGAPSIDESIDEWHITE 向上向）。确认日为第三根。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) > pl.col("high").shift(2)
    gap_holds = pl.col("low") > pl.col("high").shift(2)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _white().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _white(),
            (pl.col("open") <= pl.col("close").shift(1)),
            (pl.col("open") >= pl.col("open").shift(1)),
            (pl.col("close") > pl.col("close").shift(1)),
            gap_holds)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_gap_side_side_white_up"))


@register_indicator(
    "pattern_gap_side_side_white_down",
    label="向下跳空并列阴线",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_gap_side_side_white_down(df: pl.DataFrame, *, body_min: float = 0.4,
                                 min_range_pct: float = 0.004) -> pl.DataFrame:
    """向下跳空并列阴线：向上版的镜像（CDLGAPSIDESIDEWHITE 向下向）。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) < pl.col("low").shift(2)
    gap_holds = pl.col("high") < pl.col("low").shift(2)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            _black().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _black(),
            (pl.col("open") >= pl.col("close").shift(1)),
            (pl.col("open") <= pl.col("open").shift(1)),
            (pl.col("close") < pl.col("close").shift(1)),
            gap_holds)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_gap_side_side_white_down"))


@register_indicator(
    "pattern_tasuki_gap_up",
    label="向上跳空并列阴阳线",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_tasuki_gap_up(df: pl.DataFrame, *, body_min: float = 0.4,
                      min_range_pct: float = 0.004) -> pl.DataFrame:
    """向上跳空并列阴阳线（上涨持续）：两根阳线跳空，第三根阴线开在前根
    实体内、收盘更低但缺口未回补（CDLTASUKIGAP 向上向）。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) > pl.col("high").shift(2)
    gap_holds = pl.col("close") > pl.col("high").shift(2)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _white().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _black(),
            (pl.col("open") <= pl.col("close").shift(1)),
            (pl.col("open") >= pl.col("open").shift(1)),
            (pl.col("close") < pl.col("close").shift(1)),
            gap_holds)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_tasuki_gap_up"))


@register_indicator(
    "pattern_tasuki_gap_down",
    label="向下跳空并列阴阳线",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_tasuki_gap_down(df: pl.DataFrame, *, body_min: float = 0.4,
                        min_range_pct: float = 0.004) -> pl.DataFrame:
    """向下跳空并列阴阳线（下跌持续）：向下向（CDLTASUKIGAP）。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) < pl.col("low").shift(2)
    gap_holds = pl.col("close") < pl.col("low").shift(2)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            _black().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _white(),
            (pl.col("open") >= pl.col("close").shift(1)),
            (pl.col("open") <= pl.col("open").shift(1)),
            (pl.col("close") > pl.col("close").shift(1)),
            gap_holds)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_tasuki_gap_down"))


@register_indicator(
    "pattern_upside_gap_two_crows",
    label="向上跳空的两只乌鸦",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_upside_gap_two_crows(df: pl.DataFrame, *, body_min: float = 0.5,
                             min_range_pct: float = 0.004) -> pl.DataFrame:
    """向上跳空的两只乌鸦：大阳后两根阴线跳空高开、依次更低收盘，
    但都收在前根收盘之上（CDLUPSIDEGAP2CROWS），见顶候选。"""
    _check_unit_interval("body_min", body_min)
    gap1 = pl.col("open").shift(1) > pl.col("high").shift(2)
    gap2 = pl.col("open") > pl.col("close").shift(1)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _body().shift(2) >= body_min * _rng().shift(2),
            _black().shift(1),
            gap1,
            (pl.col("close").shift(1) > pl.col("close").shift(2)),
            _black(),
            gap2,
            (pl.col("close") < pl.col("close").shift(1)),
            (pl.col("close") > pl.col("close").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_upside_gap_two_crows"))


@register_indicator(
    "pattern_xside_gap_3_methods_up",
    label="向上跳空三法",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_xside_gap_3_methods_up(df: pl.DataFrame, *, body_min: float = 0.4,
                               min_range_pct: float = 0.004) -> pl.DataFrame:
    """向上跳空三法：两根阳线跳空，第三根阴线开在前根实体内、收在缺口
    之中（高于第一根收盘、低于第二根收盘，CDLXSIDEGAP3METHODS 向上向）。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) > pl.col("high").shift(2)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _white().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _black(),
            (pl.col("open") <= pl.col("close").shift(1)),
            (pl.col("close") < pl.col("close").shift(1)),
            (pl.col("close") > pl.col("close").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_xside_gap_3_methods_up"))


@register_indicator(
    "pattern_xside_gap_3_methods_down",
    label="向下跳空三法",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_xside_gap_3_methods_down(df: pl.DataFrame, *, body_min: float = 0.4,
                                 min_range_pct: float = 0.004) -> pl.DataFrame:
    """向下跳空三法：向下向（CDLXSIDEGAP3METHODS）。"""
    _check_unit_interval("body_min", body_min)
    gap = pl.col("open").shift(1) < pl.col("low").shift(2)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            _black().shift(1),
            _body().shift(1) >= body_min * _rng().shift(1),
            gap,
            _white(),
            (pl.col("open") >= pl.col("close").shift(1)),
            (pl.col("close") > pl.col("close").shift(1)),
            (pl.col("close") < pl.col("close").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_xside_gap_3_methods_down"))


@register_indicator(
    "pattern_kicking",
    label="反冲形态",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_kicking(df: pl.DataFrame, *, shadow_ratio: float = 0.1,
                min_range_pct: float = 0.004) -> pl.DataFrame:
    """反冲形态：前阴后阳两根光头光脚，且当根跳空开盘在前根实体之外
    （CDLKICKING）。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    maru = (_upper_shadow() <= shadow_ratio * _rng()) & (_lower_shadow() <= shadow_ratio * _rng())
    cond = (*_base(min_range_pct),
            _black().shift(1),
            maru.shift(1),
            _white(),
            maru,
            (pl.col("open") > pl.col("open").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_kicking"))


@register_indicator(
    "pattern_kicking_by_length",
    label="由较长缺影线决定的反冲形态",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_kicking_by_length(df: pl.DataFrame, *, shadow_ratio: float = 0.1,
                          length_tol: float = 0.02,
                          min_range_pct: float = 0.004) -> pl.DataFrame:
    """较长缺影线反冲：反冲 + 两根实体长度相等（CDLKICKINGBYLENGTH）。
    ``length_tol`` 为实体长度相对比例容差。"""
    _check_unit_interval("shadow_ratio", shadow_ratio)
    _check_tolerance("length_tol", length_tol)
    maru = (_upper_shadow() <= shadow_ratio * _rng()) & (_lower_shadow() <= shadow_ratio * _rng())
    same_len = _eq(_body(), _body().shift(1), length_tol)
    cond = (*_base(min_range_pct),
            _black().shift(1),
            maru.shift(1),
            _white(),
            maru,
            (pl.col("open") > pl.col("open").shift(1)),
            same_len)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_kicking_by_length"))


# ---------------- 三根及以上形态 ----------------


@register_indicator(
    "pattern_morning_doji_star",
    label="十字晨星",
    category="pattern",
    pane="sub",
    min_window=4,
    inputs=_OHLC,
)
def add_morning_doji_star(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """十字晨星：大阴 + 向下跳空十字 + 大阳收复（CDLMORNINGDOJISTAR，
    与 :func:`lquant.indicators.patterns.add_morning_star` 的差异在中根
    必须是十字且要求严格跳空）。确认日第三根。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    d1_bear = _black().shift(2) & (_body().shift(2) >= 0.5 * _rng().shift(2))
    d2_doji = _body().shift(1) <= 0.1 * _rng().shift(1)
    d2_gap = pl.col("high").shift(1) < pl.col("low").shift(2)
    d3_bull = _white() & (pl.col("close") > pl.col("close").shift(2) + 0.5 * _body().shift(2))
    gap_hold = pl.col("close") > pl.col("high").shift(1)
    cond = (*_base(min_range_pct), d1_bear, d2_doji, d2_gap, d3_bull | gap_hold)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_morning_doji_star"))


@register_indicator(
    "pattern_evening_star",
    label="暮星",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_evening_star(df: pl.DataFrame, *, small_body_ratio: float = 0.5,
                     fall_ratio: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
    """暮星：早晨之星的看跌镜像（CDLEVENINGSTAR）：

    1. 大阳线；2. 小实体且实体底部高于第一根收盘（向上跳空/高位宽松判据）；
    3. 阴线且收盘跌回第一根实体的 ``fall_ratio`` 以下。确认日第三根。"""
    _check_unit_interval("small_body_ratio", small_body_ratio)
    _check_unit_interval("fall_ratio", fall_ratio)
    d1_bull = _white().shift(2)
    d2_small = _body().shift(1) <= small_body_ratio * _body().shift(2)
    d2_gap = pl.min_horizontal("open", "close").shift(1) > pl.col("close").shift(2)
    d3_bear = _black()
    d3_fall = pl.col("close") <= pl.col("close").shift(2) - fall_ratio * _body().shift(2)
    cond = (*_base(min_range_pct), d1_bull, d2_small, d2_gap, d3_bear, d3_fall)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_evening_star"))


@register_indicator(
    "pattern_evening_doji_star",
    label="十字暮星",
    category="pattern",
    pane="sub",
    min_window=4,
    inputs=_OHLC,
)
def add_evening_doji_star(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """十字暮星：暮星的中根为十字且严格跳空（CDLEVENINGDOJISTAR）。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    d1_bull = _white().shift(2) & (_body().shift(2) >= 0.5 * _rng().shift(2))
    d2_doji = _body().shift(1) <= 0.1 * _rng().shift(1)
    d2_gap = pl.col("low").shift(1) > pl.col("high").shift(2)
    d3_bear = _black() & (pl.col("close") < pl.col("close").shift(2) - 0.5 * _body().shift(2))
    gap_hold = pl.col("close") < pl.col("low").shift(1)
    cond = (*_base(min_range_pct), d1_bull, d2_doji, d2_gap, d3_bear | gap_hold)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_evening_doji_star"))


@register_indicator(
    "pattern_abandoned_baby_bull",
    label="看涨弃婴",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_abandoned_baby_bull(df: pl.DataFrame, *, body_ratio: float = 0.1,
                            min_range_pct: float = 0.004) -> pl.DataFrame:
    """看涨弃婴：大阴 + 向下跳空十字（与两侧完全脱离）+ 大阳跳空回补
    （CDLABANDONEDBABY 向上向）。缺口对缺口，教科书最严的底部反转。"""
    _check_unit_interval("body_ratio", body_ratio)
    d2_doji = _body().shift(1) <= body_ratio * _rng().shift(1)
    gap_dn = pl.col("high").shift(1) < pl.col("low").shift(2)
    gap_up = pl.col("low") > pl.col("high").shift(1)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            d2_doji,
            gap_dn,
            _white(),
            gap_up)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_abandoned_baby_bull"))


@register_indicator(
    "pattern_abandoned_baby_bear",
    label="看跌弃婴",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_abandoned_baby_bear(df: pl.DataFrame, *, body_ratio: float = 0.1,
                            min_range_pct: float = 0.004) -> pl.DataFrame:
    """看跌弃婴：向上向（CDLABANDONEDBABY）。"""
    _check_unit_interval("body_ratio", body_ratio)
    d2_doji = _body().shift(1) <= body_ratio * _rng().shift(1)
    gap_up = pl.col("low").shift(1) > pl.col("high").shift(2)
    gap_dn = pl.col("high") < pl.col("low").shift(1)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            d2_doji,
            gap_up,
            _black(),
            gap_dn)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_abandoned_baby_bear"))


@register_indicator(
    "pattern_three_white_soldiers",
    label="三个白兵",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_white_soldiers(df: pl.DataFrame, *, body_min: float = 0.4,
                             min_range_pct: float = 0.004) -> pl.DataFrame:
    """三个白兵：三连阳、逐根创新高、开盘落入前根实体、实体占主体
    （CDL3WHITESOLDIERS）。确认日第三根。"""
    _check_unit_interval("body_min", body_min)
    big = lambda k: _body().shift(k) >= body_min * _rng().shift(k)  # noqa: E731
    open_in = lambda k: (pl.col("open").shift(k) > pl.col("open").shift(k + 1)) & (  # noqa: E731
        pl.col("open").shift(k) < pl.col("close").shift(k + 1))
    cond = (*_base(min_range_pct),
            _white().shift(2), _white().shift(1), _white(),
            _newer_high(1), _newer_high(0),
            big(2), big(1), big(0),
            open_in(1), open_in(0))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_white_soldiers"))


@register_indicator(
    "pattern_three_black_crows",
    label="三只乌鸦",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_black_crows(df: pl.DataFrame, *, body_min: float = 0.4,
                          min_range_pct: float = 0.004) -> pl.DataFrame:
    """三只乌鸦：三个白兵的看跌镜像（CDL3BLACKCROWS）。"""
    _check_unit_interval("body_min", body_min)
    big = lambda k: _body().shift(k) >= body_min * _rng().shift(k)  # noqa: E731
    open_in = lambda k: (pl.col("open").shift(k) < pl.col("open").shift(k + 1)) & (  # noqa: E731
        pl.col("open").shift(k) > pl.col("close").shift(k + 1))
    cond = (*_base(min_range_pct),
            _black().shift(2), _black().shift(1), _black(),
            _newer_low(1), _newer_low(0),
            big(2), big(1), big(0),
            open_in(1), open_in(0))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_black_crows"))


@register_indicator(
    "pattern_identical_three_crows",
    label="三胞胎乌鸦",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_identical_three_crows(df: pl.DataFrame, *, body_min: float = 0.4,
                              equal_tol_pct: float = 0.003,
                              min_range_pct: float = 0.004) -> pl.DataFrame:
    """三胞胎乌鸦：三只乌鸦且每根开盘=前根收盘（CDLIDENTICAL3CROWS）。"""
    _check_unit_interval("body_min", body_min)
    _check_tolerance("equal_tol_pct", equal_tol_pct)
    big = lambda k: _body().shift(k) >= body_min * _rng().shift(k)  # noqa: E731
    open_eq_prev_close = lambda k: _eq(  # noqa: E731
        pl.col("open").shift(k), pl.col("close").shift(k + 1), equal_tol_pct)
    cond = (*_base(min_range_pct),
            _black().shift(2), _black().shift(1), _black(),
            big(2), big(1), big(0),
            _newer_low(1), _newer_low(0),
            open_eq_prev_close(1), open_eq_prev_close(0))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_identical_three_crows"))


@register_indicator(
    "pattern_advance_block",
    label="大敌当前",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_advance_block(df: pl.DataFrame, *, body_min: float = 0.3,
                      min_range_pct: float = 0.004) -> pl.DataFrame:
    """大敌当前：三连阳但第三根实体缩小且上影变长，上涨动能衰竭
    （CDLADVANCEBLOCK）。"""
    _check_unit_interval("body_min", body_min)
    white = lambda k: _white().shift(k)  # noqa: E731
    big = lambda k: _body().shift(k) >= body_min * _rng().shift(k)  # noqa: E731
    open_in = lambda k: (pl.col("open").shift(k) > pl.col("open").shift(k + 1)) & (  # noqa: E731
        pl.col("open").shift(k) < pl.col("close").shift(k + 1))
    cond = (*_base(min_range_pct),
            white(2), white(1), white(0),
            _newer_high(1), _newer_high(0),
            big(2), big(1),
            (_body() < _body().shift(1)),
            (_upper_shadow() > _upper_shadow().shift(1)),
            open_in(1), open_in(0))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_advance_block"))


@register_indicator(
    "pattern_stalled_pattern",
    label="停顿形态",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_stalled_pattern(df: pl.DataFrame, *, shrink_ratio: float = 0.3,
                        equal_tol_pct: float = 0.003,
                        min_range_pct: float = 0.004) -> pl.DataFrame:
    """停顿形态：三连阳，第三根实体缩到第二根的 ``shrink_ratio`` 以下且
    开盘贴近第二根收盘（CDLSTALLEDPATTERN），强弩之末。"""
    _check_unit_interval("shrink_ratio", shrink_ratio)
    _check_tolerance("equal_tol_pct", equal_tol_pct)
    cond = (*_base(min_range_pct),
            _white().shift(2), _white().shift(1), _white(),
            _newer_high(1), _newer_high(0),
            _body().shift(1) >= 0.5 * _rng().shift(1),
            (_body() <= shrink_ratio * _body().shift(1)),
            _eq(pl.col("open"), pl.col("close").shift(1), equal_tol_pct))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_stalled_pattern"))


@register_indicator(
    "pattern_three_stars_in_south",
    label="南方三星",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_stars_in_south(df: pl.DataFrame, *, body_min: float = 0.5,
                             min_range_pct: float = 0.004) -> pl.DataFrame:
    """南方三星：长阴长下影 + 依次缩小的两根小阴，下跌动能衰竭
    （CDL3STARSINSOUTH）。确认日第三根。"""
    _check_unit_interval("body_min", body_min)
    d1 = (_black().shift(2) & (_body().shift(2) >= 0.45 * _rng().shift(2))
          & (_lower_shadow().shift(2) >= 0.8 * _body().shift(2)))
    d2 = (_black().shift(1)
          & (pl.col("open").shift(1) > pl.col("close").shift(2))
          & (pl.col("close").shift(1) < pl.col("close").shift(2)))
    d3 = (_black()
          & (pl.col("open") >= pl.min_horizontal("open", "close").shift(1))
          & (pl.col("open") <= pl.max_horizontal("open", "close").shift(1))
          & (pl.col("close") > pl.col("close").shift(1)))
    cond = (*_base(min_range_pct), d1, d2, d3)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_stars_in_south"))


@register_indicator(
    "pattern_three_inside_up",
    label="三内部上涨",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_inside_up(df: pl.DataFrame, *, prev_body_min: float = 0.5,
                        min_range_pct: float = 0.004) -> pl.DataFrame:
    """三内部上涨：看涨孕线（前大阴+内含小阳）+ 第三根阳线收复前根收盘
    （CDL3INSIDE 向上向）。确认日第三根。"""
    _check_unit_interval("prev_body_min", prev_body_min)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            _body().shift(2) >= prev_body_min * _rng().shift(2),
            _white().shift(1),
            _body_within(1, 2),
            _white(),
            (pl.col("close") > pl.col("close").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_inside_up"))


@register_indicator(
    "pattern_three_inside_down",
    label="三内部下跌",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_inside_down(df: pl.DataFrame, *, prev_body_min: float = 0.5,
                          min_range_pct: float = 0.004) -> pl.DataFrame:
    """三内部下跌：看跌孕线 + 第三根阴线跌破前根收盘（CDL3INSIDE 向下向）。"""
    _check_unit_interval("prev_body_min", prev_body_min)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _body().shift(2) >= prev_body_min * _rng().shift(2),
            _black().shift(1),
            _body_within(1, 2),
            _black(),
            (pl.col("close") < pl.col("close").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_inside_down"))


@register_indicator(
    "pattern_three_outside_up",
    label="三外部上涨",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_outside_up(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """三外部上涨：看涨吞没 + 第三根阳线创新高（CDL3OUTSIDE 向上向）。
    吞没条件与 :func:`lquant.indicators.patterns.add_bullish_engulfing` 同口径。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    engulf = (_black().shift(2)
              & _white().shift(1)
              & (pl.col("open").shift(1) <= pl.col("close").shift(2))
              & (pl.col("close").shift(1) >= pl.col("open").shift(2))
              & (_body().shift(1) > _body().shift(2)))
    cond = (*_base(min_range_pct), engulf, _white(),
            (pl.col("close") > pl.col("close").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_outside_up"))


@register_indicator(
    "pattern_three_outside_down",
    label="三外部下跌",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_three_outside_down(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """三外部下跌：看跌吞没 + 第三根阴线创新低（CDL3OUTSIDE 向下向）。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    engulf = (_white().shift(2)
              & _black().shift(1)
              & (pl.col("open").shift(1) >= pl.col("close").shift(2))
              & (pl.col("close").shift(1) <= pl.col("open").shift(2))
              & (_body().shift(1) > _body().shift(2)))
    cond = (*_base(min_range_pct), engulf, _black(),
            (pl.col("close") < pl.col("close").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_outside_down"))


@register_indicator(
    "pattern_three_line_strike_bull",
    label="看涨三线打击",
    category="pattern",
    pane="sub",
    min_window=4,
    inputs=_OHLC,
)
def add_three_line_strike_bull(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看涨三线打击：三连阴逐根新低，第四根大阳开盘低于第三根收盘、
    收盘收复第一根开盘（CDL3LINESTRIKE 向上向），一阴吃三阳的看涨反打。
    确认日第四根。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    three_blacks = (_black().shift(3) & _black().shift(2) & _black().shift(1)
                    & _newer_low(2) & _newer_low(1))
    big4 = _body() >= 0.8 * _rng()
    cond = (*_base(min_range_pct),
            three_blacks,
            (pl.col("open") < pl.col("close").shift(1)),
            big4,
            (pl.col("close") > pl.col("open").shift(3)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_line_strike_bull"))


@register_indicator(
    "pattern_three_line_strike_bear",
    label="看跌三线打击",
    category="pattern",
    pane="sub",
    min_window=4,
    inputs=_OHLC,
)
def add_three_line_strike_bear(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看跌三线打击：三连阳逐根新高，第四根大阴吞掉三根（向下向）。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    three_whites = (_white().shift(3) & _white().shift(2) & _white().shift(1)
                    & _newer_high(2) & _newer_high(1))
    big4 = _body() >= 0.8 * _rng()
    cond = (*_base(min_range_pct),
            three_whites,
            (pl.col("open") > pl.col("close").shift(1)),
            big4,
            (pl.col("close") < pl.col("open").shift(3)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_three_line_strike_bear"))


@register_indicator(
    "pattern_rising_three_methods",
    label="上升三法",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_rising_three_methods(df: pl.DataFrame, *, small_body: float = 0.3,
                             min_range_pct: float = 0.004) -> pl.DataFrame:
    """上升三法：大阳 + 三根小实体在其体内回调 + 大阳创新高
    （CDLRISEFALL3METHODS 向上向）。确认日第五根。"""
    _check_unit_interval("small_body", small_body)
    body1 = _body().shift(4)
    smalls = pl.all_horizontal([_body().shift(k) <= small_body * body1 for k in (3, 2, 1)])
    inside = pl.all_horizontal([_body_within(k, 4) for k in (3, 2, 1)])
    cond = (*_base(min_range_pct),
            _white().shift(4),
            body1 >= 0.6 * _rng().shift(4),
            smalls,
            inside,
            _white(),
            (pl.col("close") > pl.col("close").shift(4)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_rising_three_methods"))


@register_indicator(
    "pattern_falling_three_methods",
    label="下降三法",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_falling_three_methods(df: pl.DataFrame, *, small_body: float = 0.3,
                              min_range_pct: float = 0.004) -> pl.DataFrame:
    """下降三法：向下向（CDLRISEFALL3METHODS）。"""
    _check_unit_interval("small_body", small_body)
    body1 = _body().shift(4)
    smalls = pl.all_horizontal([_body().shift(k) <= small_body * body1 for k in (3, 2, 1)])
    inside = pl.all_horizontal([_body_within(k, 4) for k in (3, 2, 1)])
    cond = (*_base(min_range_pct),
            _black().shift(4),
            body1 >= 0.6 * _rng().shift(4),
            smalls,
            inside,
            _black(),
            (pl.col("close") < pl.col("close").shift(4)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_falling_three_methods"))


@register_indicator(
    "pattern_breakaway_bull",
    label="看涨脱离",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_breakaway_bull(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看涨脱离：大阴跳空下探三根后，第五根阳线收复跳空缺口上沿
    （CDLBREAKAWAY 向上向）。确认日第五根。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    mid_low = pl.min_horizontal(pl.col("low").shift(3), pl.col("low").shift(2), pl.col("low").shift(1))
    cond = (*_base(min_range_pct),
            _black().shift(4),
            _body().shift(4) >= 0.5 * _rng().shift(4),
            (pl.col("high").shift(3) < pl.col("low").shift(4)),  # 向下跳空
            _white(),
            (pl.col("close") > mid_low),
            (pl.col("close") < pl.col("close").shift(4)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_breakaway_bull"))


@register_indicator(
    "pattern_breakaway_bear",
    label="看跌脱离",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_breakaway_bear(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """看跌脱离：向下向（CDLBREAKAWAY）。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    mid_high = pl.max_horizontal(pl.col("high").shift(3), pl.col("high").shift(2), pl.col("high").shift(1))
    cond = (*_base(min_range_pct),
            _white().shift(4),
            _body().shift(4) >= 0.5 * _rng().shift(4),
            (pl.col("low").shift(3) > pl.col("high").shift(4)),  # 向上跳空
            _black(),
            (pl.col("close") < mid_high),
            (pl.col("close") > pl.col("close").shift(4)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_breakaway_bear"))


@register_indicator(
    "pattern_ladder_bottom",
    label="梯底",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_ladder_bottom(df: pl.DataFrame, *, min_range_pct: float = 0.004) -> pl.DataFrame:
    """梯底：三连阴新低 + 第四根带长上影的阴线（盘口试图反弹被压回）+
    第五根阳线收复第四根开盘价（CDLLADDERBOTTOM）。确认日第五根。"""
    _check_unit_interval("min_range_pct", min_range_pct)
    d4_shadow = _upper_shadow().shift(1) >= 0.3 * _rng().shift(1)
    cond = (*_base(min_range_pct),
            _black().shift(4), _black().shift(3), _black().shift(2),
            _newer_low(3), _newer_low(2),
            _black().shift(1),
            d4_shadow,
            _white(),
            (pl.col("close") > pl.col("open").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_ladder_bottom"))


@register_indicator(
    "pattern_concealing_baby_swallow",
    label="藏婴吞没",
    category="pattern",
    pane="sub",
    min_window=4,
    inputs=_OHLC,
)
def add_concealing_baby_swallow(df: pl.DataFrame, *, body_min: float = 0.4,
                                shadow_ratio: float = 0.4,
                                min_range_pct: float = 0.004) -> pl.DataFrame:
    """藏婴吞没：两根大阴 + 两根带长下影的阴线（第三根跳空低开，
    第四根实体被第三根吞没，CDLCONCEALBABYSWALL）。确认日第四根。"""
    _check_unit_interval("body_min", body_min)
    _check_unit_interval("shadow_ratio", shadow_ratio)
    long_shadow = lambda k: _lower_shadow().shift(k) >= shadow_ratio * _rng().shift(k)  # noqa: E731
    # 第四根「吞没」第三根：实体包含（TA-Lib 口径），不是被包含。
    engulf = (
        (pl.max_horizontal("open", "close") >= pl.max_horizontal("open", "close").shift(1))
        & (pl.min_horizontal("open", "close") <= pl.min_horizontal("open", "close").shift(1))
    )
    cond = (*_base(min_range_pct),
            _black().shift(3), _black().shift(2),
            _body().shift(3) >= body_min * _rng().shift(3),
            _body().shift(2) >= body_min * _rng().shift(2),
            _black().shift(1), _black(),
            (pl.col("open").shift(1) < pl.col("low").shift(2)),  # 第三根跳空低开
            engulf,
            (pl.col("close") < pl.col("close").shift(1)),
            long_shadow(1), long_shadow(0))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_concealing_baby_swallow"))


@register_indicator(
    "pattern_stick_sandwich",
    label="条形三明治",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_stick_sandwich(df: pl.DataFrame, *, equal_tol_pct: float = 0.003,
                       min_range_pct: float = 0.004) -> pl.DataFrame:
    """条形三明治：两根阴线夹一根向上跳空的阳线，两根阴线收盘几乎相同
    （CDLSTICKSANDWICH），确认日第三根。"""
    _check_tolerance("equal_tol_pct", equal_tol_pct)
    cond = (*_base(min_range_pct),
            _black().shift(2),
            _white().shift(1),
            (pl.col("open").shift(1) > pl.col("high").shift(2)),  # 跳空高开
            _black(),
            _eq(pl.col("close"), pl.col("close").shift(2), equal_tol_pct))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_stick_sandwich"))


@register_indicator(
    "pattern_tri_star",
    label="三星",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_tri_star(df: pl.DataFrame, *, body_ratio: float = 0.1,
                 min_range_pct: float = 0.006) -> pl.DataFrame:
    """三星：三根十字且中根与两侧形成跳空脱离（CDLTRISTAR），
    中根跳空方向决定看涨/看跌，本实现双向合一。"""
    _check_unit_interval("body_ratio", body_ratio)
    doji = lambda k: _body().shift(k) <= body_ratio * _rng().shift(k)  # noqa: E731
    gap_dn = pl.col("high").shift(1) < pl.col("low").shift(2)
    gap_up = pl.col("low").shift(1) > pl.col("high").shift(2)
    cond = (*_base(min_range_pct), doji(2), doji(1), doji(0), gap_dn | gap_up)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_tri_star"))


@register_indicator(
    "pattern_unique_three_river",
    label="奇特三河床",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_unique_three_river(df: pl.DataFrame, *, body_min: float = 0.5,
                           min_range_pct: float = 0.004) -> pl.DataFrame:
    """奇特三河床：大阴 + 带长下影的小阴（河床底）+ 低开小阳不收复前根
    实体（CDLUNIQUE3RIVER）。确认日第三根。"""
    _check_unit_interval("body_min", body_min)
    d1 = _black().shift(2) & (_body().shift(2) >= body_min * _rng().shift(2))
    d2 = (_black().shift(1)
          & (_body().shift(1) <= 0.5 * _body().shift(2))
          & (_lower_shadow().shift(1) >= 2 * _body().shift(1)))
    d3 = (_white()
          & (pl.col("open") < pl.col("close").shift(1))
          & (pl.col("close") < pl.col("close").shift(1)))
    cond = (*_base(min_range_pct), d1, d2, d3)
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_unique_three_river"))


@register_indicator(
    "pattern_mat_hold",
    label="铺垫",
    category="pattern",
    pane="sub",
    min_window=5,
    inputs=_OHLC,
)
def add_mat_hold(df: pl.DataFrame, *, small_body: float = 0.3,
                 min_range_pct: float = 0.004) -> pl.DataFrame:
    """铺垫：大阳 + 第二根跳空高开回吐 + 两根小阴整理 + 第五根阳线收复
    并创新高（CDLMATHOLD 向上向）。确认日第五根。"""
    _check_unit_interval("small_body", small_body)
    body1 = _body().shift(4)
    gap2 = pl.col("open").shift(3) > pl.col("high").shift(4)
    smalls = pl.all_horizontal([_body().shift(k) <= small_body * body1 for k in (3, 2, 1)])
    cond = (*_base(min_range_pct),
            _white().shift(4),
            body1 >= 0.6 * _rng().shift(4),
            gap2,
            smalls,
            _body_within(2, 4), _body_within(1, 4),
            _white(),
            (pl.col("close") > pl.col("close").shift(4)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_mat_hold"))


def _register_hikkake(kind: str) -> None:
    """陷阱 / 修正陷阱（CDLHIKKAKEMOD / CDLHIKKAKE）。

    陷阱是「假突破」结构：inside bar 之后一根向下（看涨版）/向上（看跌版）
    假突破收盘，随后 3 日内价格收回到 inside bar 的另一侧 —— 信号记在
    **确认日**（收回那日），不是假突破日。修正陷阱额外要求假突破收盘
    不得越过更早一根的极值（假得更明显）。

    实现：对回溯 1~3 日的确认窗做显式三路析取，全部历史参照，无未来。
    """
    bull = kind == "bull"
    name = f"pattern_hikkake_{'bull' if bull else 'bear'}"
    label = "看涨陷阱" if bull else "看跌陷阱"

    @register_indicator(name, label=label, category="pattern", pane="sub",
                        min_window=6, inputs=_OHLC)
    def _fn(df: pl.DataFrame, *, mod: bool = False) -> pl.DataFrame:
        conds = []
        for k in (1, 2, 3):  # 假突破发生在 d-k 日，inside bar 在 d-k-1，确认日 = 当根
            inside = _inside(k + 1, k + 2)
            if bull:
                fake = pl.col("close").shift(k) < pl.col("low").shift(k + 1)
                confirm = pl.col("close") > pl.col("high").shift(k + 1)
                mod_ok = pl.col("low").shift(k) > pl.col("low").shift(k + 2)
            else:
                fake = pl.col("close").shift(k) > pl.col("high").shift(k + 1)
                confirm = pl.col("close") < pl.col("low").shift(k + 1)
                mod_ok = pl.col("high").shift(k) < pl.col("high").shift(k + 2)
            conds.append(inside & fake & confirm & (mod_ok if mod else pl.lit(True)))
        cond = (*_base(0.0), pl.any_horizontal(conds))
        return df.with_columns(_signal(pl.all_horizontal(cond), name))

    _fn.__name__ = f"add_{name}"


_register_hikkake("bull")
_register_hikkake("bear")


def _register_hikkake_mod(direction: str) -> None:
    bull = direction == "bull"
    name = f"pattern_hikkake_mod_{'bull' if bull else 'bear'}"
    label = "看涨修正陷阱" if bull else "看跌修正陷阱"

    @register_indicator(name, label=label, category="pattern", pane="sub",
                        min_window=6, inputs=_OHLC)
    def _fn(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(_signal(
            _hikkake_cond(bull, mod=True), name))

    _fn.__name__ = f"add_{name}"


def _hikkake_cond(bull: bool, mod: bool) -> pl.Expr:
    """陷阱条件核心（供普通/修正两版复用），语义见 :func:`_register_hikkake`。"""
    conds = []
    for k in (1, 2, 3):
        inside = _inside(k + 1, k + 2)
        if bull:
            fake = pl.col("close").shift(k) < pl.col("low").shift(k + 1)
            confirm = pl.col("close") > pl.col("high").shift(k + 1)
            mod_ok = pl.col("low").shift(k) > pl.col("low").shift(k + 2)
        else:
            fake = pl.col("close").shift(k) > pl.col("high").shift(k + 1)
            confirm = pl.col("close") < pl.col("low").shift(k + 1)
            mod_ok = pl.col("high").shift(k) < pl.col("high").shift(k + 2)
        conds.append(inside & fake & confirm & (mod_ok if mod else pl.lit(True)))
    return pl.any_horizontal(conds)


_register_hikkake_mod("bull")
_register_hikkake_mod("bear")


# ---------------- 补齐 TA-Lib 全集遗漏的三型 ----------------
# InStock ``tablestructure.py`` 的 61 项 TA-Lib ``CDL*`` 清单里，以下三型在
# 首批实现中遗漏，此处补齐以使覆盖度对齐 61/61。


@register_indicator(
    "pattern_two_crows",
    label="两只乌鸦",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
)
def add_two_crows(df: pl.DataFrame, *, body_min: float = 0.5,
                  min_range_pct: float = 0.004) -> pl.DataFrame:
    """两只乌鸦（CDL2CROWS，看跌反转）：大阳之后两根阴线，第一根**向上
    跳空**高开（``open > 前收``）但收回到阳线收盘之下，第二根再收更低
    （``close < 前收``）却仍站在阳线收盘之上（``close > 阳线收盘``）——
    两阴无力全数吞没阳线，构成上行乏力。

    ⚠️ 与 TA-Lib 的差异：TA-Lib 的 ``CDL2CROWS`` 额外要求**前置趋势**
    （更早若干根显著上涨）与严格的实体比例门槛，实践中极度难触发；
    这里按经典教科书定义实现（``min_window`` 只用 3 根），保持可用性，
    与 ``pattern_upside_gap_two_crows``（高开版）成对。
    """
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct),
            _white().shift(2),
            _body().shift(2) >= body_min * _rng().shift(2),
            _black().shift(1),
            (pl.col("open").shift(1) > pl.col("close").shift(2)),
            (pl.col("close").shift(1) < pl.col("close").shift(2)),
            _black(),
            (pl.col("close") < pl.col("close").shift(1)),
            (pl.col("close") > pl.col("open").shift(2)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_two_crows"))


@register_indicator(
    "pattern_doji_star",
    label="十字星（十字胎星）",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
)
def add_doji_star(df: pl.DataFrame, *, body_ratio: float = 0.1,
                  body_min: float = 0.5, min_range_pct: float = 0.004) -> pl.DataFrame:
    """十字星（CDLDOJISTAR）：前一根为**显著实体**（大阴或大阳），当根为
    十字（实体极小）且与前根**实体跳空**（十字整体脱离前根实体区间）——
    方向由前根阴阳决定（前阴后十字 = 看涨晨星雏形；前阳后十字 = 看跌暮星
    雏形），故本实现按 lquant 统一的 0/1 语义输出**命中**，方向由调用方
    结合前根阴阳（``pattern_doji`` + 前根 ``close/open``）读取。

    ⚠️ 与 TA-Lib 的差异：TA-Lib 的 ``CDLDOJISTAR`` 同时返回 ±100 编码
    方向；此处遵循仓库统一的 Int8 0/1 信号约定（见 ``_signal`` docstring）。
    """
    _check_unit_interval("body_ratio", body_ratio)
    _check_unit_interval("body_min", body_min)
    cond = (*_base(min_range_pct),
            _body().shift(1) >= body_min * _rng().shift(1),
            _body() <= body_ratio * _rng(),
            (pl.min_horizontal("open", "close") >= pl.max_horizontal("open", "close").shift(1))
            | (pl.max_horizontal("open", "close") <= pl.min_horizontal("open", "close").shift(1)))
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_doji_star"))


@register_indicator(
    "pattern_rickshaw_man",
    label="黄包车夫",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
)
def add_rickshaw_man(df: pl.DataFrame, *, body_ratio: float = 0.1,
                     min_range_pct: float = 0.01) -> pl.DataFrame:
    """黄包车夫（CDLRICKSHAWMAN）：实体极小（十字）且上下影**都很长**，
    近似等长 —— 多空僵持、方向不明的中继信号。判据与长脚十字相近，
    但额外要求振幅显著（``min_range_pct`` 更大）以区别于普通十字。
    """
    _check_unit_interval("body_ratio", body_ratio)
    cond = (*_base(min_range_pct),
            _body() <= body_ratio * _rng(),
            _upper_shadow() >= 0.4 * _rng(),
            _lower_shadow() >= 0.4 * _rng())
    return df.with_columns(_signal(pl.all_horizontal(cond), "pattern_rickshaw_man"))
