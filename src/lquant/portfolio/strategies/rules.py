"""选股策略实现：每个策略一个 ``_run_<name>`` 纯函数，规则写在函数 docstring。

统一输入：单标的日线 ``pl.DataFrame``（trade_date/open/high/low/close/volume/
amount/pre_close，按日升序）；统一输出：``StrategyResult``。
注册在文件底部 ``register_strategy`` 完成 —— 新策略 = 新函数 + 一行注册。
"""
from __future__ import annotations

import polars as pl

from lquant.portfolio.strategies.registry import _need_rows, _no, _ok

# 策略是否只看「最后一根 K 线」之后的截面 —— 全部是当日快照型判定，
# 与 screener 的「先过滤后打分」互补：screener 排除买不到的，这里找形态。


def _run_volume_surge(df: pl.DataFrame, *, vol_ratio: float = 2.0,
                      min_amount: float = 2e8, min_pct: float = 0.0) -> pl.DataFrame:
    """放量上涨（InStock 同名策略）：
    1. 当日上涨且收阳（close > open）；
    2. 当日涨幅 >= min_pct%（百分比口径，默认 0 即只要求上涨）；
    3. 成交量 >= vol_ratio × 5 日均量；
    4. 成交额 >= min_amount（默认 2 亿，InStock 口径；小资金可调低）。
    """
    last = df.tail(1).row(0, named=True)
    pre_close = last["pre_close"]
    # 涨幅是「上涨」的量化口径，没有前收就算不出来；显式说不算，而不是让它
    # 在后面除法里崩成「计算失败」。
    if not pre_close:
        return _no("volume_surge", "缺少有效前收 pre_close，无法判定当日涨幅")
    vol_ma5 = df["volume"].tail(6).head(5).mean()
    amount = last["amount"] or (last["close"] * last["volume"])
    pct = (last["close"] / pre_close - 1) * 100
    if last["close"] <= last["open"] or last["close"] <= pre_close:
        return _no("volume_surge",
                   f"未收阳: open={last['open']} close={last['close']} "
                   f"pre_close={pre_close}")
    # min_pct 此前只是签名里的装饰：调用方传了会被 _guard 的签名过滤保留、
    # 然后被彻底忽略。这里让它真正作为当日涨幅下限参与判定。
    if pct < min_pct:
        return _no("volume_surge", f"涨幅不足: {pct:+.2f}% < {min_pct:.2f}%")
    ratio = last["volume"] / vol_ma5 if vol_ma5 else 0.0
    if ratio < vol_ratio:
        return _no("volume_surge",
                   f"量比不足: {ratio:.2f}x < {vol_ratio}x（5日均量 {vol_ma5:,.0f}）")
    if amount < min_amount:
        return _no("volume_surge",
                   f"成交额不足: {amount/1e8:.2f}亿 < {min_amount/1e8:.0f}亿")
    return _ok("volume_surge",
               f"量比 {ratio:.2f}x，成交额 {amount/1e8:.2f}亿，"
               f"收阳涨幅 {pct:.2f}%")


def _run_keep_rising(df: pl.DataFrame, *, days: int = 4) -> pl.DataFrame:
    """持续上涨（InStock: keep_increasing）：最近 days 日收盘逐日抬升。"""
    if days < 1:
        return _no("keep_rising", f"参数非法: days={days} 必须 ≥ 1")
    # days 是运行时参数，注册时的静态 min_rows 看不到它：days 调大后行数不足
    # 会让 closes 取不满 → IndexError 被吞成 evidence。这里按实际 days 拦截。
    short = _need_rows("keep_rising", df, days + 1,
                       f"逐日抬升需 days+1={days + 1} 根收盘")
    if short is not None:
        return short
    closes = df["close"].tail(days + 1).to_list()
    ups = [closes[i] < closes[i + 1] for i in range(days)]
    if all(ups):
        pct = (closes[-1] / closes[0] - 1) * 100
        return _ok("keep_rising", f"连续 {days} 日上涨，区间涨幅 {pct:+.2f}%")
    bad = next(i for i, u in enumerate(ups) if not u)
    return _no("keep_rising",
               f"第 {bad + 1}→{bad + 2} 日未上涨 "
               f"({closes[bad]:.2f}→{closes[bad + 1]:.2f})")


