"""``market.regime``：5 档市场状态 + 6 阶段情绪周期。

这里守四条命门（对应模块 docstring）：

1. **分位标定**：阈值是配置而非硬编码，且标定行为可复现（``quantile_band``）。
2. **缺失 ≠ 0**：``nan/inf/None`` → 中性 50，0 是"最看空"的断言而不是"不知道"。
3. **切换要平滑 + 确认**：单日噪声不翻转阶段；连续 2 日同向才切换。
4. **无未来函数**：``as_of=d`` 的结果与"把 >d 的行截掉再算"逐元素一致。

阶段规则用 ``dataclasses.replace`` 把 EMA 关掉（``ema_alpha=1.0``）来单独验证
"确认"机制 —— 否则平滑本身就会把单日尖峰抹平，测不出确认是否真的在起作用。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.market import regime as R

D0 = date(2026, 6, 1)
# 测试日期一律落在过去（相对仓库运行时钟），避免 as_of 默认值把数据截没。
FAR = date(2099, 1, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _ladder(
    ge2: list[int] | None = None,
    *,
    height: int = 3,
    first: int = 20,
    promo: float | None = 0.20,
    seal: float | None = 0.60,
    n: int = 8,
) -> pl.DataFrame:
    """直接构造梯队列（绕开涨停池），用来单独验证阶段规则。"""
    g2 = list(ge2) if ge2 is not None else [5] * n
    return pl.DataFrame(
        {
            "trade_date": _days(len(g2)),
            "max_consecutive": [height] * len(g2),
            "first_board": [first] * len(g2),
            "ge2_count": g2,
            "promo_rate": [promo] * len(g2),
            "seal_rate": [seal] * len(g2),
        }
    )


def _pool(counts: list[int], *, broken: bool = True, reuse: bool = True) -> pl.DataFrame:
    """合成涨停池：``counts[i]`` = 第 i 个交易日的涨停家数。

    ``reuse=True`` 时各日复用同一批 symbol（可构造连板 / 高晋级率）；
    ``reuse=False`` 时每天换一批（晋级率恒为 0，但那是**真实的 0**）。
    ``broken=True`` 时每 5 只里插一只炸板行（``limit_up_type`` 为 NULL）。
    """
    rows: list[dict] = []
    base = 600000
    for i, n in enumerate(counts):
        d = D0 + timedelta(days=i)
        off = 0 if reuse else i * 1000
        for k in range(n):
            is_broken = broken and k % 5 == 0
            rows.append(
                {
                    "trade_date": d,
                    "symbol": f"{base + off + k * 3:06d}.SH",
                    "limit_up_type": None if is_broken else "换手板",
                }
            )
    return pl.DataFrame(rows)


# ─────────────────────── 一、子分标定与配置化 ───────────────────────


def test_score_component_maps_band_to_0_100_and_clamps():
    assert R.score_component(0.21, 0.21, 0.75) == 0.0
    assert R.score_component(0.75, 0.21, 0.75) == 100.0
    assert R.score_component(0.48, 0.21, 0.75) == pytest.approx(50.0)
    # 越界钳制：极端行情不该把分数打到区间外（否则一次股灾会污染整年分布）
    assert R.score_component(-5.0, 0.21, 0.75) == 0.0
    assert R.score_component(99.0, 0.21, 0.75) == 100.0
    # invert：值越大越差（大跌股占比）
    assert R.score_component(0.02, 0.02, 0.18, invert=True) == 100.0
    assert R.score_component(0.18, 0.02, 0.18, invert=True) == 0.0


def test_quantile_band_is_linear_interpolation_p15_p85():
    band = R.quantile_band(range(1, 102))  # 1..101，101 个样本
    assert (band.low, band.high) == (16.0, 86.0)
    # 非法分位区间 fail loudly，而不是静默返回一个颠倒的区间
    with pytest.raises(ValueError):
        R.quantile_band([1, 2, 3], q_low=0.9, q_high=0.1)
    with pytest.raises(ValueError):
        R.quantile_band([1])  # 样本不足 → 分位数不可靠


def test_thresholds_are_configurable_and_shift_scores():
    """同一份指标，换标定区间 → 子分与档位都跟着变（证明阈值真的可配置）。"""
    metrics = {
        "up_ratio": 0.5, "median_change": 0.2, "limit_up_count": 50,
        "seal_ratio": 0.6, "max_consecutive": 5, "strong_down_ratio": 0.05,
        "index_change": 0.0, "above_ma20_ratio": 0.4,
    }
    base = R.classify_regime(metrics)

    # up_ratio 的 band 收窄到 [0.45, 0.55]：同一个 0.5 从"中上"变成"正中偏上"
    tight = replace(
        R.DEFAULT_REGIME_THRESHOLDS,
        profit=(R.Component("up_ratio", 0.45, 0.55, 1.0),),
    )
    assert R.classify_regime(metrics, tight).profit != pytest.approx(base.profit)

    # 切点整体抬高 → 同一分数落到更低的档
    strict = replace(R.DEFAULT_REGIME_THRESHOLDS, strong_cut=99.0, lean_strong_cut=98.0)
    assert R.classify_regime(metrics, strict).state in {"range", "lean_weak", "weak"}
    assert base.state != "weak"  # 默认标定下这份指标不该是最弱档


def test_thresholds_reject_inconsistent_config():
    with pytest.raises(ValueError):  # 切点必须递减
        R.RegimeThresholds(strong_cut=40.0)
    with pytest.raises(ValueError):  # 子分内权重和必须为 1
        R.RegimeThresholds(profit=(R.Component("up_ratio", 0, 1, 0.5),))
    with pytest.raises(ValueError):  # low >= high：该分量会永远钉在中性 50
        R.RegimeThresholds(
            profit=(R.Component("up_ratio", 0.5, 0.5, 1.0),)
        )
    with pytest.raises(ValueError):  # 子分权重和必须为 1
        R.RegimeThresholds(weights={"profit": 1.0, "speculation": 1.0,
                                    "resilience": 1.0, "trend": 1.0})
    with pytest.raises(ValueError):  # 阶段阈值自校验
        R.PhaseThresholds(ema_alpha=0.0)
    with pytest.raises(ValueError):
        R.PhaseThresholds(confirm_days=0)
    with pytest.raises(ValueError):
        R.PhaseThresholds(promo_min_pool=0)


# ─────────────────────── 二、缺失语义：nan/inf → 50，不是 0 ───────────────────────


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_nonfinite_component_is_neutral_50(bad):
    assert R.score_component(bad, 0.21, 0.75) == 50.0
    # 0.0 是**真值**：它必须映射到区间下界，而不是被判成"缺失"
    assert R.score_component(0.0, 0.21, 0.75) == 0.0


def test_missing_component_is_not_treated_as_zero():
    """缺失 → 中性 50（"没有意见"），不是 0（"最看空"）。"""
    full = {
        "up_ratio": 0.6, "median_change": 0.9, "limit_up_count": 60,
        "seal_ratio": 0.7, "max_consecutive": 6, "strong_down_ratio": 0.03,
        "index_change": 1.0, "above_ma20_ratio": 0.6,
    }
    missing = R.classify_regime({**full, "up_ratio": None})
    as_zero = R.classify_regime({**full, "up_ratio": 0.0})
    assert missing.profit > as_zero.profit
    # 中性 50 那一票被显式记账，接线漏字段时看得见
    assert "up_ratio" in missing.neutral
    assert missing.neutral == ("up_ratio",)
    assert as_zero.neutral == ()


def test_all_missing_is_range_not_weak():
    """全维缺失 → 综合分 50 → 震荡。"不知道"既不是看多也不是看空。"""
    res = R.classify_regime({})
    assert res.score == pytest.approx(50.0)
    assert res.state == "range"
    assert set(res.neutral) == {
        "up_ratio", "median_change", "limit_up_count", "seal_ratio",
        "max_consecutive", "strong_down_ratio", "index_change", "above_ma20_ratio",
    }


def test_non_numeric_component_fails_loudly():
    with pytest.raises(TypeError):
        R.score_component("涨", 0.0, 1.0)
    with pytest.raises(TypeError):
        R.score_component(True, 0.0, 1.0)


def test_series_counts_neutral_components():
    df = pl.DataFrame(
        {
            "trade_date": _days(3),
            "up_ratio": [0.5, None, 0.5],
            "median_change": [0.1, None, 0.1],
            "limit_up_count": [50, 0, 50],
            "seal_ratio": [0.6, 0.6, 0.6],
            "max_consecutive": [5, 5, 5],
            "strong_down_ratio": [0.05, 0.05, 0.05],
            "index_change": [0.0, 0.0, 0.0],
            "above_ma20_ratio": [0.4, 0.4, 0.4],
        }
    )
    out = R.classify_regime_series(df, as_of=FAR)
    assert out["neutral_count"].to_list() == [0, 2, 0]
    assert out["score"][1] != out["score"][2]  # 缺失那天的分数确实不同


# ─────────────────────── 三、连板梯队（纯日线派生） ───────────────────────


def test_consecutive_limit_ups_requires_adjacent_session():
    """周五上榜、下周三再上榜不算连板 —— 中间隔着周一/周二两个会话。"""
    rows = [
        {"trade_date": date(2026, 6, 5), "symbol": "600000.SH", "limit_up_type": "换手板"},
        # 别的票在周一开市，让周一进入"会话序列"
        {"trade_date": date(2026, 6, 8), "symbol": "600001.SH", "limit_up_type": "换手板"},
        {"trade_date": date(2026, 6, 10), "symbol": "600000.SH", "limit_up_type": "换手板"},
    ]
    out = R.consecutive_limit_ups(pl.DataFrame(rows)).sort(["symbol", "trade_date"])
    got = out.filter(pl.col("symbol") == "600000.SH")["consecutive_limit_ups"].to_list()
    assert got == [1, 1]  # 不是 [1, 2]
    # 相邻会话才累加
    adj = pl.DataFrame(
        [
            {"trade_date": date(2026, 6, 8), "symbol": "600000.SH", "limit_up_type": "换手板"},
            {"trade_date": date(2026, 6, 9), "symbol": "600000.SH", "limit_up_type": "换手板"},
        ]
    )
    assert R.consecutive_limit_ups(adj)["consecutive_limit_ups"].to_list() == [1, 2]


def test_broken_pool_rows_are_excluded_from_ladder():
    """炸板行（limit_up_type 为 NULL）不算涨停，否则连板高度系统性虚高。"""
    pool = pl.DataFrame(
        [
            {"trade_date": D0, "symbol": "600000.SH", "limit_up_type": "换手板"},
            {"trade_date": D0, "symbol": "600001.SH", "limit_up_type": None},  # 炸板
        ]
    )
    ladder = R.ladder_daily(pool, as_of=FAR)
    assert ladder["first_board"].to_list() == [1]
    assert ladder["pool_size"].to_list() == [1]
    assert ladder["seal_rate"].to_list() == [0.5]  # 真封板 1 / 池内 2


def test_ladder_fails_loudly_on_missing_columns_or_all_broken():
    with pytest.raises(ValueError, match="缺列"):
        R.consecutive_limit_ups(pl.DataFrame({"trade_date": [D0], "symbol": ["600000.SH"]}))
    # 全是炸板行 = 涨停池没采到，与"今天 0 家涨停"是相反结论 → 必须报错
    all_broken = pl.DataFrame(
        [{"trade_date": D0, "symbol": "600000.SH", "limit_up_type": None}]
    )
    with pytest.raises(ValueError, match="涨停池采集缺失"):
        R.consecutive_limit_ups(all_broken)


def test_ladder_completeness_and_pool_too_small_gives_null_promo():
    # 池子过小：晋级率必须是 null，而不是 0 或 33% 这种噪声比率
    small = _pool([3, 3, 3], broken=False, reuse=True)
    lad = R.ladder_daily(small, as_of=FAR)
    assert lad["promo_pool"].to_list() == [None, 3, 3]
    assert lad["promo_rate"].to_list() == [None, None, None]

    # 池子够大：晋级率 0 是**真实观测**（昨日有人气、今日全断），必须保留 0
    zero = _pool([20, 20], broken=False, reuse=False)
    lad0 = R.ladder_daily(zero, as_of=FAR)
    assert lad0["promo_pool"].to_list() == [None, 20]
    assert lad0["promo_rate"].to_list() == [None, 0.0]


def test_ladder_completeness_counts_filled_rungs():
    """最后一天同时存在 1/2/3/4/5 连板 → 2..5 四个档位全填 → 完整度 1.0。

    构造：第 s 板的那条链从窗口倒数第 s 天开始，于是所有链都在最后一天交汇。
    """
    rows = []
    for s in (1, 2, 3, 4, 5):
        for j in range(s):
            rows.append(
                {
                    "trade_date": D0 + timedelta(days=5 - s + j),
                    "symbol": f"{600000 + s * 10:06d}.SH",
                    "limit_up_type": "换手板",
                }
            )
    lad = R.ladder_daily(pl.DataFrame(rows), as_of=FAR).sort("trade_date")
    last = lad.tail(1).to_dicts()[0]
    assert last["max_consecutive"] == 5
    assert last["rungs_filled"] == 4
    assert last["ladder_completeness"] == pytest.approx(1.0)
    # 只有高度 2 时，完整度定义为 0（没有 2..height 的中间档位可填）
    assert lad.head(1).to_dicts()[0]["ladder_completeness"] == 0.0


def test_phase_series_requires_ladder_columns():
    with pytest.raises(ValueError, match="缺列"):
        R.classify_phase_series(pl.DataFrame({"trade_date": [D0]}), as_of=FAR)


# ─────────────────────── 四、平滑 + 连续确认：单日噪声不翻转 ───────────────────────


def test_single_day_spike_does_not_flip_phase():
    """只有 1 天变化的序列，标签必须不变（EMA 关掉，单独验证确认机制）。

    多个尖峰位置/幅度都试，避免"只挑了一组幸运数据"。
    （序列**第 1 天**必须是例外：那时没有"上一个标签"可供确认，起点只能取
    当天原始标签 —— 见 ``test_first_day_has_no_previous_label_to_confirm_against``。）
    """
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    for spike in (
        [5, 5, 5, 60, 5, 5, 5, 5],
        [5, 5, 5, 5, 5, 5, 60, 5],
        [5, 5, 5, 15, 5, 5, 5, 5],
        [5, 5, 5, 8, 5, 5, 5, 5],
    ):
        out = R.classify_phase_series(_ladder(spike), thresholds=th, as_of=FAR)
        assert set(out["phase"].to_list()) == {"ice"}, f"单日噪声翻转了阶段：{spike}"


def test_first_day_has_no_previous_label_to_confirm_against():
    """第 1 天没有可比对的"上一个标签"，只能取当天原始标签 —— 显式锁定。"""
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    out = R.classify_phase_series(_ladder([60, 5, 5, 5]), thresholds=th, as_of=FAR)
    # 起点取当天原始标签 climax；随后回落到冰点同样要连等 2 天才切
    assert out["phase"].to_list() == ["climax", "climax", "ice", "ice"]


def test_ema_tail_is_a_known_single_day_spike_channel():
    """**已知边界，如实锁定，不假装不存在**。

    默认 ``α=1/3`` 下，一个远离基线的单日尖峰（宽度 5 → 60）会把平滑后的宽度
    拖高约 ``lookback`` 天，于是"启动"的 ``g2 - g2_prev >= 3`` 连续为真 ——
    这时 ``confirm_days`` 挡不住它（它挡的是"原始标签只持续 1 天"，而 EMA 尾巴
    让原始标签持续了 4~5 天）。同一个尖峰在 ``α=1.0`` 下只影响 1 天，被确认
    机制干净挡住（见上一个测试）。

    要收紧这条路，调小 ``ema_alpha``（如 1/5）或抬高 ``ignite_ge2_delta``；
    本测试把当前行为钉住，任何修改必须显式同步这里与 ``PhaseThresholds`` 注释。
    """
    out = R.classify_phase_series(_ladder([5, 5, 5, 60, 5, 5, 5, 5]), as_of=FAR)
    phases = out["phase"].to_list()
    assert phases[3] == "ice"  # 尖峰当天：EMA 还没把平滑值推过阈值
    assert "ignite" in phases  # 但随后几天会被 EMA 尾巴带起来
    assert set(out["phase"].to_list()) != {"ice"}


def test_two_consecutive_days_do_confirm_and_switch():
    """确认是"等 2 天"，不是"永不切换" —— 连续 2 日同向必须切过去。"""
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    out = R.classify_phase_series(_ladder([5, 5, 5, 60, 60, 5, 5, 5]), thresholds=th, as_of=FAR)
    phases = out["phase"].to_list()
    assert phases[2] == "ice"  # 切换前
    assert phases[3] == "ice"  # 第 1 日：只记为 pending，不切
    assert phases[4] == "climax"  # 第 2 日：连续同向 → 确认切换
    assert phases[5] == "climax"  # 回落同样需要 2 日确认


def test_phase_priority_ice_beats_ebb():
    """长期死寂（高度/宽度/首板贴地）是冰点，不是"自高位退潮"。"""
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    # 晋级率极低 + 封板率低本可命中退潮规则 B，但冰点规则优先级更高
    out = R.classify_phase_series(
        _ladder([5, 5], height=3, first=10, promo=0.05, seal=0.40), thresholds=th, as_of=FAR
    )
    assert set(out["phase"].to_list()) == {"ice"}


def test_ema_alpha_one_is_identity_and_none_stays_none():
    assert R._ema([1.0, 2.0, 3.0], 1.0) == [1.0, 2.0, 3.0]
    # 缺失沿用上一平滑值；开头缺失用首个有效值回填（否则整段规则退化成兜底档）
    assert R._ema([None, 2.0, None, 4.0], 1.0) == [2.0, 2.0, 2.0, 4.0]
    # 全缺失 → 全 None，不伪造 0
    assert R._ema([None, float("nan")], 0.5) == [None, None]


# ─────────────────────── 五、大盘弱档否决 ───────────────────────


def test_weak_regime_vetoes_positive_phases():
    """梯队很强但大盘很弱 → 不得判为任何正面阶段。"""
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    ladder = _ladder([60, 60, 60, 60])
    dates = ladder["trade_date"].to_list()

    # 对照：同样梯队 + 大盘强 → 正常给出高潮
    ok = R.classify_phase_series(
        ladder, states=dict.fromkeys(dates, "strong"), thresholds=th, as_of=FAR
    )
    assert "climax" in ok["phase"].to_list()

    for weak_state in ("weak", "lean_weak"):
        out = R.classify_phase_series(
            ladder, states=dict.fromkeys(dates, weak_state), thresholds=th, as_of=FAR
        )
        assert set(out["phase"].to_list()).isdisjoint(R.POSITIVE_PHASES)
        assert set(out["phase"].to_list()) == {"repair"}
        assert out["vetoed"].to_list() == [True] * 4
        assert out["state"].to_list() == [weak_state] * 4


def test_veto_is_hard_even_with_confirmation_lag():
    """先强势确立高潮，再转弱：确认滞后不能把正面标签多留一天。"""
    th = replace(R.DEFAULT_PHASE_THRESHOLDS, ema_alpha=1.0)
    ladder = _ladder([60, 60, 60, 60, 60])
    dates = ladder["trade_date"].to_list()
    states = {dates[0]: "strong", dates[1]: "strong", dates[2]: "weak",
              dates[3]: "weak", dates[4]: "weak"}
    out = R.classify_phase_series(ladder, states=states, thresholds=th, as_of=FAR)
    phases = out["phase"].to_list()
    assert phases[2] == "repair"  # 硬否决：不给滞后留口子
    assert phases[3] == "repair"
    assert phases[4] == "repair"


def test_unknown_state_is_recorded_not_assumed():
    """没给大盘档位 → 不启用否决，但必须在 state_known 里记账，不静默。"""
    out = R.classify_phase_series(_ladder([5, 5]), as_of=FAR)
    assert out["state_known"].to_list() == [False, False]
    assert out["state"].to_list() == [None, None]
    assert out["vetoed"].to_list() == [False, False]


# ─────────────────────── 六、as_of 语义与无未来函数 ───────────────────────


def test_resolve_as_of_defaults_to_latest_completed_session(monkeypatch):
    monkeypatch.setattr(
        "lquant.core.sessions.latest_completed_session", lambda: date(2026, 6, 3)
    )
    assert R.resolve_as_of(None) == date(2026, 6, 3)
    assert R.resolve_as_of(date(2026, 1, 1)) == date(2026, 1, 1)  # 显式值原样透传


def test_as_of_excludes_later_rows():
    df = pl.DataFrame(
        {
            "trade_date": _days(5),
            "up_ratio": [0.5] * 5, "median_change": [0.1] * 5,
            "limit_up_count": [50] * 5, "seal_ratio": [0.6] * 5,
            "max_consecutive": [5] * 5, "strong_down_ratio": [0.05] * 5,
            "index_change": [0.0] * 5, "above_ma20_ratio": [0.4] * 5,
        }
    )
    out = R.classify_regime_series(df, as_of=_days(5)[2])
    assert out["trade_date"].to_list() == _days(3)
    # 默认 as_of 走 latest_completed_session：测试日期都在过去 → 不被截断
    assert R.classify_regime_series(df).height == 5


def _sample_pool() -> pl.DataFrame:
    return _pool([30, 34, 40, 26, 18, 12, 22, 35, 44, 50, 28, 16], broken=True)


def test_regime_series_prefix_invariance():
    """as_of=d 的结果 == 把 >d 的行截掉再算（逐元素一致）。"""
    df = pl.DataFrame(
        {
            "trade_date": _days(12),
            "up_ratio": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.6, 0.4, 0.2, 0.3, 0.5],
            "median_change": [-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 0.5, -0.5, -2.0, 0.0, 1.0],
            "limit_up_count": [20, 30, 50, 70, 90, 110, 130, 90, 60, 30, 20, 45],
            "seal_ratio": [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.7, 0.6, 0.5, 0.55, 0.65],
            "max_consecutive": [3, 4, 5, 6, 7, 8, 9, 7, 5, 3, 2, 4],
            "strong_down_ratio": [0.2, 0.15, 0.1, 0.05, 0.02, 0.01, 0.0, 0.05, 0.1,
                                  0.25, 0.15, 0.05],
            "index_change": [-2.0, -1.0, 0.0, 1.0, 2.0, 2.5, 3.0, 1.0, -1.0, -3.0, 0.0, 1.5],
            "above_ma20_ratio": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 0.7, 0.5, 0.2,
                                 0.3, 0.5],
        }
    )
    for cut in (_days(12)[3], _days(12)[6], _days(12)[-1]):
        live = R.classify_regime_series(df, as_of=cut)
        replay = R.classify_regime_series(
            df.filter(pl.col("trade_date") <= cut), as_of=FAR
        )
        assert live.equals(replay), f"regime 序列在 as_of={cut} 出现了未来函数"


def test_phase_series_prefix_invariance():
    """阶段链（连板派生 → EMA → 确认 → 否决）整体无未来函数。"""
    pool = _sample_pool()
    dates = sorted(set(pool["trade_date"].to_list()))
    # 故意让状态在窗口内出现翻转，把否决路径也纳入前缀不变性检查
    states = {d: ("weak" if i % 3 == 2 else "lean_strong") for i, d in enumerate(dates)}

    for cut in (dates[3], dates[7], dates[-1]):
        live = R.phase_history(pool, states=states, as_of=cut)
        replay = R.phase_history(
            pool.filter(pl.col("trade_date") <= cut), states=states, as_of=FAR
        )
        assert live.equals(replay), f"阶段序列在 as_of={cut} 出现了未来函数"

    # 顺带确认这条构造真的产生了非平凡输出（否则上面是"空序列相等"的假通过）
    full = R.phase_history(pool, states=states, as_of=FAR)
    assert full.height == len(dates)
    assert set(full["phase"].to_list()) - {R.PHASE_REPAIR}


def test_prefix_invariance_of_ladder_itself():
    pool = _sample_pool()
    dates = sorted(set(pool["trade_date"].to_list()))
    cut = dates[6]
    live = R.ladder_daily(pool, as_of=cut)
    replay = R.ladder_daily(pool.filter(pl.col("trade_date") <= cut), as_of=FAR)
    assert live.equals(replay)


# ─────────────────────── 七、口径常量自洽 ───────────────────────


def test_labels_and_state_set_are_consistent():
    assert set(R.REGIME_LABELS) == R.valid_state_set()
    assert set(R.PHASE_LABELS) == set(R.PHASES)
    assert set(R.PHASE_PRIORITY) == set(R.PHASES)  # 优先级表必须覆盖全部阶段
    assert set(R.PHASES) >= R.POSITIVE_PHASES
    assert R.VETO_FALLBACK not in R.POSITIVE_PHASES
    assert R.valid_state_set() >= R.VETO_STATES
