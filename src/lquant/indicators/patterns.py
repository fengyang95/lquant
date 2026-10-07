"""K 线形态因子包 —— ``category="pattern"`` 的首批落地。

为什么形态走「预计算信号列」而不是因子 DSL：形态是**跨根比较**结构
（这根 vs 上一根的实体包含、三根星的递进关系），DSL 的窗口化纯函数
表达不了 —— 见 ``indicators/__init__.py`` 与 ``registry.py`` 的模块注释。
指标以 0/1 信号列反哺因子层（``lq factor`` 可直接引用信号列做再加工）。

信号语义（所有形态统一）：

1. **信号记在确认日**：形态由「当根 + 严格历史根（shift ≥ 1）」判定，
   不含未来数据 —— 每个指标都过 ``assert_no_lookahead`` 前缀不变性门禁；
2. **除零安全**：一字板（高=低，A 股涨跌停常见）振幅为 0，所有比例
   判定一律不成立，输出 0，不产生 NaN、不误报；
3. **阈值全部参数化**，缺省取经典教科书的宽松档（形态信号宁漏勿滥，
   过严会把教科书正例也漏掉，过滥则信号无信息量 —— 回归测试锁住
   「教科书正例必报」的下界）；
4. 输出列 dtype 为 Int8、无 null（头部预热行也是 0，不是 null）。

趋势背景（锤头/射击之星的「跌势后/涨势后」）用 ``close.shift(trend_n)``
与当根收盘比较 —— 纯历史参照，同样无未来数据。
"""

from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = [
    "add_bearish_engulfing",
    "add_bullish_engulfing",
    "add_doji",
    "add_hammer",
    "add_morning_star",
    "add_shooting_star",
]

_OHLC = ("open", "high", "low", "close")


def _body() -> pl.Expr:
    """实体高度 |收 - 开|。"""
    return (pl.col("close") - pl.col("open")).abs()


def _rng() -> pl.Expr:
    """整根振幅 高 - 低。"""
    return pl.col("high") - pl.col("low")


def _upper_shadow() -> pl.Expr:
    """上影线：高 - max(开, 收)。"""
    return pl.col("high") - pl.max_horizontal("open", "close")


def _lower_shadow() -> pl.Expr:
    """下影线：min(开, 收) - 低。"""
    return pl.min_horizontal("open", "close") - pl.col("low")


def _signal(cond: pl.Expr, name: str) -> pl.Expr:
    """0/1 信号列：条件 null（预热行）一律走 otherwise(0)，Int8 无 null。"""
    return pl.when(cond.fill_null(False)).then(1).otherwise(0).cast(pl.Int8).alias(name)


def _check_trend_n(trend_n: int) -> None:
    """趋势背景的窗口下限校验（fail-loudly）。

    trend_n < 1 时 ``shift(0)`` 自比较恒 False（指标静默全 0）、负数
    ``shift(-n)`` 会引用**未来根**（真实 lookahead —— 前缀不变性门禁只
    测缺省参数，抓不到运行时传入），所以在入口直接拒绝。
    """
    if int(trend_n) < 1:
        raise ValueError(f"trend_n 必须 >= 1，收到 {trend_n}（负数会引用未来数据）")


@register_indicator(
    "pattern_doji",
    label="十字星",
    category="pattern",
    pane="sub",
    min_window=1,
    inputs=_OHLC,
    outputs=("pattern_doji",),
)
def add_doji(
    df: pl.DataFrame, *, body_ratio: float = 0.1, min_range_pct: float = 0.004
) -> pl.DataFrame:
    """十字星：实体占整根振幅比例极小。

    ``min_range_pct`` 是绝对振幅下限（相对当根收盘价），防极窄幅 K 线的
    比例噪声：分母太小的时候 body/rng 的随机性没有意义。一字板
    （高=低）振幅为 0 直接不成立；close<=0 的脏价数据同样不成立。
    """
    rng = _rng()
    cond = (
        (rng > 0)
        & (pl.col("close") > 0)
        & (rng >= pl.col("close") * min_range_pct)
        & (_body() <= body_ratio * rng)
    )
    return df.with_columns(_signal(cond, "pattern_doji"))


@register_indicator(
    "pattern_hammer",
    label="锤头线",
    category="pattern",
    pane="sub",
    min_window=6,
    inputs=_OHLC,
    outputs=("pattern_hammer",),
)
def add_hammer(
    df: pl.DataFrame,
    *,
    body_max: float = 0.35,
    shadow_min: float = 2.0,
    upper_max: float = 0.25,
    trend_n: int = 5,
) -> pl.DataFrame:
    """锤头线：下跌之后出现，长下影（≥ shadow_min 倍实体）、小实体、短上影。

    跌势判定：trend_n 根前收盘高于当根收盘（严格历史参照）。
    实体为 0 的蜻蜓十字（长下影 + 开收同价）同属锤头家族，会被判为 1
    —— 教科书口径即如此。

    注意：注册表 ``min_window`` 按**缺省参数**（trend_n=5）静态声明；
    运行时调大 trend_n 后真实预热为 trend_n+1，取数方需自行多备。
    """
    _check_trend_n(trend_n)
    rng = _rng()
    cond = (
        (rng > 0)
        & (pl.col("close").shift(trend_n) > pl.col("close"))
        & (_lower_shadow() >= shadow_min * _body())
        & (_upper_shadow() <= upper_max * rng)
        & (_body() <= body_max * rng)
    )
    return df.with_columns(_signal(cond, "pattern_hammer"))


