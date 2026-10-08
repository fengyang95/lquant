"""B4 研判闭环：不可变留痕、逐项对账、加权分数/覆盖率、窗口语义与统计汇总。

每个用例都把日线湖与 DuckDB 指到 tmp，绝不碰真实数据；交易日历显式写入，
因此不依赖本机时钟或真实节假日。参考实现只借思路（PolyForm Noncommercial），
本文件与 ``research/journal.py`` 的实现、口径均为 lquant 自有。
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

SYM_A = "600000.SH"
SYM_B = "600001.SH"
#: 60 只主板股票：让「涨停家数」这类**全市场截面**条件有真实可算的样本。
UNIVERSE = [f"6000{n:02d}.SH" for n in range(60)]


def _weekdays(start: date, n: int) -> list[date]:
    """从 ``start`` 起的 n 个工作日（测试里显式当交易日写进日历，避开真实调休）。"""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _dt(d: date, hour: int = 16, minute: int = 0) -> datetime:
    return datetime.combine(d, time(hour, minute))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离数据湖 + DuckDB，并建好全部 DDL 表。"""
    data = tmp_path / "data"
    (data / "parquet" / "daily").mkdir(parents=True)
    (data / "duckdb").mkdir(parents=True)
    monkeypatch.setenv("LQ_DATA_DIR", str(data))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(data / "duckdb" / "lquant.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    yield data
    get_settings.cache_clear()


def _seed_calendar(sessions: list[date]) -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.executemany(
            "INSERT OR REPLACE INTO trade_calendar (trade_date, is_open, exchange) "
            "VALUES (?, ?, ?)",
            [(d, True, "SSE") for d in sessions],
        )


def _seed_grid(sessions: list[date], symbols: list[str], pct_fn, start: float = 10.0) -> None:
    """按 ``pct_fn(symbol, day) -> 当日涨跌幅(%)`` 生成全市场日线。

    ``pre_close`` 用上一根收盘价，于是核验算出的 ``change_pct`` 恰好等于 pct_fn，
    手算对照时不必理会浮点复利。
    """
    from lquant.data.store.parquet import write_daily

    cols: dict[str, list] = {
        "symbol": [], "trade_date": [], "open": [], "high": [], "low": [],
        "close": [], "pre_close": [], "volume": [], "amount": [], "adj_factor": [],
    }
    for sym in symbols:
        close = start
        for d in sessions:
            prev = close
            close = close * (1.0 + pct_fn(sym, d) / 100.0)
            cols["symbol"].append(sym)
            cols["trade_date"].append(d)
            cols["open"].append(close)
            cols["high"].append(close)
            cols["low"].append(close)
            cols["close"].append(close)
            cols["pre_close"].append(prev)
            cols["volume"].append(1000.0)
            cols["amount"].append(close * 1000.0)
            cols["adj_factor"].append(1.0)
    df = pl.DataFrame(cols).with_columns(pl.col("trade_date").cast(pl.Date))
    write_daily(df)


def _scenario() -> list[date]:
    """10 个交易日，sessions[4] 是「次日」的默认对账日。"""
    return _weekdays(date(2026, 3, 2), 10)


def _seed_default(sessions: list[date]) -> None:
    """默认：全市场每日 +1%，sessions[4] 除 SYM_B 外全部 +10%（涨停）。"""
    def pct(sym: str, d: date) -> float:
        if d == sessions[4]:
            return 0.0 if sym == SYM_B else 10.0
        return 1.0

    _seed_grid(sessions, UNIVERSE, pct)


def _outlook(trade_date: date, checklist, **kw):
    from lquant.research.journal import DailyOutlook

    return DailyOutlook(trade_date=trade_date, market_state="range", checklist=tuple(checklist), **kw)


# --------------------------------------------------------------------------- #
# 留痕不可变
# --------------------------------------------------------------------------- #
def test_record_is_idempotent_and_row_is_unique(env) -> None:
    from lquant.core.db import reader
    from lquant.research.journal import record_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    from lquant.research.journal import (
        BooleanCheck,
        ChecklistItem,
        PendingCondition,
        market_item,
    )
    from lquant.research.verify import Condition

    outlook = _outlook(sessions[3], [
        market_item("涨停家数 >= 50（次日）", BooleanCheck("limit_up_count", ">=", 50)),
        # PendingCondition 在录入时冻结锚点，覆盖 B5 复用路径。
        ChecklistItem("SYM_A 收盘 >= 9.0", dimension="stock",
                      check=PendingCondition(SYM_A, (Condition("close", ">=", 9.0),), window_days=1)),
    ])
    first = record_outlook(outlook, now=_dt(sessions[9]))
    second = record_outlook(outlook, now=_dt(sessions[9]))  # 同内容重复记录

    assert first.outlook_id == second.outlook_id
    # PendingCondition 已被冻结成 FrozenCondition，且锚点来自 trade_date 那根日线。
    frozen = second.checklist[1].check
    assert frozen.as_of == sessions[3]
    with reader() as con:
        n = con.execute("SELECT count(*) FROM research_outlook").fetchone()[0]
    assert n == 1  # 同内容重复记录绝不产生重复行


def test_record_same_day_different_content_raises(env) -> None:
    from lquant.research.journal import BooleanCheck, market_item, record_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    base = _outlook(sessions[3], [market_item("涨停家数 >= 50", BooleanCheck("limit_up_count", ">=", 50))])
    record_outlook(base, now=_dt(sessions[9]))

    # 同一天改判：明确报错，不覆盖、不落新版本、不产生重复行。
    with pytest.raises(ValueError, match="不可变"):
        record_outlook(replace(base, notes="事后改口"), now=_dt(sessions[9]))


def test_outlook_is_frozen(env) -> None:
    from lquant.research.journal import BooleanCheck, market_item

    item = market_item("涨停家数 >= 50", BooleanCheck("limit_up_count", ">=", 50))
    outlook = _outlook(date(2026, 3, 5), [item])
    with pytest.raises(FrozenInstanceError):
        outlook.notes = "想原地改"  # type: ignore[misc]


def test_load_outlook_roundtrip_with_regime_and_narrative(env) -> None:
    """快照可脱离实时源复现：叙述字段、证据、regime 全量输出都随行冻结。"""
    from lquant.market.regime import classify_regime
    from lquant.research.journal import (
        BooleanCheck,
        DailyOutlook,
        Direction,
        Scenario,
        StockFocus,
        load_outlook,
        market_item,
        record_outlook,
    )

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    regime = classify_regime({
        "up_ratio": 0.62, "median_change": 0.9, "limit_up_count": 61,
        "seal_ratio": 0.71, "max_consecutive": 6, "strong_down_ratio": 0.04,
        "index_change": 0.6, "above_ma20_ratio": 0.63,
    })
    outlook = DailyOutlook(
        trade_date=sessions[3],
        market_state=regime,  # 直接接 regime.py 的输出
        scenarios=(Scenario("情绪延续", probability=0.6, expectation="涨停家数维持"),),
        directions=(Direction(SYM_A, "up", note="龙头"),),
        focus_next=(StockFocus(SYM_B, note="接力"),),
        checklist=(market_item("涨停家数 >= 50（次日）", BooleanCheck("limit_up_count", ">=", 50)),),
        notes="次日预期：情绪延续",
        evidence=(("breadth", {"up_ratio": 0.62}),),
    )
    assert outlook.market_state == regime.state
    assert dict(outlook.evidence)["regime"]["state"] == regime.state

    stored = record_outlook(outlook, now=_dt(sessions[9]))
    back = load_outlook(sessions[3])
    assert back.outlook_id == stored.outlook_id
    assert back.market_state == regime.state
    assert back.scenarios == outlook.scenarios
    assert back.directions == outlook.directions
    assert back.focus_next == outlook.focus_next
    assert back.notes == "次日预期：情绪延续"
    assert dict(back.evidence)["breadth"] == {"up_ratio": 0.62}
    assert dict(back.evidence)["regime"]["label"] == regime.label


def test_unknown_market_state_is_rejected(env) -> None:
    from lquant.research.journal import DailyOutlook

    with pytest.raises(ValueError, match="未知市场状态"):
        DailyOutlook(trade_date=date(2026, 3, 5), market_state="牛市")


def test_record_refuses_intraday_trade_date(env) -> None:
    from lquant.research.journal import BooleanCheck, market_item, record_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    outlook = _outlook(sessions[3], [market_item("x", BooleanCheck("up_count", ">=", 1))])
    # 当日 10:00：bar 还没定稿，拒绝录入。
    with pytest.raises(ValueError, match="不是已收盘的交易日"):
        record_outlook(outlook, now=_dt(sessions[3], 10))


# --------------------------------------------------------------------------- #
# 逐项对账
# --------------------------------------------------------------------------- #
def _mixed_outlook(sessions: list[date]):
    """2 项命中、1 项未中、1 项窗口未走完。"""
    from lquant.research.journal import (
        BooleanCheck,
        direction_item,
        market_item,
        scenario_item,
        stock_item,
    )

    return _outlook(sessions[3], [
        market_item("涨停家数 >= 50（次日）", BooleanCheck("limit_up_count", ">=", 50)),
        direction_item(SYM_A, "up", threshold_pct=5.0),
        stock_item(SYM_B, change_pct=5.0),
        scenario_item("极端情景：涨停家数 >= 10000（3 日内）",
                      BooleanCheck("up_count", ">=", 10000, window_days=3)),
    ])


def test_item_level_verdicts(env) -> None:
    from lquant.research.journal import record_outlook, validate_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)
    record_outlook(_mixed_outlook(sessions), now=_dt(sessions[9]))

    res = validate_outlook(sessions[3], today=_dt(sessions[4]))
    assert [v.verdict for v in res.items] == ["correct", "correct", "wrong", "unverified"]
    assert (res.correct, res.partial, res.wrong, res.unverified) == (2, 0, 1, 1)

    # unverified 单独统计：不在分数分母里，但留在覆盖率分母里、并逐项列出。
    assert len(res.unverified_items) == 1
    assert res.unverified_items[0].text.startswith("极端情景")
    assert res.unverified_items[0].weight == pytest.approx(20.0)
    assert "未走完" in res.unverified_items[0].reason
    assert "未走完" in res.reason


