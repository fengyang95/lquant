"""``core.sessions``：会话级新鲜度（当日 bar 是否已收盘 / 按会话计龄）。

全部用**冻结时间**（显式传 ``now=``）测试，不依赖真实时钟 —— 否则用例在
周末 / 长假 / CI 不同日期会给出不同结论。

日历不连 DuckDB：``core.sessions`` 惰性 import ``core.calendar.trade_days``，
这里 monkeypatch 掉它，用一个「周一至周五 + 指定节假日」的假日历。真实日历的
查询路径另有 ``test_dialect_fundamentals_extra`` 等覆盖。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from lquant.core import sessions

# 假日历：周一至周五开市，外加一段国庆休市（2026-10-01 ~ 2026-10-07）。
# 2026-09-30 是周三，2026-10-08 是周四 —— 长假前后各一个交易日。
_HOLIDAYS = {date(2026, 10, d) for d in range(1, 8)}


def _is_open(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


@pytest.fixture
def fake_calendar(monkeypatch):
    def trade_days(lo: date, hi: date) -> list[date]:
        out, d = [], lo
        while d <= hi:
            if _is_open(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    monkeypatch.setattr("lquant.core.calendar.trade_days", trade_days)
    return trade_days


def _dt(y, m, d, hh, mm=0) -> datetime:
    """naive 上海墙钟（``_wall_clock`` 按此口径解释 naive 值）。"""
    return datetime(y, m, d, hh, mm)


# ---------------- latest_completed_session ----------------


def test_before_close_returns_previous_session(fake_calendar):
    # 周一 09:30：当日 bar 还在变，只能拿上周五
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 9, 30)) == date(2026, 6, 12)
    # 14:59 仍是收盘前
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 14, 59)) == date(2026, 6, 12)


def test_after_close_returns_today_when_trading_day(fake_calendar):
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 15, 0)) == date(2026, 6, 15)
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 20, 0)) == date(2026, 6, 15)


def test_weekend_after_close_never_returns_the_weekend(fake_calendar):
    # 周六 16:00 已过收盘时刻，但周六不是交易日
    assert sessions.latest_completed_session(_dt(2026, 6, 13, 16, 0)) == date(2026, 6, 12)
    assert sessions.latest_completed_session(_dt(2026, 6, 14, 16, 0)) == date(2026, 6, 12)


def test_holiday_after_close_returns_previous_session(fake_calendar):
    # 国庆假期中的周五 16:00：已经过 15:00，但这一周都不开市
    assert sessions.latest_completed_session(_dt(2026, 10, 2, 16, 0)) == date(2026, 9, 30)
    # 节后第一个交易日 09:00（未收盘）→ 仍是节前最后一个交易日
    assert sessions.latest_completed_session(_dt(2026, 10, 8, 9, 0)) == date(2026, 9, 30)
    assert sessions.latest_completed_session(_dt(2026, 10, 8, 15, 0)) == date(2026, 10, 8)


def test_aware_datetime_is_converted_to_shanghai(fake_calendar):
    # 周一 UTC 09:00 = 上海 17:00（已收盘）→ 当日；UTC 01:00 = 上海 09:00 → 上一交易日
    assert sessions.latest_completed_session(
        datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    ) == date(2026, 6, 15)
    assert sessions.latest_completed_session(
        datetime(2026, 6, 15, 1, 0, tzinfo=UTC)
    ) == date(2026, 6, 12)


# ---------------- is_session_complete ----------------


def test_is_session_complete_matrix(fake_calendar):
    now = _dt(2026, 6, 15, 10, 0)  # 周一盘中
    assert sessions.is_session_complete(date(2026, 6, 15), now) is False   # 当日未收盘
    assert sessions.is_session_complete(date(2026, 6, 12), now) is True    # 上一个交易日
    assert sessions.is_session_complete(date(2026, 6, 13), now) is False   # 周六不是会话
    assert sessions.is_session_complete(date(2026, 6, 16), now) is False   # 未来

    after = _dt(2026, 6, 15, 15, 0)
    assert sessions.is_session_complete(date(2026, 6, 15), after) is True


def test_is_session_complete_on_holiday_is_false(fake_calendar):
    # 假期里的日期即便已经是「过去」，也没有可用的会话 bar
    assert sessions.is_session_complete(date(2026, 10, 1), _dt(2026, 10, 8, 16, 0)) is False
    assert sessions.is_session_complete(date(2026, 9, 30), _dt(2026, 10, 8, 16, 0)) is True


# ---------------- session_lag ----------------


def test_session_lag_weekend_does_not_age(fake_calendar):
    # 周五（2026-06-12）的数据在周一早上：自然日差 3，会话差 1
    now = _dt(2026, 6, 15, 9, 30)
    assert (now.date() - date(2026, 6, 12)).days == 3
    assert sessions.session_lag(date(2026, 6, 12), now) == 1
    # 周末当天回看周五：0（周末不是会话，不老化）
    assert sessions.session_lag(date(2026, 6, 12), _dt(2026, 6, 13, 16, 0)) == 0
    assert sessions.session_lag(date(2026, 6, 12), _dt(2026, 6, 14, 16, 0)) == 0


def test_session_lag_counts_holiday_as_one_session(fake_calendar):
    # 节前最后一个交易日 → 节后第一个交易日 09:00：自然日差 8，会话差 1
    now = _dt(2026, 10, 8, 9, 0)
    assert (now.date() - date(2026, 9, 30)).days == 8
    assert sessions.session_lag(date(2026, 9, 30), now) == 1
    # 隔了两个交易日 → 2
    assert sessions.session_lag(date(2026, 9, 29), now) == 2


def test_session_lag_zero_when_data_covers_current_session(fake_calendar):
    assert sessions.session_lag(date(2026, 6, 15), _dt(2026, 6, 15, 9, 30)) == 0
    assert sessions.session_lag(date(2026, 6, 15), _dt(2026, 6, 15, 20, 0)) == 0
    # 周六的数据（湖里被写成周六，属脏数据）在周六当天不老化
    assert sessions.session_lag(date(2026, 6, 13), _dt(2026, 6, 13, 16, 0)) == 0


def test_session_lag_rejects_future_date(fake_calendar):
    with pytest.raises(ValueError, match="晚于当前交易日"):
        sessions.session_lag(date(2026, 6, 16), _dt(2026, 6, 15, 16, 0))


# ---------------- 日历不可用：保守降级 ----------------


def test_missing_calendar_before_close_never_uses_today(monkeypatch):
    def boom(lo, hi):
        raise RuntimeError("trade_calendar 表不存在")

    monkeypatch.setattr("lquant.core.calendar.trade_days", boom)
    # 周三 10:00 → 回退到周二，绝不返回周三
    assert sessions.latest_completed_session(_dt(2026, 6, 17, 10, 0)) == date(2026, 6, 16)
    assert sessions.is_session_complete(date(2026, 6, 17), _dt(2026, 6, 17, 10, 0)) is False


def test_missing_calendar_after_close_uses_weekday_only(monkeypatch):
    def boom(lo, hi):
        raise RuntimeError("trade_calendar 表不存在")

    monkeypatch.setattr("lquant.core.calendar.trade_days", boom)
    # 周三 16:00 → 周三；周六 16:00 → 周五（只跳周末）
    assert sessions.latest_completed_session(_dt(2026, 6, 17, 16, 0)) == date(2026, 6, 17)
    assert sessions.latest_completed_session(_dt(2026, 6, 13, 16, 0)) == date(2026, 6, 12)
    # 会话计龄同样回退到周一至周五：周五数据在周一早上仍是 1，不是 3
    assert sessions.session_lag(date(2026, 6, 12), _dt(2026, 6, 15, 9, 30)) == 1


def test_empty_calendar_is_treated_as_unavailable(monkeypatch):
    # 表在但一行都没有：``[]`` 不能理解成「永远没有交易日」，否则 lag 恒为 0
    monkeypatch.setattr("lquant.core.calendar.trade_days", lambda lo, hi: [])
    assert sessions.latest_completed_session(_dt(2026, 6, 17, 10, 0)) == date(2026, 6, 16)
    assert sessions.session_lag(date(2026, 6, 12), _dt(2026, 6, 15, 9, 30)) == 1
    # 同一套近似必须贯穿三个函数：不能出现「上一会话是周二、周二却不算交易日」
    assert sessions.is_session_complete(date(2026, 6, 16), _dt(2026, 6, 17, 10, 0)) is True
    assert sessions.is_session_complete(date(2026, 6, 13), _dt(2026, 6, 17, 10, 0)) is False


def test_close_time_boundary_is_closed_at_or_after_1500(fake_calendar):
    assert (sessions.CLOSE_TIME.hour, sessions.CLOSE_TIME.minute) == (15, 0)
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 14, 59)) == date(2026, 6, 12)
    assert sessions.latest_completed_session(_dt(2026, 6, 15, 15, 0)) == date(2026, 6, 15)


def test_open_days_inverted_range_is_empty():
    """hi < lo 直接空列表：不是「日历读不到」（None），两者语义必须分开。"""
    from lquant.core.sessions import _open_days

    assert _open_days(date(2026, 3, 2), date(2026, 3, 1)) == []
