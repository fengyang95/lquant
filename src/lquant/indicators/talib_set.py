"""TA-Lib 常用指标补齐（借鉴 InStock ``core/indicator/calculate_indicator.py``
的 32 指标清单，按 lquant 纪律重写）：

- **零新依赖**：不引 TA-Lib C 库（InStock 因此要求用户编译 C 共享库），
  指标一律 Polars 表达式；唯一例外是 CCI 的窗口内平均绝对偏差（先取绝对值
  再求和，不可分解为滚动累积量），用 numpy 滑窗一次算完整列；
- **严格因果**：只用 rolling / shift / ewm（adjust=False，递推），全部过
  ``assert_no_lookahead`` 前缀不变性门禁；
- Wilder 平滑（ATR / ADX）用 ``ewm_mean(alpha=1/n, adjust=False)``，
  与 TA-Lib 的 Wilder 递推等价；
- 除零安全：TR=0 / span=0 的窗口显式归 null 或中性值，不产生 NaN 污染下游。

纳入的是回测与组合层真正消费的子集（InStock 清单里与本仓库已注册指标
重叠的 MACD/KDJ/BOLL/RSI 等不再重复实现）：
ATR（组合层 ATR 风险预算依赖）、OBV、DMI/ADX、CCI、WR、ROC、DPO、
Supertrend。
"""

from __future__ import annotations

import math
import operator

import numpy as np
import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = [
    "add_adxr",
    "add_atr",
    "add_cci",
    "add_dmi",
    "add_dpo",
    "add_mfi",
    "add_obv",
    "add_ppo",
    "add_roc",
    "add_sar",
    "add_stochrsi",
    "add_supertrend",
    "add_tema",
    "add_trix",
    "add_wr",
]

_OHLC = ("open", "high", "low", "close")


def _check_n(n: int, name: str = "n") -> int:
    """周期参数 fail-loudly：必须为正整数，返回规范化后的 Python ``int``。

    ``n=0`` 会让 ``ewm_mean(alpha=1/n)`` 抛 ``ZeroDivisionError``、负周期让窗口
    语义整体反向 —— 都属于「调用方写错了」，不该以陌生异常或静默垃圾值的形式
    漏到下游（与 patterns 系列同一条纪律）。

    接受 Python ``int``、整数 ``float``（``14.0``，优化器/配置里常见）以及
    任何实现 ``__index__`` 的整数类型（``numpy.int64`` 等），统一归一化成
    ``int``（输出列名是 ``atr14`` 而不是 ``atr14.0``）；``bool`` 与非整数拒绝。
    """
    if not isinstance(n, bool):
        value: int | None = None
        if isinstance(n, int):
            value = n
        else:
            try:
                value = operator.index(n)          # numpy 整数等
            except TypeError:
                if isinstance(n, float) and math.isfinite(n) and float(n).is_integer():
                    value = int(n)
        if value is not None and value >= 1:
            return value
    raise ValueError(f"{name} 必须为正整数，收到 {n!r}")


def _mad_about_mean(col: str, n: int, df: pl.DataFrame) -> pl.Series:
    """窗口内「关于**该窗口均值**」的平均绝对偏差（CCI 的分母）。

    TA-Lib ``TA_CCI`` / Lambert 原始口径：``mean_j |TP_j − MA_i|``。
    先取绝对值再求和，不可分解为滚动累积量，所以 ``rolling_mean`` 帮不上忙；
    这里用 numpy ``sliding_window_view`` 一次算完整列 —— 比 ``Expr.rolling_map``
    （逐窗口进 Python）快两个数量级（60k 行实测 12.3s → 0.08s）。

    ⚠️ 刻意在**调用线程**里把整列取出来算，而不是用 ``Expr.map_batches``：后者的
    Python 回调跑在 Polars 的 Rust 线程池里，叠加 coverage 的 C tracer 会死锁
    （本地 ``-n 8 --cov`` 直接挂住；CI 靠 ``COVERAGE_CORE=sysmon`` 才绕开）。

    缺失值语义与 ``rolling_mean(n)`` 的 min_periods 一致：窗口内出现任一空值的
    行输出 **null**（不是 NaN），行数与输入等长。
    """
    values = df[col].cast(pl.Float64).to_numpy()
    out = np.full(values.size, np.nan, dtype=np.float64)
    if values.size >= n:
        windows = np.lib.stride_tricks.sliding_window_view(values, n)
        with np.errstate(invalid="ignore"):
            ok = ~np.isnan(windows).any(axis=1)
            mean = windows.mean(axis=1)
            md = np.abs(windows - mean[:, None]).mean(axis=1)
        out[n - 1:] = np.where(ok, md, np.nan)
    return pl.Series("_mad", out, dtype=pl.Float64).fill_nan(None)