def test_weighted_score_and_coverage_math(env) -> None:
    """手算对照：维度权重 25/20/25/20，每项独占其维度。

    total_weight = 25(market) + 25(direction) + 20(stock) + 20(scenario) = 90
    judged_weight = 25 + 25 + 20 = 70（scenario 项窗口未走完，不计分）
    score = (25*1 + 25*1 + 20*0) / 70 * 100 = 71.4285… → 71.4
    coverage = 70 / 90 = 0.7777…
    """
    from lquant.research.journal import record_outlook, validate_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)
    record_outlook(_mixed_outlook(sessions), now=_dt(sessions[9]))

    res = validate_outlook(sessions[3], today=_dt(sessions[4]))
    assert res.total_weight == pytest.approx(90.0)
    assert res.judged_weight == pytest.approx(70.0)
    assert res.score == pytest.approx(71.4)
    assert res.coverage == pytest.approx(70.0 / 90.0)

    by_dim = {d.dimension: d for d in res.dimensions}
    assert by_dim["market"].score == pytest.approx(100.0)
    assert by_dim["direction"].score == pytest.approx(100.0)
    assert by_dim["stock"].score == pytest.approx(0.0)
    assert by_dim["scenario"].score is None
    assert by_dim["scenario"].coverage == pytest.approx(0.0)


