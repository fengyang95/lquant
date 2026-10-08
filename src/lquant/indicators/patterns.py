"""K 线形态因子包 —— ``category="pattern"`` 的首批落地。

为什么形态走「预计算信号列」而不是因子 DSL：形态是**跨根比较**结构
（这根 vs 上一根的实体包含、三根星的递进关系），DSL 的窗口化纯函数
表达不了 —— 见 ``indicators/__init__.py`` 与 ``registry.py`` 的模块注释。
指标以 0/1 信号列输出，目前**只供图表（``GET /api/data/indicators``）与单票
分析端点消费**；这些信号列尚未接入因子评价面板与 G0 字段白名单（因子面板字段
来自 ``read_daily()`` + covariates，``lq factor check pattern_doji`` 会以
``STATIC_FAIL`` 拒绝），所以别把 ``pattern_*`` / ``cyq_*`` 当成 ``lq factor``
表达式里能直接引用的字段 —— 接进去是独立的一步工作。

信号语义（所有形态统一）：

1. **信号记在确认日**：形态由「当根 + 严格历史根（shift ≥ 1）」判定，
   不含未来数据 —— 每个指标都过 ``assert_no_lookahead`` 前缀不变性门禁；
2. **除零安全**：一字板（高=低，A 股涨跌停常见）振幅为 0，所有比例
   判定一律不成立，输出 0，不产生 NaN、不误报；
3. **阈值全部参数化**，缺省取经典教科书的宽松档（形态信号宁漏勿滥，
   过严会把教科书正例也漏掉，过滥则信号无信息量 —— 回归测试锁住
   「教科书正例必报」的下界）；比例/阈值参数在入口显式校验（``_check_*``），
   越界抛 ``ValueError``，不静默退化；
4. 输出列 dtype 为 Int8。**null 语义**：
   - 当根 OHLC 缺任一项（null）→ 输出 **null**：数据缺失 ≠ 形态未命中，
     把 null 收敛成 0 会让「缺数据」伪装成「没信号」（见 ``_signal``）；
   - 头部预热行（条件因 ``shift`` 越界而为 null）→ 输出 0，不是 null；
   - 有完整 OHLC 时恒为 1/0。

趋势背景（锤头/射击之星的「跌势后/涨势后」）用 ``close.shift(trend_n)``
与当根收盘比较 —— 纯历史参照，同样无未来数据。
"""

from __future__ import annotations

import math

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
    """0/1 信号列，带显式 null 语义。

    为什么不能简单 ``cond.fill_null(False)``：那会把「当根 OHLC 缺失」
    和「形态未命中」压成同一个 0，下游无法区分「没信号」与「没数据」
    （脏数据/停牌/取数缺口会被当成形态不成立）。

    - 当根 OHLC 缺任一项 → **null**（数据缺失，保持行数与 Int8 dtype）；
    - 否则条件为真 → 1；条件为假或为 null（头部预热，``shift`` 越界）
      → 0：预热是「还没法判」，不是历史数据缺失，沿用 0 的下游兼容语义。
    """
    missing = pl.any_horizontal([pl.col(c).is_null() for c in _OHLC])
    return (
        pl.when(missing)
        .then(pl.lit(None, dtype=pl.Int8))
        .when(cond)
        .then(pl.lit(1, dtype=pl.Int8))
        .otherwise(pl.lit(0, dtype=pl.Int8))
        .cast(pl.Int8)
        .alias(name)
    )


def _check_trend_n(trend_n: int) -> None:
    """趋势背景的窗口下限校验（fail-loudly）。

    trend_n < 1 时 ``shift(0)`` 自比较恒 False（指标静默全 0）、负数
    ``shift(-n)`` 会引用**未来根**（真实 lookahead —— 前缀不变性门禁只
    测缺省参数，抓不到运行时传入），所以在入口直接拒绝。
    """
    if int(trend_n) < 1:
        raise ValueError(f"trend_n 必须 >= 1，收到 {trend_n}（负数会引用未来数据）")


def _check_unit_interval(name: str, value: float) -> None:
    """比例类参数必须落在 (0, 1]：越界会静默退化或直接反转语义。

    如 ``body_ratio=-1`` 使 ``body <= -1*rng`` 恒 False（指标静默全 0）；
    ``small_body_ratio=-1`` 让「小实体」判据失去意义；``recover_ratio=2``
    则要求收盘涨到实体两倍以上，教科书正例全被漏掉。
    """
    if not (isinstance(value, (int, float)) and math.isfinite(value) and 0.0 < value <= 1.0):
        raise ValueError(f"{name} 必须落在 (0, 1]，收到 {value!r}")