@register_indicator(
    "pattern_shooting_star",
    label="射击之星",
    category="pattern",
    pane="sub",
    min_window=6,
    inputs=_OHLC,
    outputs=("pattern_shooting_star",),
)
def add_shooting_star(
    df: pl.DataFrame,
    *,
    body_max: float = 0.35,
    shadow_min: float = 2.0,
    upper_max: float = 0.25,
    trend_n: int = 5,
) -> pl.DataFrame:
    """射击之星（流星）：上涨之后出现，长上影、小实体、短下影（锤头的镜像）。"""
    _check_trend_n(trend_n)
    rng = _rng()
    cond = (
        (rng > 0)
        & (pl.col("close").shift(trend_n) < pl.col("close"))
        & (_upper_shadow() >= shadow_min * _body())
        & (_lower_shadow() <= upper_max * rng)
        & (_body() <= body_max * rng)
    )
    return df.with_columns(_signal(cond, "pattern_shooting_star"))


@register_indicator(
    "pattern_bullish_engulfing",
    label="看涨吞没",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
    outputs=("pattern_bullish_engulfing",),
)
def add_bullish_engulfing(df: pl.DataFrame, *, require_bigger: bool = True) -> pl.DataFrame:
    """看涨吞没：前根阴线，当根阳线且实体完全包住前根实体。

    当根开盘 ≤ 前收 且 当根收盘 ≥ 前开（允许平开/平收的「骑颈」口径）。
    ``require_bigger``：当根实体严格大于前根 —— 防止两根实体完全相等的
    退化情形（如前收=当开、前开=当收的复制 K 线）也被算作吞没。
    """
    cond = (
        (pl.col("close").shift(1) < pl.col("open").shift(1))  # 前根阴线
        & (pl.col("close") > pl.col("open"))  # 当根阳线
        & (pl.col("open") <= pl.col("close").shift(1))
        & (pl.col("close") >= pl.col("open").shift(1))
        & (_body() > _body().shift(1) if require_bigger else pl.lit(True))
    )
    return df.with_columns(_signal(cond, "pattern_bullish_engulfing"))


@register_indicator(
    "pattern_bearish_engulfing",
    label="看跌吞没",
    category="pattern",
    pane="sub",
    min_window=2,
    inputs=_OHLC,
    outputs=("pattern_bearish_engulfing",),
)
def add_bearish_engulfing(df: pl.DataFrame, *, require_bigger: bool = True) -> pl.DataFrame:
    """看跌吞没：前根阳线，当根阴线且实体完全包住前根实体（看涨吞没的镜像）。"""
    cond = (
        (pl.col("close").shift(1) > pl.col("open").shift(1))  # 前根阳线
        & (pl.col("close") < pl.col("open"))  # 当根阴线
        & (pl.col("open") >= pl.col("close").shift(1))
        & (pl.col("close") <= pl.col("open").shift(1))
        & (_body() > _body().shift(1) if require_bigger else pl.lit(True))
    )
    return df.with_columns(_signal(cond, "pattern_bearish_engulfing"))


@register_indicator(
    "pattern_morning_star",
    label="早晨之星",
    category="pattern",
    pane="sub",
    min_window=3,
    inputs=_OHLC,
    outputs=("pattern_morning_star",),
)
def add_morning_star(
    df: pl.DataFrame, *, small_body_ratio: float = 0.5, recover_ratio: float = 0.5
) -> pl.DataFrame:
    """早晨之星（三根 K）：

    1. 第一根：大阴线；
    2. 第二根：小实体（≤ small_body_ratio × 第一根实体），且实体顶部
       低于第一根收盘（向下跳空/低位的宽松判据，A 股常见低开形态）；
    3. 第三根（确认日）：阳线，收盘收复第一根实体的 recover_ratio 以上。

    信号记在第三根（确认日）。
    """
    d1_bear = pl.col("close").shift(2) < pl.col("open").shift(2)
    d2_small = _body().shift(1) <= small_body_ratio * _body().shift(2)
    d2_gap = pl.max_horizontal("open", "close").shift(1) < pl.col("close").shift(2)
    d3_bull = pl.col("close") > pl.col("open")
    d3_recover = (
        pl.col("close") > (pl.col("open").shift(2) + pl.col("close").shift(2)) * recover_ratio
    )
    cond = d1_bear & d2_small & d2_gap & d3_bull & d3_recover
    return df.with_columns(_signal(cond, "pattern_morning_star"))