def test_window_not_complete_gives_provisional_not_final(env) -> None:
    """窗口未走完 → 不给最终 verdict；走完并在未命中处收尾 → final。"""
    from lquant.research.journal import record_outlook, validate_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)
    record_outlook(_mixed_outlook(sessions), now=_dt(sessions[9]))

    early = validate_outlook(sessions[3], today=_dt(sessions[4]))
    assert early.final is False
    assert early.status == "provisional"
    # 已经命中的项不受窗口未走完影响，仍记 correct。
    assert early.correct == 2

    # 窗口推到第 3 个交易日后：第 4 项窗口走完且全程未命中 → wrong，结论 final。
    late = validate_outlook(sessions[3], today=_dt(sessions[6]))
    assert late.final is True
    assert late.status == "final"
    assert [v.verdict for v in late.items] == ["correct", "correct", "wrong", "wrong"]
    assert late.unverified == 0


def test_frozen_condition_item_is_unverified_before_window_ends(env) -> None:
    """复用 B5：FrozenCondition 的窗口未走完 → unverified，而不是 wrong。"""
    from lquant.research.journal import (
        ChecklistItem,
        PendingCondition,
        record_outlook,
        validate_outlook,
    )
    from lquant.research.verify import Condition

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    outlook = _outlook(sessions[3], [
        ChecklistItem("SYM_A 收盘 >= 1e6（5 日内）", dimension="stock",
                      check=PendingCondition(SYM_A, (Condition("close", ">=", 1e6),), window_days=5)),
    ])
    record_outlook(outlook, now=_dt(sessions[9]))

    # 只走完 1/5 个交易日：必须 unverified。
    res = validate_outlook(sessions[3], today=_dt(sessions[4]))
    assert res.items[0].verdict == "unverified"
    assert res.final is False
    assert res.score is None  # 一项都没判定 → 没有分数，而不是 0 分

    # 窗口走完仍不满足 → wrong（与上面的 unverified 明确区分）。
    done = validate_outlook(sessions[3], today=_dt(sessions[8]))
    assert done.items[0].verdict == "wrong"
    assert done.final is True
    assert done.score == pytest.approx(0.0)