def _check_positive(name: str, value: float) -> None:
    """正数阈值（SAR 加速因子、通道倍数等）：<= 0 会让递推/通道失去意义。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0.0
    ):
        raise ValueError(f"{name} 必须 > 0，收到 {value!r}")


@register_indicator(
    "atr", label="ATR 真实波幅", category="oscillator", pane="sub", min_window=40,
    inputs=("high", "low", "close"), outputs=("atr14",),
)
def add_atr(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """Wilder ATR：TR（真实波幅）的指数平滑（alpha=1/n）。

    TR = max(高-低, |高-昨收|, |昨收-低|)；首行没有昨收，``max_horizontal``
    会忽略这个 null 项，于是 TR[0] = 高-低（不是 null）。ATR 自首行起用
    ``ewm_mean(adjust=False)`` 递推。
    """
    n = _check_n(n)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pl.col("close").shift(1)).abs(),
        (pl.col("close").shift(1) - pl.col("low")).abs(),
    )
    df = df.with_columns(tr.alias("_tr"))
    df = df.with_columns(
        pl.col("_tr").ewm_mean(alpha=1 / n, adjust=False).alias(f"atr{n}")
    )
    return df.drop("_tr")


@register_indicator(
    "obv", label="OBV 能量潮", category="volume", pane="volume", min_window=2,
    inputs=("close", "volume"), outputs=("obv",),
)
def add_obv(df: pl.DataFrame, col: str = "close", vol: str = "volume") -> pl.DataFrame:
    """OBV：收盘上涨加量、下跌减量、平盘不变的累计和（纯因果）。
    首行无前收，涨跌按 0 记（不贡献量）。"""
    diff = pl.col(col).diff().fill_null(0.0)
    sign = (diff > 0).cast(pl.Float64) - (diff < 0).cast(pl.Float64)
    return df.with_columns((sign * pl.col(vol)).cum_sum().alias("obv"))


def _dmi_exprs(n: int) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """+DI / -DI / ADX 三个表达式（``dmi`` 与 ``adxr`` 共用，都只算不落列）。

    ADXR 只需要 ADX，不该为此把 ``dmi_pdi/mdi/adx`` 三列塞进调用方的 frame ——
    在 ``compute_many`` 链式拼接里那会变成「后执行的 adxr 删掉前面 dmi 请求的
    列」这种顺序相关的静默丢列。共用同一份表达式同时避免了两份 DMI 数学。
    """
    up_move = pl.col("high") - pl.col("high").shift(1)
    down_move = pl.col("low").shift(1) - pl.col("low")
    pdm = pl.when((up_move > down_move) & (up_move > 0)).then(up_move).otherwise(0.0)
    mdm = pl.when((down_move > up_move) & (down_move > 0)).then(down_move).otherwise(0.0)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pl.col("close").shift(1)).abs(),
        (pl.col("close").shift(1) - pl.col("low")).abs(),
    )
    s_pdm = pdm.ewm_mean(alpha=1 / n, adjust=False)
    s_mdm = mdm.ewm_mean(alpha=1 / n, adjust=False)
    s_tr = tr.ewm_mean(alpha=1 / n, adjust=False)
    pdi = pl.when(s_tr <= 0).then(None).otherwise(100 * s_pdm / s_tr)
    mdi = pl.when(s_tr <= 0).then(None).otherwise(100 * s_mdm / s_tr)
    dx_denom = pdi + mdi
    dx = pl.when(dx_denom <= 0).then(None).otherwise(100 * (pdi - mdi).abs() / dx_denom)
    adx = dx.ewm_mean(alpha=1 / n, adjust=False)
    return (
        pdi.alias("dmi_pdi"),
        mdi.alias("dmi_mdi"),
        adx.alias("dmi_adx"),
    )


@register_indicator(
    "dmi", label="DMI/ADX 趋向", category="trend", pane="sub", min_window=60,
    inputs=("high", "low", "close"),
    outputs=("dmi_pdi", "dmi_mdi", "dmi_adx"),
)
def add_dmi(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """Wilder DMI：+DI / -DI / ADX。

    +DM = 当根高点 > 昨高 且 (高-昨高) > (昨低-低) 时的 (高-昨高)，否则 0；
    -DM 镜像。DI = 100 * Wilder 平滑(DM) / ATR 平滑值；DX = 100*|+DI−-DI|/(+DI+-DI)；
    ADX = Wilder 平滑(DX)。分母为 0（无波动）时 DI 置 null、DX 置 null，
    不用 0 冒充方向信息。
    """
    n = _check_n(n)
    return df.with_columns(*_dmi_exprs(n))


@register_indicator(
    "cci", label="CCI 顺势指标", category="oscillator", pane="sub", min_window=20,
    inputs=_OHLC, outputs=("cci14",),
)
def add_cci(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """CCI：典型价 (H+L+C)/3 对其 n 日均线的偏离，用平均绝对偏差标准化。

    口径为 Lambert 原始定义（= TA-Lib ``TA_CCI``）：
    ``MD_i = mean_j |TP_j − MA_i|``，其中 ``j`` 遍历**以 i 结尾的同一个窗口**、
    ``MA_i`` 是该窗口自身的均值（也就是当根 SMA）。

    ⚠️ 注意别写成「各点先减去自己的滚动均值再取均值」——那是
    ``rolling_mean(|TP − rolling_mean(TP)|)``，分母会系统性偏小（线性序列上
    恰好小一半，于是 CCI 大整整两倍）。MD 关于窗口均值取绝对偏差，先取绝对值
    再求和不可分解为滚动累积量，只能用滑窗一次算完（见
    :func:`_mad_about_mean`）。

    分母为 0（窗口内典型价恒定）时置 null。常数 0.015 为 Lambert 原始口径。
    """
    n = _check_n(n)
    tp = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    df = df.with_columns(tp.alias("_tp"))
    df = df.with_columns(_mad_about_mean("_tp", n, df))
    ma = pl.col("_tp").rolling_mean(n)
    cci = pl.when(pl.col("_mad") <= 0).then(None).otherwise(
        (pl.col("_tp") - ma) / (0.015 * pl.col("_mad"))
    )
    df = df.with_columns(cci.alias(f"cci{n}"))
    return df.drop(["_tp", "_mad"])


@register_indicator(
    "wr", label="W&R 威廉指标", category="oscillator", pane="sub", min_window=14,
    inputs=("high", "low", "close"), outputs=("wr14",),
)
def add_wr(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """Williams %R（国内口径 0~100，值大 = 超卖）：区间内收盘相对位置的反向。

    高=低的窗口（连续一字板）置 50 中性，不产生 NaN。
    """
    n = _check_n(n)
    hhv = pl.col("high").rolling_max(n)
    llv = pl.col("low").rolling_min(n)
    span = hhv - llv
    wr = pl.when(span <= 0).then(50.0).otherwise((hhv - pl.col("close")) / span * 100)
    return df.with_columns(wr.clip(0.0, 100.0).alias(f"wr{n}"))


@register_indicator(
    "roc", label="ROC 变动率", category="oscillator",
    pane="sub", min_window=13, inputs=("close",), outputs=("roc12",),
)
def add_roc(df: pl.DataFrame, n: int = 12, col: str = "close") -> pl.DataFrame:
    """ROC：n 日价格变动率（%）。前收盘 ≤ 0 的脏价置 null。"""
    n = _check_n(n)
    prev = pl.col(col).shift(n)
    roc = pl.when(prev <= 0).then(None).otherwise((pl.col(col) - prev) / prev * 100)
    return df.with_columns(roc.alias(f"roc{n}"))


@register_indicator(
    "dpo", label="DPO 区间震荡", category="oscillator", pane="sub", min_window=35,
    inputs=("close",), outputs=("dpo20",),
)
def add_dpo(df: pl.DataFrame, n: int = 20, col: str = "close") -> pl.DataFrame:
    """DPO：收盘价减去 shift(n/2+1) 的 n 日均线 —— 右对齐均线的历史参照，
    天然无未来数据（TA-Lib 同口径）。"""
    n = _check_n(n)
    half = n // 2 + 1
    dpo = pl.col(col) - pl.col(col).rolling_mean(n).shift(half)
    return df.with_columns(dpo.alias(f"dpo{n}"))


@register_indicator(
    "supertrend", label="Supertrend 通道", category="channel", pane="price",
    min_window=40, inputs=("high", "low", "close"),
    outputs=("supertrend", "supertrend_dir"),
)
def add_supertrend(df: pl.DataFrame, n: int = 10, mult: float = 3.0) -> pl.DataFrame:
    """Supertrend：ATR 通道的递推翻转。

    上轨 = (高+低)/2 + mult·ATR，下轨 = (高+低)/2 − mult·ATR；
    趋势由收盘与轨线的穿越递推翻转（纯因果，只用当根与前根）；
    方向 dir = 1 多头 / -1 空头。递推是确定性的因果状态机，满足前缀不变性
    （前缀重算与全量计算在共同前缀上逐行一致，测试门禁锁定）。
    """
    n = _check_n(n)
    _check_positive("mult", mult)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pl.col("close").shift(1)).abs(),
        (pl.col("close").shift(1) - pl.col("low")).abs(),
    )
    df = df.with_columns(tr.alias("_tr"))
    df = df.with_columns(pl.col("_tr").ewm_mean(alpha=1 / n, adjust=False).alias("_atr"))
    hl2 = (pl.col("high") + pl.col("low")) / 2
    df = df.with_columns(
        (hl2 + mult * pl.col("_atr")).alias("_ub"),
        (hl2 - mult * pl.col("_atr")).alias("_lb"),
    )
    o, h, l, c = (
        df["_ub"].to_list(), df["high"].to_list(),
        df["low"].to_list(), df["close"].to_list(),
    )
    n_rows = len(o)
    st_vals: list[float | None] = [None] * n_rows
    dirs: list[int | None] = [None] * n_rows
    upper = lower = None
    direction = 1
    for i in range(n_rows):
        if o[i] is None or h[i] is None or l[i] is None or c[i] is None:
            st_vals[i] = None
            dirs[i] = None
            upper = lower = None
            continue
        ub = o[i] if upper is None else (o[i] if (o[i] < upper or c[i - 1] > upper) else upper)
        lb = lower if lower is not None and not (l[i] > lower or c[i - 1] < lower) else l[i]
        if lower is None:
            lb = l[i]
        upper, lower = ub, lb
        if direction == 1 and c[i] < lower:
            direction = -1
        elif direction == -1 and c[i] > upper:
            direction = 1
        dirs[i] = direction
        st_vals[i] = lower if direction == 1 else upper
    return df.with_columns(
        pl.Series("supertrend", st_vals, dtype=pl.Float64),
        pl.Series("supertrend_dir", dirs, dtype=pl.Int8),
    ).drop(["_tr", "_atr", "_ub", "_lb"])


# ---------------- 第二批：对齐 InStock 25 指标清单的剩余 7 项 ----------------


@register_indicator(
    "adxr", label="ADXR 平均趋向评级", category="trend", pane="sub",
    min_window=75, inputs=("high", "low", "close"), outputs=("adxr14",),
)
def add_adxr(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """ADXR：ADX 与其 shift(n) 的均值 —— 趋势强度的平滑确认（TA-Lib 同口径）。

    ADX 用 Wilder 平滑（alpha=1/n），ADXR 取当前与 n 根前 ADX 的算术平均，
    天然只用历史值。**只输出 adxr 本身**：用共享的 :func:`_dmi_exprs` 直接算
    ADX 表达式，不落 ``dmi_pdi/mdi/adx`` 中间列 —— 既不产生未登记的野列，也不
    会删掉调用方在 ``compute_many`` 里先后请求的 ``dmi`` 列（先删后丢是顺序
    相关的静默丢列）。
    """
    n = _check_n(n)
    _, _, adx = _dmi_exprs(n)
    return df.with_columns(((adx + adx.shift(n)) / 2).alias(f"adxr{n}"))


@register_indicator(
    "mfi", label="MFI 资金流量", category="oscillator", pane="sub",
    min_window=15, inputs=("high", "low", "close", "volume"), outputs=("mfi14",),
)
def add_mfi(df: pl.DataFrame, n: int = 14) -> pl.DataFrame:
    """MFI（Money Flow Index，带量的 RSI）：典型价 × 成交量的正/负向资金比。

    MFI = 100 − 100/(1 + 正流向/负流向)；分母（负流向）为 0 时置 100
    （全为正向资金，超买极值）。窗口内无昨收的行不参与（首行 null）。
    """
    n = _check_n(n)
    tp = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    raw = tp * pl.col("volume")
    diff = tp.diff()
    pos = pl.when(diff > 0).then(raw).otherwise(0.0)
    neg = pl.when(diff < 0).then(raw).otherwise(0.0)
    pos_sum = pos.rolling_sum(n)
    neg_sum = neg.rolling_sum(n)
    mfi = pl.when(neg_sum <= 0).then(100.0).otherwise(
        100 - 100 / (1 + pos_sum / neg_sum)
    )
    return df.with_columns(mfi.clip(0.0, 100.0).alias(f"mfi{n}"))


@register_indicator(
    "ppo", label="PPO 价格振荡百分比", category="oscillator", pane="sub",
    min_window=35, inputs=("close",), outputs=("ppo", "ppo_signal", "ppo_hist"),
)
def add_ppo(df: pl.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pl.DataFrame:
    """PPO：快慢 EMA 差值占慢 EMA 的比例（%），比 MACD 更适应跨品种比较。

    PPO = 100·(EMA_fast − EMA_slow)/EMA_slow；signal = EMA_9(PPO)；
    hist = PPO − signal。分母（慢 EMA）≤ 0 的脏价置 null。
    """
    fast = _check_n(fast, "fast")
    slow = _check_n(slow, "slow")
    signal = _check_n(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast 必须 < slow，收到 fast={fast!r} slow={slow!r}")
    ema_fast = pl.col("close").ewm_mean(span=fast, adjust=False)
    ema_slow = pl.col("close").ewm_mean(span=slow, adjust=False)
    ppo = pl.when(ema_slow <= 0).then(None).otherwise(100 * (ema_fast - ema_slow) / ema_slow)
    df = df.with_columns(ppo.alias("ppo"))
    sig = pl.col("ppo").ewm_mean(span=signal, adjust=False)
    df = df.with_columns(sig.alias("ppo_signal"))
    return df.with_columns((pl.col("ppo") - pl.col("ppo_signal")).alias("ppo_hist"))


def _rsi_expr(col: str, n: int) -> pl.Expr:
    """Wilder RSI 表达式：``100·avg_gain/(avg_gain+avg_loss)``。

    与 ``100 − 100/(1+avg_gain/avg_loss)`` 在 ``avg_loss > 0`` 时等价，但**零亏损
    窗口**（纯上涨）下前者给出 100（超买极值），后者会因除零被置 null —— 恰好把
    最该报警的位置抹掉。窗口内既无涨也无跌（avg_gain = avg_loss = 0）时，
    RSI 在数学上无定义，显式置 null 而不是用 0 或 50 冒充方向信息。
    """
    delta = pl.col(col).diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    avg_gain = gain.ewm_mean(alpha=1 / n, adjust=False)
    avg_loss = loss.ewm_mean(alpha=1 / n, adjust=False)
    denom = avg_gain + avg_loss
    return pl.when(denom <= 0).then(None).otherwise(100 * avg_gain / denom)


@register_indicator(
    "stochrsi", label="StochRSI 随机相对强弱", category="oscillator", pane="sub",
    min_window=30, inputs=("close",), outputs=("stochrsi_k", "stochrsi_d"),
)
def add_stochrsi(df: pl.DataFrame, n: int = 14, k: int = 3, d: int = 3) -> pl.DataFrame:
    """StochRSI：把 RSI 再做一次随机指标（KD）标准化到 0~1。

    StochRSI = (RSI − min_n(RSI)) / (max_n(RSI) − min_n(RSI))；
    %K = SMA_k(StochRSI)·100，%D = SMA_d(%K)。窗口内 RSI 恒定（分母 0）置 null；
    RSI 本身由 :func:`_rsi_expr` 给出（纯上涨窗口 = 100，不是 null）。
    """
    n = _check_n(n)
    k = _check_n(k, "k")
    d = _check_n(d, "d")
    df = df.with_columns(_rsi_expr("close", n).alias("_rsi"))
    hhv = pl.col("_rsi").rolling_max(n)
    llv = pl.col("_rsi").rolling_min(n)
    span = hhv - llv
    st = pl.when(span <= 0).then(None).otherwise((pl.col("_rsi") - llv) / span)
    # clip 消除浮点噪声：Polars rolling_mean 对已归一化到 [0,1] 的值求和时，
    # 累加误差可能产生 -5e-15 这类极小负值，回测里会被当成越界异常，故在
    # rolling 之后再夹一次。
    df = df.with_columns(
        (st.clip(0.0, 1.0) * 100).rolling_mean(k).clip(0.0, 100.0).alias("stochrsi_k")
    )
    df = df.with_columns(
        pl.col("stochrsi_k").rolling_mean(d).clip(0.0, 100.0).alias("stochrsi_d")
    )
    return df.drop("_rsi")


@register_indicator(
    "tema", label="TEMA 三重指数均线", category="trend", pane="price",
    min_window=60, inputs=("close",), outputs=("tema",),
)
def add_tema(df: pl.DataFrame, n: int = 20, col: str = "close") -> pl.DataFrame:
    """TEMA：三重指数移动平均，比同周期 EMA 更贴价、更少滞后。

    TEMA = 3·EMA1 − 3·EMA2 + EMA3，其中 EMA1=EMA(close)，
    EMA2=EMA(EMA1)，EMA3=EMA(EMA2)；全部 ewm 递推（adjust=False）。
    """
    n = _check_n(n)
    ema1 = pl.col(col).ewm_mean(span=n, adjust=False)
    df = df.with_columns(ema1.alias("_e1"))
    df = df.with_columns(pl.col("_e1").ewm_mean(span=n, adjust=False).alias("_e2"))
    df = df.with_columns(pl.col("_e2").ewm_mean(span=n, adjust=False).alias("_e3"))
    tema = 3 * pl.col("_e1") - 3 * pl.col("_e2") + pl.col("_e3")
    return df.with_columns(tema.alias("tema")).drop(["_e1", "_e2", "_e3"])


@register_indicator(
    "trix", label="TRIX 三重指数平滑", category="oscillator", pane="sub",
    min_window=40, inputs=("close",), outputs=("trix", "trix_signal"),
)
def add_trix(df: pl.DataFrame, n: int = 12, signal: int = 9, col: str = "close") -> pl.DataFrame:
    """TRIX：三重 EMA 的变化率（%），滤掉短期噪音的中期动量。

    TRIX = 100·(TEMA3_t − TEMA3_{t−1})/TEMA3_{t−1}，其中 TEMA3 为三重 EMA；
    分母 ≤ 0 的脏价置 null；signal = EMA_9(TRIX)。
    """
    n = _check_n(n)
    signal = _check_n(signal, "signal")
    e1 = pl.col(col).ewm_mean(span=n, adjust=False)
    df = df.with_columns(e1.alias("_t1"))
    df = df.with_columns(pl.col("_t1").ewm_mean(span=n, adjust=False).alias("_t2"))
    df = df.with_columns(pl.col("_t2").ewm_mean(span=n, adjust=False).alias("_t3"))
    prev = pl.col("_t3").shift(1)
    trix = pl.when(prev <= 0).then(None).otherwise(100 * (pl.col("_t3") - prev) / prev)
    df = df.with_columns(trix.alias("trix"))
    return df.with_columns(
        pl.col("trix").ewm_mean(span=signal, adjust=False).alias("trix_signal")
    ).drop(["_t1", "_t2", "_t3"])


@register_indicator(
    "sar", label="SAR 抛物线转向", category="trend", pane="price",
    min_window=10, inputs=("high", "low"), outputs=("sar", "sar_dir"),
)
def add_sar(df: pl.DataFrame, af_step: float = 0.02, af_max: float = 0.2) -> pl.DataFrame:
    """SAR（Parabolic SAR）：抛物线止转点，用加速因子递推的跟踪止损线。

    递推状态机纯因果（只用当根与历史），与 ``supertrend`` 同为逐行状态机，
    满足前缀不变性（含第 0 根）。``sar_dir`` = 1 多头（SAR 在价下）/ −1 空头。

    空值语义：缺口行置 null，状态机**保持**（不重置）——缺口后的第一个有效行
    仍从陈旧状态继续，因为「跳空/停牌」时没有可用的前一根翻转判据。前导空值
    行同样置 null（种子取第一对相邻有效 K 线）。

    ⚠️ 与 TA-Lib 的差异：TA-Lib 用「前两根 + 更早的上下文」确定初始方向与
    初始极值（EP），本实现以**第一对相邻有效 K 线**的高低关系定初始方向、
    以其极值定初始 EP/SAR，且初始 SAR 记在**后一根**上（前一根为 null）。
    两者在若干根之后收敛到同一条轨线（实测尾部一致），差异仅集中在序列开头的
    初始相位，对回测无实质影响。
    """
    _check_positive("af_step", af_step)
    _check_positive("af_max", af_max)
    if af_max < af_step:
        raise ValueError(f"af_max 必须 >= af_step，收到 af_max={af_max!r} af_step={af_step!r}")
    h = df["high"].to_list()
    l = df["low"].to_list()
    n_rows = len(h)
    sar_vals: list[float | None] = [None] * n_rows
    dirs: list[int | None] = [None] * n_rows
    # 种子 = 第一对「两根都有效」的相邻 K 线。种子 SAR/EP 只用这两根算，
    # 且只写在后一根上：任何一根的输出都只依赖当根与更早的数据，第 0 根
    # 永远是 null（首根没有前一根，无从定方向），不会出现「用下一根定当根」
    # 的前视；前导空值行也在这里被安全跳过（不再直接读 h[0]/h[1]）。
    seed = next((i for i in range(1, n_rows)
                 if None not in (h[i - 1], l[i - 1], h[i], l[i])), None)
    if seed is None:
        return df.with_columns(
            pl.Series("sar", sar_vals, dtype=pl.Float64),
            pl.Series("sar_dir", dirs, dtype=pl.Int8),
        )
    long = h[seed] >= h[seed - 1]
    af = af_step
    ep = max(h[seed - 1], h[seed]) if long else min(l[seed - 1], l[seed])
    sar = min(l[seed - 1], l[seed]) if long else max(h[seed - 1], h[seed])
    dirs[seed] = 1 if long else -1
    sar_vals[seed] = sar
    for i in range(seed + 1, n_rows):
        if h[i] is None or l[i] is None or h[i - 1] is None or l[i - 1] is None:
            # 缺口行：本行置 null，状态机保持原值（缺口后没有干净的前一根
            # 可用于翻转判据，重置反而会凭空造出一个新方向）。
            sar_vals[i] = None
            dirs[i] = None
            continue
        sar = sar + af * (ep - sar)
        # 夹逼只用**前两根**（Wilder 原始规则：SAR 不得进入前两个周期的价格
        # 区间）。这里绝不能把当根的 l[i]/h[i] 也夹进来 —— 那样多头分支下
        # ``sar <= l[i]`` 恒成立，``l[i] < sar`` 的翻转判据永远为假，SAR 就再也
        # 不会转向（实测 25% 的单边下跌全程 dir=1）。翻转判据必须在夹逼之后、
        # 用当根极值来判，这才是「抛物线**转向**」的语义。
        prior_lows = [l[j] for j in (i - 1, i - 2) if j >= 0 and l[j] is not None]
        prior_highs = [h[j] for j in (i - 1, i - 2) if j >= 0 and h[j] is not None]
        if long:
            sar = min([sar, *prior_lows])
            if l[i] < sar:
                long = False
                sar = ep
                ep = l[i]
                af = af_step
            elif h[i] > ep:
                ep = h[i]
                af = min(af + af_step, af_max)
        else:
            sar = max([sar, *prior_highs])
            if h[i] > sar:
                long = True
                sar = ep
                ep = h[i]
                af = af_step
            elif l[i] < ep:
                ep = l[i]
                af = min(af + af_step, af_max)
        sar_vals[i] = sar
        dirs[i] = 1 if long else -1
    return df.with_columns(
        pl.Series("sar", sar_vals, dtype=pl.Float64),
        pl.Series("sar_dir", dirs, dtype=pl.Int8),
    )
