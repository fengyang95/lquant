"""选股策略库单测：注册表纪律 / 各策略命中与落选的 evidence / 数据不足降级。

全部离线：合成日线 DataFrame，不读数据湖。
"""

import polars as pl
import pytest

from lquant.portfolio.strategies import (
    STRATEGIES,
    StrategyResult,
    run_all,
    run_strategy,
)


def make_df(
    closes: list[float],
    volumes: list[float] | None = None,
    amounts: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pl.DataFrame:
    """按收盘序列合成日线：open=前收，pre_close=前收；尾日可覆盖量额。"""
    n = len(closes)
    volumes = volumes or [1e6] * n
    amounts = amounts or [closes[-1] * 1e6] * n
    highs = highs or [max(closes[max(i - 1, 0)], closes[i]) * 1.001 for i in range(n)]
    lows = lows or [min(closes[max(i - 1, 0)], closes[i]) * 0.999 for i in range(n)]
    return pl.DataFrame(
        {
            "trade_date": [f"d{i:03d}" for i in range(n)],
            "open": [closes[max(i - 1, 0)] for i in range(n)],
            "high": highs,
            "low": lows,
            "close": closes,
            "pre_close": [closes[max(i - 1, 0)] for i in range(n)],
            "volume": volumes,
            "amount": amounts,
        }
    )


# ---------- 注册表纪律 ----------


def test_registry_has_instock_five():
    assert set(STRATEGIES.keys()) >= {
        "volume_surge",
        "keep_rising",
        "pullback_ma250",
        "turtle_breakout",
        "low_atr",
    }


def test_describe_exposes_meta_for_frontend():
    items = STRATEGIES.describe()
    labels = {i["name"]: i["label"] for i in items}
    assert labels["volume_surge"] == "放量上涨"
    assert all("min_rows" in i for i in items)


def test_unknown_strategy_fails_loudly():
    with pytest.raises(KeyError, match="未知策略"):
        run_strategy("bogus_name", make_df([10.0] * 30))


# ---------- 各策略 ----------


def test_volume_surge_hits_with_2x_volume():
    closes = [10.0] * 9 + [10.5]
    vols = [1e6] * 9 + [2.5e6]
    amounts = [1e7] * 9 + [2.5e8]  # 2.5 亿 ≥ 2 亿
    r = run_strategy("volume_surge", make_df(closes, vols, amounts))
    assert r.passed and "量比" in r.evidence and "2.50" in r.evidence


def test_volume_surge_misses_on_low_ratio():
    closes = [10.0] * 9 + [10.5]
    r = run_strategy("volume_surge", make_df(closes))  # 等量 → 量比 1
    assert not r.passed and "量比不足" in r.evidence


def test_volume_surge_misses_on_yin_close():
    closes = [10.0] * 9 + [10.5]
    df = make_df(closes, [1e6] * 9 + [2.5e6], [1e7] * 9 + [2.5e8])
    # 把尾日改成收阴：close < open（open=前收 10.0 → close 改 9.9 需同步 high/low）
    df = df.with_columns(
        [
            pl.when(pl.arange(0, pl.len()) == pl.len() - 1)
            .then(pl.lit(10.6))
            .otherwise(pl.col("open"))
            .alias("open"),
            pl.when(pl.arange(0, pl.len()) == pl.len() - 1)
            .then(pl.lit(9.9))
            .otherwise(pl.col("close"))
            .alias("close"),
        ]
    )
    r = run_strategy("volume_surge", df)
    assert not r.passed and "未收阳" in r.evidence


def test_keep_rising_four_days():
    r = run_strategy("keep_rising", make_df([10.0, 10.1, 10.2, 10.3, 10.5]))
    assert r.passed and "连续 4 日上涨" in r.evidence


def test_keep_rising_fails_on_flat():
    r = run_strategy("keep_rising", make_df([10.0, 10.1, 10.1, 10.2, 10.3]))
    assert not r.passed and "未上涨" in r.evidence


def test_turtle_breakout_new_high():
    closes = [10.0 + i * 0.1 for i in range(21)]
    highs = [c + 0.05 for c in closes]
    r = run_strategy("turtle_breakout", make_df(closes, highs=highs))
    assert r.passed and "突破 20 日高点" in r.evidence


def test_turtle_breakout_misses_below_high():
    closes = [10.0 + i * 0.1 for i in range(21)]
    closes[-1] = 10.5  # 远低于前 20 日高点
    highs = [c + 0.05 for c in closes]
    r = run_strategy("turtle_breakout", make_df(closes, highs=highs))
    assert not r.passed and "未突破" in r.evidence


def test_pullback_ma250_in_band():
    # 年线 10.0，尾日 10.2（+2% 在 0~3% 回踩带内）
    closes = [10.0] * 249 + [10.2]
    r = run_strategy("pullback_ma250", make_df(closes))
    assert r.passed and "回踩确认区" in r.evidence


def test_pullback_ma250_too_far():
    closes = [10.0] * 249 + [15.0]  # +50% 远离年线
    r = run_strategy("pullback_ma250", make_df(closes))
    assert not r.passed and "偏离年线过远" in r.evidence


def test_low_atr_boundary():
    # 恒定 close + high=low=close → ATR≈0 → 必然低波动
    closes = [10.0] * 15
    df = make_df(closes, highs=closes, lows=closes)
    r = run_strategy("low_atr", df)
    assert r.passed and "低波动" not in r.evidence and "ATR" in r.evidence


# ---------- 降级与健壮性 ----------


def test_insufficient_data_is_explicit_not_false():
    r = run_strategy("pullback_ma250", make_df([10.0] * 30))
    assert not r.passed and r.data_insufficient
    assert "数据不足" in r.evidence and "250" in r.evidence


def test_run_all_never_raises_and_covers_all():
    df = make_df([10.0] * 30)
    results = run_all(df)
    assert len(results) == len(STRATEGIES.keys())
    assert all(isinstance(r, StrategyResult) for r in results)
    # 每条 evidence 非空 —— 不静默纪律的落点
    assert all(r.evidence for r in results)


# ---------- 审阅修复回归：公共参数按签名过滤 ----------


def test_run_all_filters_common_params_by_signature():
    """run_all(df, min_amount=...) 只该传给认识它的策略。

    回归：此前 min_amount 被原样转发给全部策略，keep_rising 等无此形参的
    策略抛 TypeError 被 except 吞成「计算失败」—— 表面不崩实则 3/5 全坏。
    """
    df = make_df([10.0 + i * 0.05 for i in range(30)])
    results = run_all(df, min_amount=2e8)
    by_name = {r.strategy: r for r in results}
    for name, r in by_name.items():
        assert "计算失败" not in r.evidence, (name, r.evidence)
    # 认这个参数的策略不受影响：仍然生效（amount 不足 → 未命中原因是成交额）
    poor = make_df([10.0] * 9 + [10.5], volumes=[1e6] * 9 + [2.5e6], amounts=[1e7] * 9 + [1.5e8])
    r = run_strategy("volume_surge", poor, min_amount=2e8)
    assert not r.passed and "成交额不足" in r.evidence
