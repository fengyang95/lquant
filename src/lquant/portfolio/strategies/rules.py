"""选股策略实现：每个策略一个 ``_run_<name>`` 纯函数，规则写在函数 docstring。

统一输入：单标的日线 ``pl.DataFrame``（trade_date/open/high/low/close/volume/
amount/pre_close，按日升序）；统一输出：``StrategyResult``。
注册在文件底部 ``register_strategy`` 完成 —— 新策略 = 新函数 + 一行注册。
"""
from __future__ import annotations

import polars as pl

from lquant.portfolio.strategies.registry import _no, _ok

# 策略是否只看「最后一根 K 线」之后的截面 —— 全部是当日快照型判定，
# 与 screener 的「先过滤后打分」互补：screener 排除买不到的，这里找形态。


def _run_volume_surge(df: pl.DataFrame, *, vol_ratio: float = 2.0,
                      min_amount: float = 2e8, min_pct: float = 0.0) -> pl.DataFrame:
    """放量上涨（InStock 同名策略）：
    1. 当日上涨且收阳（close > open）；
    2. 成交量 >= vol_ratio × 5 日均量；
    3. 成交额 >= min_amount（默认 2 亿，InStock 口径；小资金可调低）。
    """
    last = df.tail(1).row(0, named=True)
    vol_ma5 = df["volume"].tail(6).head(5).mean()
    amount = last["amount"] or (last["close"] * last["volume"])
    if last["close"] <= last["open"] or last["close"] <= (last["pre_close"] or 0):
        return _no("volume_surge",
                   f"未收阳: open={last['open']} close={last['close']} "
                   f"pre_close={last['pre_close']}")
    ratio = last["volume"] / vol_ma5 if vol_ma5 else 0.0
    if ratio < vol_ratio:
        return _no("volume_surge",
                   f"量比不足: {ratio:.2f}x < {vol_ratio}x（5日均量 {vol_ma5:,.0f}）")
    if amount < min_amount:
        return _no("volume_surge",
                   f"成交额不足: {amount/1e8:.2f}亿 < {min_amount/1e8:.0f}亿")
    return _ok("volume_surge",
               f"量比 {ratio:.2f}x，成交额 {amount/1e8:.2f}亿，"
               f"收阳涨幅 {(last['close']/last['pre_close']-1)*100:.2f}%")


def _run_keep_rising(df: pl.DataFrame, *, days: int = 4) -> pl.DataFrame:
    """持续上涨（InStock: keep_increasing）：最近 days 日收盘逐日抬升。"""
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