def _check_positive(name: str, value: float) -> None:
    """正倍数阈值（如影线/实体倍数）：<=0 会让判据恒真或反向。"""
    if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0.0):
        raise ValueError(f"{name} 必须 > 0，收到 {value!r}")


def _check_min_range(min_range_pct: float) -> None:
    """振幅下界相对 close 的比例：与其它阈值同为 (0,1]，额外允许 0（关闭防御）。"""
    if not (
        isinstance(min_range_pct, (int, float))
        and math.isfinite(min_range_pct)
        and 0.0 <= min_range_pct <= 1.0
    ):
        raise ValueError(f"min_range_pct 必须落在 [0, 1]，收到 {min_range_pct!r}")


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
    _check_unit_interval("body_ratio", body_ratio)
    _check_min_range(min_range_pct)
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
    min_range_pct: float = 0.004,
) -> pl.DataFrame:
    """锤头线：下跌之后出现，长下影（≥ shadow_min 倍实体）、小实体、短上影。

    跌势判定：trend_n 根前收盘高于当根收盘（严格历史参照）。
    实体为 0 的蜻蜓十字（长下影 + 开收同价）同属锤头家族，会被判为 1
    —— 教科书口径即如此。

    ``min_range_pct`` 与 ``add_doji`` 同参数名、同缺省量级：振幅（相对当根
    收盘）低于该比例的窄幅 K 线一律不成立。没有这道下界时，日振幅仅
    零点几个百分点、实体 0.002 元的日常 K 线也会被报成锤头 —— 比例判据
    在极小分母下没有统计意义。

    注意：注册表 ``min_window`` 按**缺省参数**（trend_n=5）静态声明；
    运行时调大 trend_n 后真实预热为 trend_n+1，取数方需自行多备。
    """
    _check_trend_n(trend_n)
    _check_unit_interval("body_max", body_max)
    _check_positive("shadow_min", shadow_min)
    _check_unit_interval("upper_max", upper_max)
    _check_min_range(min_range_pct)
    rng = _rng()
    cond = (
        (rng > 0)
        & (pl.col("close") > 0)
        & (rng >= pl.col("close") * min_range_pct)
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
    min_range_pct: float = 0.004,
) -> pl.DataFrame:
    """射击之星（流星）：上涨之后出现，长上影、小实体、短下影（锤头的镜像）。

    ``min_range_pct`` 同 :func:`add_hammer`：窄幅 K 线不产生信号。
    """
    _check_trend_n(trend_n)
    _check_unit_interval("body_max", body_max)
    _check_positive("shadow_min", shadow_min)
    _check_unit_interval("upper_max", upper_max)
    _check_min_range(min_range_pct)
    rng = _rng()
    cond = (
        (rng > 0)
        & (pl.col("close") > 0)
        & (rng >= pl.col("close") * min_range_pct)
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
    3. 第三根（确认日）：阳线，收盘**收复第一根实体的 recover_ratio 以上**。

    第 3 条的坐标系：以第一根实体**起点**（阴线的收盘，实体下沿）为基准，
    阈值 = ``close_1 + recover_ratio × (open_1 - close_1)``，且含等号（``>=``）。
    此前写作 ``close_3 > (open_1 + close_1) × recover_ratio``——那是拿**实体
    中点**当基准并带严格不等号：第一根 (open=11.00, close=10.00)、
    recover_ratio=0.5 时 close_3=10.50 恰好「收复 50%」却判 0，与文档矛盾。

    信号记在第三根（确认日）。
    """
    _check_unit_interval("small_body_ratio", small_body_ratio)
    _check_unit_interval("recover_ratio", recover_ratio)
    d1_bear = pl.col("close").shift(2) < pl.col("open").shift(2)
    d2_small = _body().shift(1) <= small_body_ratio * _body().shift(2)
    d2_gap = pl.max_horizontal("open", "close").shift(1) < pl.col("close").shift(2)
    d3_bull = pl.col("close") > pl.col("open")
    # d1_bear 已保证实体向下（close_1 < open_1），故实体高度即 _body().shift(2)；
    # 以实体下沿 close_1 为基准，收复 recover_ratio 的实体高度。
    d3_recover = pl.col("close") >= pl.col("close").shift(2) + recover_ratio * _body().shift(2)
    cond = d1_bear & d2_small & d2_gap & d3_bull & d3_recover
    return df.with_columns(_signal(cond, "pattern_morning_star"))
