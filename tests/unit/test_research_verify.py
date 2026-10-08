"""B5 条件级事后核验：裁决语义、证据、复权修订与落库幂等。

每个用例都把日线湖与 DuckDB 指到 tmp（``LQ_DATA_DIR`` / ``LQ_DUCKDB_PATH``），
绝不碰真实数据；交易日历显式写入，因此不依赖本机系统时钟或真实节假日。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

SYMBOL = "600000.SH"


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


def _seed(sessions: list[date], closes, volumes) -> None:
    from lquant.data.store.parquet import write_daily

    df = pl.DataFrame({
        "symbol": [SYMBOL] * len(sessions),
        "trade_date": sessions,
        "open": [float(c) for c in closes],
        "high": [float(c) for c in closes],
        "low": [float(c) for c in closes],
        "close": [float(c) for c in closes],
        "pre_close": [float(closes[0]), *[float(c) for c in closes[:-1]]],
        "volume": [float(v) for v in volumes],
        "amount": [float(v) * 100.0 for v in volumes],
        "adj_factor": [1.0] * len(sessions),
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    write_daily(df)


def _breakout_conditions():
    from lquant.research.verify import Condition

    # 示例判断：若收盘价站上 12.5 且量比 ≤ 1.8，则视为突破确认。
    return [Condition("close", ">=", 12.5), Condition("volume_ratio", "<=", 1.8)]


def _scenario():
    """15 个交易日，as_of = sessions[9]，窗口 = sessions[10..14]。"""
    sessions = _weekdays(date(2026, 3, 2), 15)
    return sessions, sessions[9]


# --------------------------------------------------------------------------- #
# 裁决
# --------------------------------------------------------------------------- #
def test_triggered_reports_day_value_and_threshold(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    closes = [10.0] * len(sessions)
    closes[10] = 12.6
    volumes = [100.0] * len(sessions)
    volumes[10] = 150.0  # 量比 = 150/100 = 1.5 ≤ 1.8
    _seed_calendar(sessions)
    _seed(sessions, closes, volumes)

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    assert cond.anchor == 10.0  # 锚点由程序从 as_of 那根已收盘日线算出

    res = verify(cond, today=_dt(sessions[14]))
    assert res.verdict == "triggered"
    assert res.window_complete is True
    assert res.checked_days == 5

    close_ev = [e for e in res.evidence
                if e.trade_date == sessions[10] and e.metric == "close"]
    assert len(close_ev) == 1
    assert close_ev[0].observed == 12.6
    assert close_ev[0].threshold == 12.5
    assert close_ev[0].op == ">=" and close_ev[0].matched is True

    vr_ev = [e for e in res.evidence
             if e.trade_date == sessions[10] and e.metric == "volume_ratio"]
    assert len(vr_ev) == 1
    assert vr_ev[0].observed == pytest.approx(1.5)
    assert vr_ev[0].threshold == 1.8 and vr_ev[0].matched is True


def test_window_finished_without_hit_is_not_triggered_complete(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    _seed_calendar(sessions)
    _seed(sessions, [10.0] * len(sessions), [100.0] * len(sessions))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    res = verify(cond, today=_dt(sessions[14]))
    assert res.verdict == "not_triggered"
    assert res.window_complete is True
    assert res.checked_days == 5
    # 未命中也有证据：哪一天、实际值多少、阈值多少。
    assert [e.trade_date for e in res.evidence if e.metric == "close"] == sessions[10:15]
    assert all(e.observed == 10.0 and e.matched is False
               for e in res.evidence if e.metric == "close")


def test_window_still_open_is_not_triggered_incomplete(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    _seed_calendar(sessions)
    _seed(sessions, [10.0] * len(sessions), [100.0] * len(sessions))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[11]))
    res = verify(cond, today=_dt(sessions[11]))
    assert res.verdict == "not_triggered"
    # 与上一条完全可区分：窗口没走完，不能记成「确实未满足」。
    assert res.window_complete is False
    assert res.checked_days == 2


def test_no_completed_session_after_as_of_is_unavailable(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    _seed_calendar(sessions)
    _seed(sessions, [10.0] * len(sessions), [100.0] * len(sessions))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[9], 20))
    res = verify(cond, today=_dt(sessions[9], 20))
    assert res.verdict == "unavailable"
    assert res.checked_days == 0
    assert res.window_complete is False
    assert "一个已收盘交易日" in res.reason


def test_intraday_call_does_not_count_today_as_complete(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    closes = [10.0] * len(sessions)
    closes[10] = 12.6  # 当日盘中 bar「看起来」已经满足
    volumes = [100.0] * len(sessions)
    volumes[10] = 150.0
    _seed_calendar(sessions)
    _seed(sessions, closes, volumes)

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[9], 20))
    res = verify(cond, today=_dt(sessions[10], 10))  # sessions[10] 10:00，尚未收盘
    assert res.verdict == "unavailable"
    assert res.checked_through == sessions[9]
    assert sessions[10] not in [e.trade_date for e in res.evidence]


def test_suspended_day_is_counted_not_skipped(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    kept = [d for d in sessions if d != sessions[11]]  # sessions[11] 停牌，无 bar
    _seed_calendar(sessions)
    _seed(kept, [10.0] * len(kept), [100.0] * len(kept))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    res = verify(cond, today=_dt(sessions[14]))
    assert res.verdict == "unavailable"
    # 停牌日占窗口一格、计入 checked_days，并在结果里点名说明。
    assert res.checked_days == 5
    assert res.missing_days == (sessions[11],)
    assert sessions[11].isoformat() in res.reason
    assert "不跳过" in res.reason


def test_adjustment_revision_makes_thresholds_unverifiable(env) -> None:
    from lquant.research.verify import freeze, verify

    sessions, as_of = _scenario()
    closes = [10.0] * len(sessions)
    _seed_calendar(sessions)
    _seed(sessions, closes, [100.0] * len(sessions))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))

    # 冻结之后历史被改写：sessions[5] 收盘 10.0 → 9.5（模拟除权后的前复权重算）。
    revised = list(closes)
    revised[5] = 9.5
    _seed(sessions, revised, [100.0] * len(sessions))

    res = verify(cond, today=_dt(sessions[14]))
    assert res.verdict == "unverifiable"
    assert sessions[5].isoformat() in res.reason
    assert "复权" in res.reason


def test_freeze_refuses_unclosed_anchor(env) -> None:
    from lquant.research.verify import freeze

    sessions, as_of = _scenario()
    _seed_calendar(sessions)
    _seed(sessions, [10.0] * len(sessions), [100.0] * len(sessions))

    # 非交易日做 as_of：不存在「已完成会话」，不能拿周末的价做锚点。
    weekend = sessions[9] + timedelta(days=2)
    assert weekend.weekday() >= 5
    with pytest.raises(ValueError, match="不是已收盘的交易日"):
        freeze(SYMBOL, _breakout_conditions(), as_of=weekend, now=_dt(sessions[14]))

    # 未来的 as_of：那根 bar 还不存在，拒绝冻结。
    with pytest.raises(ValueError, match="晚于最近已完成会话"):
        freeze(SYMBOL, _breakout_conditions(), as_of=sessions[14], now=_dt(sessions[10]))


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def test_ensure_research_verify_tables_is_idempotent() -> None:
    import duckdb

    from lquant.data.store import ddl

    con = duckdb.connect()
    assert ddl.ensure_research_verify_tables(con) == 2
    assert ddl.ensure_research_verify_tables(con) == 0  # 幂等
    cols = {r[0] for r in con.execute("DESCRIBE research_verify_result").fetchall()}
    assert {"condition_id", "checked_through", "verdict", "window_complete"} <= cols


def test_verify_and_record_is_idempotent(env) -> None:
    from lquant.research.verify import freeze, verify_and_record

    sessions, as_of = _scenario()
    closes = [10.0] * len(sessions)
    closes[10] = 12.6
    volumes = [100.0] * len(sessions)
    volumes[10] = 150.0
    _seed_calendar(sessions)
    _seed(sessions, closes, volumes)

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    r1 = verify_and_record(cond, today=_dt(sessions[14]))
    r2 = verify_and_record(cond, today=_dt(sessions[14]))  # 同条件重复核验
    assert r1 == r2

    # 再冻结同一判断也不新增条件行。
    again = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    assert again.condition_id == cond.condition_id

    from lquant.core.db import reader

    with reader() as con:
        n_cond = con.execute("SELECT count(*) FROM research_condition").fetchone()[0]
        n_res = con.execute("SELECT count(*) FROM research_verify_result").fetchone()[0]
        row = con.execute(
            "SELECT verdict, window_complete, checked_days FROM research_verify_result"
        ).fetchone()
    assert n_cond == 1
    assert n_res == 1
    assert tuple(row) == ("triggered", True, 5)


def test_verify_and_record_keeps_progress_history(env) -> None:
    """窗口推进到新的交易日 → 新增一行（而不是覆盖掉「当时怎么判的」）。"""
    from lquant.research.verify import freeze, verify_and_record

    sessions, as_of = _scenario()
    _seed_calendar(sessions)
    _seed(sessions, [10.0] * len(sessions), [100.0] * len(sessions))

    cond = freeze(SYMBOL, _breakout_conditions(), as_of=as_of, now=_dt(sessions[14]))
    early = verify_and_record(cond, today=_dt(sessions[11]))
    late = verify_and_record(cond, today=_dt(sessions[14]))
    assert early.window_complete is False
    assert late.window_complete is True

    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT checked_through, window_complete FROM research_verify_result "
            "ORDER BY checked_through"
        ).fetchall()
    assert [(r[0], bool(r[1])) for r in rows] == [
        (sessions[11], False), (sessions[14], True),
    ]