def _run_pullback_ma250(df: pl.DataFrame, *, band: float = 0.03) -> pl.DataFrame:
    """回踩年线（InStock: backtrace_ma250）：收盘价在 MA250 上方 0~band 内 ——
    站上年线后的回踩确认，跌破即落选（年线是牛熊分界的朴素口径）。"""
    ma250 = df["close"].tail(250).mean()
    last = df.tail(1).row(0, named=True)
    dev = last["close"] / ma250 - 1
    if dev < 0:
        return _no("pullback_ma250",
                   f"已跌破年线: close={last['close']:.2f} < MA250={ma250:.2f}")
    if dev > band:
        return _no("pullback_ma250",
                   f"偏离年线过远: +{dev*100:.2f}% > {band*100:.0f}%（非回踩区间）")
    return _ok("pullback_ma250",
               f"MA250={ma250:.2f}，close={last['close']:.2f}，"
               f"偏离 +{dev*100:.2f}%（回踩确认区）")


def _run_turtle_breakout(df: pl.DataFrame, *, window: int = 20) -> pl.DataFrame:
    """海龟突破（InStock: turtle_trade 的入场腿）：收盘创最近 window 日新高
    （不含当日）—— 趋势跟踪的经典入场信号；出场腿（跌破 10 日低点）属卖出
    判定，不在选股策略范围。"""
    if window < 1:
        return _no("turtle_breakout", f"参数非法: window={window} 必须 ≥ 1")
    # 要取「不含当日」的前 window 根高点，实际需要 window+1 根；静态 min_rows
    # 按默认 20 写死 21，window 调大后会静默把 20 根当成「30 日高点」。
    short = _need_rows("turtle_breakout", df, window + 1,
                       f"前 {window} 日高点需 window+1={window + 1} 根")
    if short is not None:
        return short
    prev_high = df["high"].tail(window + 1).head(window).max()
    last = df.tail(1).row(0, named=True)
    if last["close"] > prev_high:
        return _ok("turtle_breakout",
                   f"突破 {window} 日高点: close={last['close']:.2f} > "
                   f"prev_high={prev_high:.2f}")
    return _no("turtle_breakout",
               f"未突破: close={last['close']:.2f} ≤ {window}日高点 {prev_high:.2f}")


def _run_low_atr(df: pl.DataFrame, *, window: int = 14,
                 max_atr_pct: float = 2.5) -> pl.DataFrame:
    """低波动（InStock: low_atr）：ATR(window)/收盘价 <= max_atr_pct%。
    波动小 = 容错空间大，适合作为底仓筛选条件与其他策略叠加。"""
    if window < 1:
        return _no("low_atr", f"参数非法: window={window} 必须 ≥ 1")
    # 静态 min_rows 按默认 window=14 写死 15；window 调大而数据不变时，
    # tail(window) 只会凑出更短的均值 —— 那是「半截数据的 ATR」，必须显式降级。
    short = _need_rows("low_atr", df, window + 1,
                       f"真 TR 需 window+1={window + 1} 根（首根只提供前收）")
    if short is not None:
        return short
    # 多取一根拿前收：TR = max(H-L, |H-C_prev|, |L-C_prev|)，首行 shift 为
    # null，tail(window) 截掉后恰好是完整 window 根的真 TR。
    tail = df.tail(window + 1).with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr"))
    atr = max(float(tail["tr"].tail(window).mean()), 1e-9)
    atr_pct = atr / df["close"][-1] * 100
    if atr_pct <= max_atr_pct:
        return _ok("low_atr",
                   f"ATR({window})={atr:.3f}，占现价 {atr_pct:.2f}% ≤ {max_atr_pct}%")
    return _no("low_atr",
               f"波动过大: ATR 占现价 {atr_pct:.2f}% > {max_atr_pct}%")


# ---------- 注册（实现与注册分离：改规则不碰注册表） ----------

from lquant.portfolio.strategies.registry import register_strategy  # noqa: E402

register_strategy("volume_surge", label="放量上涨",
                  desc="收阳 + 量比≥2 + 成交额≥2亿（参数可调）", min_rows=6)(
    _run_volume_surge)
register_strategy("keep_rising", label="持续上涨",
                  desc="最近 N 日收盘逐日抬升（默认 4 日）", min_rows=5)(
    _run_keep_rising)
register_strategy("pullback_ma250", label="回踩年线",
                  desc="收盘在 MA250 上方 0~3%（回踩确认区）", min_rows=250)(
    _run_pullback_ma250)
register_strategy("turtle_breakout", label="海龟突破",
                  desc="收盘创 N 日新高（默认 20 日，入场腿）", min_rows=21)(
    _run_turtle_breakout)
register_strategy("low_atr", label="低波动",
                  desc="ATR(14)/现价 ≤ 2.5%（底仓容错筛选）", min_rows=15)(
    _run_low_atr)