def test_lessons_and_realized_risks(env) -> None:
    from lquant.research.journal import (
        BooleanCheck,
        ChecklistItem,
        record_outlook,
        validate_outlook,
    )

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    outlook = _outlook(sessions[3], [
        # 风险假设：涨停家数 >= 50 —— 次日确实成立 → 风险兑现。
        ChecklistItem("风险：涨停家数 >= 50", dimension="market", risk=True,
                      check=BooleanCheck("limit_up_count", ">=", 50)),
        # 正向判断：SYM_B 次日涨 > 5% —— 实际 0% → 判断未兑现。
        ChecklistItem("SYM_B 次日涨 > 5%", dimension="stock",
                      check=BooleanCheck("change_pct", ">", 5.0, symbol=SYM_B)),
    ])
    record_outlook(outlook, now=_dt(sessions[9]))
    res = validate_outlook(sessions[3], today=_dt(sessions[4]))

    assert res.realized_risks and "涨停家数" in res.realized_risks[0]
    assert any("判断未兑现" in x and "SYM_B" in x for x in res.lessons)
    assert all("方法论" in x for x in res.lessons[-1:])


def test_boolean_window_counts_gap_days_not_skipped(env) -> None:
    """某天缺 bar 不跳过：窗口走完但仍有缺口 → unverified（不当成未命中）。"""
    from lquant.research.journal import (
        BooleanCheck,
        ChecklistItem,
        record_outlook,
        validate_outlook,
    )

    sessions = _scenario()
    _seed_calendar(sessions)
    # 只种到 sessions[3]，sessions[4] 整天缺数据（采集缺失）。
    _seed_grid(sessions[:4], [SYM_A], lambda s, d: 1.0)

    outlook = _outlook(sessions[3], [
        ChecklistItem("SYM_A 次日涨 > 5%", dimension="direction",
                      check=BooleanCheck("change_pct", ">", 5.0, symbol=SYM_A)),
    ])
    record_outlook(outlook, now=_dt(sessions[9]))
    res = validate_outlook(sessions[3], today=_dt(sessions[4]))
    assert res.items[0].verdict == "unverified"
    assert "缺数据" in res.items[0].reason
    assert res.wrong == 0


# --------------------------------------------------------------------------- #
# 落库幂等与统计
# --------------------------------------------------------------------------- #
def test_ddl_is_idempotent() -> None:
    import duckdb

    from lquant.data.store import ddl

    con = duckdb.connect()
    assert ddl.ensure_research_journal_tables(con) == 2
    assert ddl.ensure_research_journal_tables(con) == 0  # 幂等
    cols = {r[0] for r in con.execute("DESCRIBE research_outlook").fetchall()}
    assert {"outlook_id", "trade_date", "checklist", "weights"} <= cols
    vcols = {r[0] for r in con.execute("DESCRIBE research_outlook_validation").fetchall()}
    assert {"outlook_id", "checked_through", "final", "coverage", "lessons"} <= vcols


