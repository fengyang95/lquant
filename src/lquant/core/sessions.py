"""交易日「会话级」数据新鲜度：当日的 bar 到底收盘定稿了没有。

日线平台最容易出的静默错误之一，是把「今天」当成一根已经完整的日线用掉：
15:00 之前跑因子 / 回测 / 快照对账，取到的当日 ``close`` 其实只是盘中某个
时点的价格 —— 算出来的结果看着合理，却没有任何一处会报错。

这里钉死两条语义：

1. **价格新 ≠ 收盘已完成**。``max(trade_date)`` 是今天，只说明「今天有过成交」，
   不说明这根 bar 已经定了。所以把「已收盘」做成显式判定
   （``latest_completed_session`` / ``is_session_complete``），需要完整日线的
   调用方一律以它为准，而不是裸 ``today``。
2. **按交易会话计龄，不按自然日**。周末与长假里行情本就不更新，用自然日算
   「数据落后几天」会让周一早上 / 节后第一个交易日的判据凭空老化 2~9 天，
   把正常运行误报成同步停摆。``session_lag`` 只在真的有交易会话过去时才 +1。

交易日序列一律复用 lquant 已有的 ``core.calendar``（DuckDB ``trade_calendar``
表），本模块**不另建一份日历**；拿不到日历时**不猜** —— 退化为「周一至周五」
的保守近似并 warning，宁可少报一个已完成会话（上层取到空数据，是可见的失败），
也绝不把未收盘的当日说成已完成（那是静默的错误结论）。

设计思路借鉴 easy-stock ``foundation/trading_calendar.go``（该仓库为 PolyForm
Noncommercial，**代码不可复制**，只取「当日未收盘不算完成 / 按会话计龄」这条
不构成表达的思路）：本文件的实现与注释均为 lquant 自己的口径。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CLOSE_TIME",
    "is_session_complete",
    "latest_completed_session",
    "session_lag",
]

#: A 股连续竞价收盘时刻（Asia/Shanghai 墙钟）。日线 bar 在 15:00 定格；
#: 早于它取到的当日 close 只是盘中快照。边界取闭区间：``>= 15:00`` 即视为已收盘。
CLOSE_TIME = time(15, 0)

#: 在日历里往回找交易日的上限（自然日）。A 股最长休市（春节 / 国庆 + 调休）
#: 不超过约 10 天，30 天足够；走满仍找不到交易日就说明日历为空或未覆盖。
_LOOKBACK_DAYS = 30


def _wall_clock(now: datetime | None) -> datetime:
    """统一到 Asia/Shanghai 墙钟。

    aware 值先做时区转换；naive 值按上海墙钟理解（与 ``now_cn_naive`` 同口径）。
    不这么做的话，服务器时区非 Asia/Shanghai 时收盘时刻会整体算错 8 小时 ——
    那正好会落回「15:00 前用了当日 bar」这个要防的错误上。
    """
    from lquant.core.types import TZ, now_cn

    if now is None:
        return now_cn()
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def _open_days(lo: date, hi: date) -> list[date] | None:
    """``[lo, hi]`` 闭区间内的交易日（升序）。日历不可用 → ``None``。

    ``None`` 与 ``[]`` 是两件事，必须区分：``[]`` 是「这段区间真的没有交易日」
    （周末 / 长假），``None`` 是「日历根本读不到，别把它当成没有交易日」——
    后者会静默把 session_lag 压成 0，是最危险的方向。
    """
    if hi < lo:
        return []
    try:
        from lquant.core.calendar import trade_days

        return list(trade_days(lo, hi))
    except Exception as e:  # noqa: BLE001 - 表未建 / 库不可用 / 超范围：一律降级
        log.warning(
            f"交易日历不可用（{type(e).__name__}: {e}）——"
            "退化为周一至周五近似（可能少报已完成会话，绝不把未收盘当日算完成）"
        )
        return None


def _fallback_open_day_on_or_before(d: date) -> date:
    """日历不可用时的保守近似：只把周六周日排除（法定节假日无法识别）。

    方向是**保守**的：可能把节假日误当成交易日（上层取到空数据，是可见的
    失败），但绝不会把 15:00 之前的当日算成已完成（那才是会静默出错的方向）。
    """
    while d.weekday() >= 5:  # 5 = 周六，6 = 周日
        d -= timedelta(days=1)
    return d


def _resolve_open_day_on_or_before(d: date) -> tuple[date, bool]:
    """``d``（含当日）之前最近的交易会话，以及是否走了日历不可用的近似。

    第二个元素必须一路往上传：日历不可用时，后续的会话计数也必须用同一套
    近似，否则会出现「上一个会话按周一至周五近似、计数却按空日历数成 0」的
    自相矛盾 —— 那正好把陈旧数据当成最新的，是本模块要防的静默错误。
    """
    days = _open_days(d - timedelta(days=_LOOKBACK_DAYS), d)
    if days:
        return days[-1], False
    if days is None:
        return _fallback_open_day_on_or_before(d), True
    log.warning(
        f"交易日历近 {_LOOKBACK_DAYS} 天（截至 {d}）没有任何交易日，"
        "日历很可能为空或未覆盖 —— 退化为周一至周五近似"
    )
    return _fallback_open_day_on_or_before(d), True


def _open_day_on_or_before(d: date) -> date:
    """``d``（含当日）之前最近的交易会话；日历不可用 → 周一至周五近似。"""
    return _resolve_open_day_on_or_before(d)[0]


def _count_weekdays(lo: date, hi: date) -> int:
    """日历不可用时的会话计数：``[lo, hi]`` 闭区间内的周一至周五。"""
    n, d = 0, lo
    while d <= hi:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def latest_completed_session(now: datetime | None = None) -> date:
    """最近一个**已收盘**的交易日。

    A 股 15:00 收盘：``now`` 早于 15:00 时当日 bar 尚未定格，**不能**算作已完成，
    此时返回上一个交易日；15:00 及之后若当日是交易日则返回当日，否则继续往前
    找（周末 / 节假日不可能是会话）。

    日历不可用时退化为「周一至周五 + 上述 15:00 口径」并 warning：宁可少报一个
    已完成会话，也绝不把未收盘的当日说成已完成。
    """
    ts = _wall_clock(now)
    today = ts.date()
    if ts.time() < CLOSE_TIME:
        return _open_day_on_or_before(today - timedelta(days=1))
    return _open_day_on_or_before(today)


def is_session_complete(d: date, now: datetime | None = None) -> bool:
    """``d`` 这个交易日的日线 bar 在 ``now`` 时刻是否已经收盘定稿。

    - 未来日期 → ``False``；
    - 当日：``now`` 已过 15:00 **且**当日是交易日才 ``True``；
    - 过去日期：该日**本身**是交易日才 ``True`` —— 周末 / 节假日不存在「已完成
      的会话」，返回 ``False`` 而不是 ``True``，避免上层把一整段空白当成一段
      有效行情（"没有 bar" 和 "bar 已定稿" 是两回事）。

    日历不可用时按「周一至周五」近似判定，保守方向同上。
    """
    ts = _wall_clock(now)
    today = ts.date()
    if d > today:
        return False
    if d == today and ts.time() < CLOSE_TIME:
        return False
    resolved, approximated = _resolve_open_day_on_or_before(d)
    if approximated:
        # 与 latest_completed_session / session_lag 用同一套近似，避免「上一个
        # 会话按周一至周五给出、本函数却说它不是交易日」的自相矛盾
        return d.weekday() < 5
    # 日历可信时：``d`` 就是「d 之前最近的交易日」当且仅当 d 本身开市
    return resolved == d


def session_lag(last_date: date, now: datetime | None = None) -> int:
    """``last_date`` 的数据按**交易会话数**计龄（自然日会让周末 / 长假虚增）。

    口径：数出 ``last_date`` 之后**已经开始**的交易会话个数 —— 当日若为交易日，
    即使尚未收盘也算一格（它的 bar 还不可用，数据相对「可用的最新会话」确实
    落后），因此这是保守上界：只可能高估陈旧，不会低报。于是

    - 周五的数据在周一早上 → ``1``（自然日差是 3，就是这条口径要消除的虚增）；
    - 周五的数据在周六 / 周日 → ``0``（周末不是会话，不老化）；
    - 国庆长假后第一个交易日 → ``1``（自然日差可能是 8）。

    日历不可用时按周一至周五近似计数（回退说明见 ``_open_days``）。
    ``last_date`` 晚于今天属于调用方口径错误（把自然日当成了交易日，或时钟
    错乱）→ fail loudly，不悄悄返回 0。
    """
    ts = _wall_clock(now)
    today = ts.date()
    if last_date > today:
        raise ValueError(
            f"数据日期 {last_date} 晚于当前交易日 {today}："
            "last_date 应当是交易日，这里可能把自然日当成了交易日（或时钟错乱）"
        )
    current, approximated = _resolve_open_day_on_or_before(today)
    if last_date >= current:
        # 数据已覆盖当前（含进行中的）会话：周末传周六、盘中传当日都落到这里
        return 0
    lo = last_date + timedelta(days=1)
    if approximated:
        return _count_weekdays(lo, current)
    days = _open_days(lo, current)
    if days is None:
        return _count_weekdays(lo, current)
    return len(days)
