"""缠论日线适配层。

本文件的核心价值是 **前缀不变性**（``test_prefix_invariance_*``）：缠论接入最容易
被绕过、也最必须守住的一条。实测 czsc 自带的 ``finished_bis`` 不满足该性质
（26 检查点 3 处违例），本模块改用 ``bi_list[:-1]`` + 自建 ``get_zs_seq`` 规避。
如果哪天有人把它改回 ``finished_bis``，这几个用例必须红。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.indicators.chan import (
    DEFAULT_SIGNAL_KINDS,
    ChanUnavailable,
    _detail,
    _signals,
    _to_pandas,
    _transition_idx,
    _v1,
    compute_chan,
    tail_result,
    warmup,
)

pytest.importorskip("czsc", reason="缠论依赖 czsc（optional extra: chan）")


def make_daily(n: int = 400, seed: int = 3) -> pl.DataFrame:
    """确定性合成日线（带趋势的随机游走，保证能走出分型/笔/中枢）。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.02, n) + np.sin(np.arange(n) / 47) * 0.006
    close = 20 * np.exp(np.cumsum(ret))
    return pl.DataFrame({
        "trade_date": [date(2021, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * (1 + abs(rng.normal(0, 0.009, n))),
        "low": close * (1 - abs(rng.normal(0, 0.009, n))),
        "close": close,
        "volume": rng.integers(100_000, 500_000, n).astype(float),
    })


@pytest.fixture(scope="module")
def bars() -> pl.DataFrame:
    return make_daily(400)


@pytest.fixture(scope="module")
def result(bars: pl.DataFrame):
    return compute_chan(bars, symbol="000001.SZ")


# ---------------------------------------------------------------- 契约


def test_contract_shapes(result) -> None:
    assert result.symbol == "000001.SZ"
    assert result.asof == date(2021, 1, 1) + timedelta(days=399)
    assert result.n_bars == 400
    assert result.bi, "至少应有一笔"
    assert result.notes, "口径说明必须非空"


def test_bi_directions_alternate(result) -> None:
    dirs = [b.direction for b in result.bi]
    assert all(d in ("up", "down") for d in dirs)
    assert all(dirs[i] != dirs[i + 1] for i in range(len(dirs) - 1)), dirs


def test_bi_endpoints_are_ordered_and_inside_range(result) -> None:
    for b in result.bi:
        assert b.sdt <= b.edt
        assert b.low <= b.high
        assert b.n_bars >= 1
    for prev, nxt in zip(result.bi, result.bi[1:], strict=False):
        assert prev.edt <= nxt.sdt, "笔必须按时间串联"


def test_fx_sorted_unique_and_alternating(result) -> None:
    dts = [f.dt for f in result.fx]
    assert dts == sorted(dts)
    assert len(dts) == len(set(dts)), "分型按 dt 去重"
    marks = [f.mark for f in result.fx]
    assert all(m in ("G", "D") for m in marks)
    assert all(marks[i] != marks[i + 1] for i in range(len(marks) - 1)), marks


def test_zs_valid_and_filtered(result) -> None:
    assert result.zs, "至少应有一个中枢"
    for z in result.zs:
        assert z.n_bis >= 3, "退化中枢（不足 3 笔）必须被过滤"
        assert z.zg >= z.zd, "中枢上沿不得低于下沿"
        assert z.dd <= z.zd <= z.zg <= z.gg, "gg/dd 必须包住 zd/zg"
        assert z.sdt <= z.edt


def test_latest_keys_cover_all_kinds(result) -> None:
    assert set(result.latest) == set(DEFAULT_SIGNAL_KINDS)
    for v in result.latest.values():
        assert v is None or isinstance(v, str)


def test_tail_result_filters_to_window(result) -> None:
    """``since`` 之后无结构 → 各列表为空，但 latest/notes 原样保留。"""
    tailed = tail_result(result, date(2099, 1, 1))
    assert tailed.bi == () and tailed.zs == () and tailed.fx == () and tailed.events == ()
    assert tailed.latest == result.latest
    assert tailed.notes == result.notes
    assert tailed.asof == result.asof


def test_tail_result_keeps_spanning_bi(result) -> None:
    """跨进窗口的笔要保留（按结束日过滤，不是起始日）。"""
    mid = result.bi[len(result.bi) // 2]
    tailed = tail_result(result, mid.edt)
    assert any(b.sdt < mid.edt for b in tailed.bi), "起始日早于窗口但结束日在窗口内的笔被误删"
    assert all(b.edt >= mid.edt for b in tailed.bi)


def test_tail_result_is_monotone(result) -> None:
    """窗口越晚，剩下的结构越少。"""
    counts = [len(tail_result(result, d).bi)
              for d in (date(2021, 1, 1), date(2021, 6, 1), date(2021, 12, 1))]
    assert counts == sorted(counts, reverse=True), counts


def test_signal_tail_does_not_change_values(bars: pl.DataFrame) -> None:
    """``signal_tail`` 是纯性能开关：尾部取值必须与全量算逐条一致。"""
    full = compute_chan(bars)
    tailed = compute_chan(bars, signal_tail=100)
    assert tailed.latest == full.latest
    cutoff = bars["trade_date"][-100]
    assert {(e.dt, e.kind, e.detail) for e in tailed.events} == \
           {(e.dt, e.kind, e.detail) for e in full.events if e.dt >= cutoff}
    # 结构不受 signal_tail 影响
    assert tailed.bi == full.bi and tailed.zs == full.zs


def test_to_dict_is_json_ready(result) -> None:
    d = result.to_dict()
    assert json.loads(json.dumps(d)) == d  # 必须可序列化
    assert d["available"] is True
    assert d["freq"] == "日线"
    assert isinstance(d["asof"], str)
    assert set(d) >= {"symbol", "asof", "n_bars", "fx", "bi", "zs", "signals", "notes"}
    assert set(d["signals"]) == {"latest", "events"}
    for row in d["bi"]:
        assert isinstance(row["sdt"], str) and isinstance(row["edt"], str)


# ---------------------------------------------------------------- 前缀不变性（核心）


@pytest.fixture(scope="module")
def full(bars: pl.DataFrame):
    return compute_chan(bars)


def test_prefix_invariance_bi(full, bars: pl.DataFrame) -> None:
    """已确认的笔在后续数据到来后必须**逐字不变**。"""
    full_set = {(b.sdt, b.edt, b.direction, b.high, b.low) for b in full.bi}
    for t in range(150, len(bars), 40):
        pre = compute_chan(bars.head(t))
        for b in pre.bi:
            key = (b.sdt, b.edt, b.direction, b.high, b.low)
            assert key in full_set, f"t={t} 的笔 {b} 在全量结果中不存在或已改变"


def test_prefix_invariance_zs(full, bars: pl.DataFrame) -> None:
    """中枢按 ``sdt`` 对齐后 ``zg``/``zd`` 必须不变（``edt``/``n_bis`` 的延伸不算违例）。"""
    full_map = {z.sdt: (z.zg, z.zd) for z in full.zs}
    for t in range(150, len(bars), 40):
        pre = compute_chan(bars.head(t))
        for z in pre.zs:
            assert z.sdt in full_map, f"t={t} 的中枢 {z.sdt} 在全量结果中不存在"
            assert full_map[z.sdt] == (z.zg, z.zd), f"t={t} 的中枢 {z.sdt} 的区间发生了漂移"


def test_prefix_invariance_signal_events(full, bars: pl.DataFrame) -> None:
    """已发生的信号沿变点不得随后续数据消失或改变。"""
    full_set = {(e.dt, e.kind, e.label, e.detail) for e in full.events}
    for t in range(150, len(bars), 40):
        pre = compute_chan(bars.head(t))
        for e in pre.events:
            assert (e.dt, e.kind, e.label, e.detail) in full_set, (
                f"t={t} 的信号事件 {e} 在全量结果中不存在或已改变")


# ---------------------------------------------------------------- 沿变检测（纯函数）


def test_transition_idx_collapses_runs() -> None:
    seq = ["其他", "其他", "二卖", "二卖", "二卖", "其他", "二买", "二买"]
    assert _transition_idx(seq, ("二卖",)) == [2]
    assert _transition_idx(seq, ("二买",)) == [6]
    assert _transition_idx(seq, ("二买", "二卖")) == [2, 6]


def test_transition_idx_fires_on_value_change() -> None:
    # 上涨 → 下跌 是两个不同的信号，各记一次
    assert _transition_idx(["上涨", "下跌", "下跌"], ("上涨", "下跌")) == [0, 1]


def test_transition_idx_ignores_empty() -> None:
    assert _transition_idx([], ("二买",)) == []
    assert _transition_idx(["其他", "其他"], ("二买",)) == []


def test_events_are_deduped_and_sorted(result) -> None:
    keys = [(e.dt, e.kind) for e in result.events]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys)), "同一天同一信号只应有一条事件"
    assert len(result.events) < result.n_bars, "状态必须被折叠成沿变点，不能逐日铺开"


# ---------------------------------------------------------------- 输入校验与降级


def test_to_pandas_maps_columns(bars: pl.DataFrame) -> None:
    pdf = _to_pandas(bars, "600519.SH")
    assert list(pdf.columns) == ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    assert pdf["symbol"].iloc[0] == "600519.SH"
    assert len(pdf) == len(bars)


def test_to_pandas_amount_fallback() -> None:
    """缺 ``amount`` 时用 ``close × volume`` 兜底（czsc 强制要求 8 列）。"""
    df = make_daily(40).drop("volume").with_columns(
        pl.Series("volume", np.full(40, 1000.0)))
    pdf = _to_pandas(df)
    assert np.allclose(pdf["amount"].to_numpy(),
                       pdf["close"].to_numpy() * pdf["vol"].to_numpy())


def test_short_bars_raises() -> None:
    with pytest.raises(ChanUnavailable, match="不足"):
        compute_chan(make_daily(10))


def test_missing_columns_raises(bars: pl.DataFrame) -> None:
    with pytest.raises(ChanUnavailable, match="缺列"):
        compute_chan(bars.drop("volume"))


def test_unknown_signal_kind_raises(bars: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="未知信号种类"):
        compute_chan(bars, signal_kinds=("nope",))


def test_unavailable_when_czsc_missing(monkeypatch, bars: pl.DataFrame) -> None:
    """未安装 czsc 时给**可执行**的补救提示，而不是炸掉整个页面。"""
    monkeypatch.setitem(sys.modules, "czsc", None)  # 令 `import czsc` 抛 ImportError
    with pytest.raises(ChanUnavailable, match="uv sync --extra chan"):
        compute_chan(bars)


def test_warmup_succeeds_with_czsc_installed() -> None:
    """预热成功返回 True（服务启动钩子据此判断要不要提示降级）。"""
    assert warmup() is True


def test_warmup_returns_false_without_czsc(monkeypatch) -> None:
    """未装 czsc 时预热返回 False 而不是抛异常 —— optional extra 没装不是错误。"""
    monkeypatch.setitem(sys.modules, "czsc", None)
    assert warmup() is False


def test_empty_bars_raises() -> None:
    with pytest.raises(ChanUnavailable):
        compute_chan(make_daily(40).head(0))


def test_too_few_bis_raises() -> None:
    """日线够 MIN_BARS 但走不出「已确认的笔」（``bi_list[:-1]`` 为空）时必须降级。

    ``seed=3 / n=40`` 是实测出来的确定性退化点（``bi_list`` 恰有 1 笔，
    丢末笔后为空），不是随手挑的数字。
    """
    with pytest.raises(ChanUnavailable, match="已确认的笔为空"):
        compute_chan(make_daily(40, seed=3))


def test_no_signal_kinds_returns_empty_signals(bars: pl.DataFrame) -> None:
    r = compute_chan(bars, signal_kinds=())
    assert r.latest == {}
    assert r.events == ()
    assert r.bi, "结构部分不受信号种类影响"


# ---------------------------------------------------------------- 信号解析（纯函数）


def test_v1_picks_first_non_placeholder() -> None:
    assert _v1({"c": "二买_任意_任意_0"}, ["c"]) == "二买"
    assert _v1({"c": "其他_任意_任意_0"}, ["c"]) == "其他"
    assert _v1({"c": "其他_任意_任意_0", "d": "三买_6笔_任意_0"}, ["c", "d"]) == "三买"
    assert _v1({}, ["missing"]) == "其他"


def test_detail_extracts_v2_and_skips_placeholder() -> None:
    assert _detail({"c": "三买_6笔_任意_0"}, ["c"], "三买") == "6笔"
    assert _detail({"c": "二买_任意_任意_0"}, ["c"], "二买") == "", "「任意」不是有效明细"
    assert _detail({"c": "二卖_任意_任意_0"}, ["c"], "二买") == "", "取值不匹配时无明细"


class _StubCzsc:
    """让 ``generate_czsc_signals`` 返回空——覆盖「信号行数为 0」的降级分支。"""

    @staticmethod
    def generate_czsc_signals(*_a, **_kw):
        return []


class _StubBar:
    """``_signals`` 只用 ``bars[0].dt`` 拼 ``sdt``。"""

    dt = "2021-01-01 00:00:00"


def test_signals_degrade_when_no_rows() -> None:
    latest, events = _signals(_StubCzsc(), [_StubBar()], DEFAULT_SIGNAL_KINDS)
    assert set(latest) == set(DEFAULT_SIGNAL_KINDS)
    assert all(v is None for v in latest.values())
    assert events == ()