def test_validation_progress_history_keyed_by_checked_through(env) -> None:
    from lquant.core.db import reader
    from lquant.research.journal import record_outlook, validate_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)
    record_outlook(_mixed_outlook(sessions), now=_dt(sessions[9]))

    validate_outlook(sessions[3], today=_dt(sessions[4]))
    validate_outlook(sessions[3], today=_dt(sessions[4]))  # 同日重跑 → 覆盖同一行
    validate_outlook(sessions[3], today=_dt(sessions[6]))  # 窗口推进 → 新增一行

    with reader() as con:
        rows = con.execute(
            "SELECT checked_through, final FROM research_outlook_validation ORDER BY checked_through"
        ).fetchall()
    assert [(r[0], bool(r[1])) for r in rows] == [(sessions[4], False), (sessions[6], True)]


def test_outlook_stats_across_days(env) -> None:
    from lquant.research.journal import (
        BooleanCheck,
        ChecklistItem,
        outlook_stats,
        record_outlook,
        validate_outlook,
    )

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)

    def one(day: date):
        return _outlook(day, [
            ChecklistItem("市场上涨家数 >= 1", dimension="market",
                          check=BooleanCheck("up_count", ">=", 1)),
            ChecklistItem("SYM_A 次日涨 > 100%", dimension="stock",
                          check=BooleanCheck("change_pct", ">", 100.0, symbol=SYM_A)),
        ])

    record_outlook(one(sessions[3]), now=_dt(sessions[9]))
    record_outlook(one(sessions[4]), now=_dt(sessions[9]))
    validate_outlook(sessions[3], today=_dt(sessions[4]))
    validate_outlook(sessions[4], today=_dt(sessions[5]))

    st = outlook_stats(start=sessions[3], end=sessions[4], now=_dt(sessions[9]))
    assert st.outlooks == 2
    assert st.final_outlooks == 2
    assert (st.items, st.judged) == (4, 4)
    assert (st.correct, st.partial, st.wrong, st.unverified) == (2, 0, 2, 0)
    assert st.hit_rate == pytest.approx(0.5)
    assert st.weighted_hit_rate == pytest.approx(0.5)
    # 手算：market 维度 2 项共摊 25 分（各 12.5），stock 维度 2 项共摊 20 分（各 10）。
    # judged_weight = 2*12.5 + 2*10 = 45；raw = 2*12.5 = 25 → 25/45*100 = 55.56 → 55.6。
    assert st.score == pytest.approx(55.6)
    assert st.coverage == pytest.approx(1.0)

    by_dim = {d.dimension: d for d in st.dimensions}
    assert by_dim["market"].score == pytest.approx(100.0)
    assert by_dim["stock"].score == pytest.approx(0.0)
    assert by_dim["market"].items == 2 and by_dim["stock"].items == 2

    # 时间窗口过滤：只取 sessions[4] 那一条。
    only_one = outlook_stats(start=sessions[4], end=sessions[4], now=_dt(sessions[9]))
    assert only_one.outlooks == 1
    assert only_one.hit_rate == pytest.approx(0.5)


def test_outlook_stats_unverified_left_in_coverage_denominator(env) -> None:
    """未判定不进命中率分母，但留在覆盖率分母里（覆盖率因此下降）。"""
    from lquant.research.journal import outlook_stats, record_outlook, validate_outlook

    sessions = _scenario()
    _seed_calendar(sessions)
    _seed_default(sessions)
    record_outlook(_mixed_outlook(sessions), now=_dt(sessions[9]))
    validate_outlook(sessions[3], today=_dt(sessions[4]))

    st = outlook_stats(now=_dt(sessions[9]))
    assert st.items == 4
    assert st.judged == 3
    assert st.unverified == 1
    assert st.hit_rate == pytest.approx(2.0 / 3.0)
    assert st.coverage == pytest.approx(70.0 / 90.0)  # 未判定的 20 分权重留在分母里
