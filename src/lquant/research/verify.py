"""条件级事后核验（Verify）：把一次研究判断冻结成未来可自动对账的记录。

lquant 有因子评价、样本外、ML 版本管理与监控，缺的是「单标的单条件」级的
可追溯核验：一句「若收盘价站上 12.5 且量比 ≤ 1.8，则视为突破确认」在当下只是
文字，事后没人能回答「它到底成立没有」。本模块把这类判断冻结成
:class:`FrozenCondition`（价格锚点由程序在冻结时算出并落库），并在之后的交易日
用**已收盘**的真实日线判定 :class:`VerifyResult`。

设计红线（每一条都对应一种会静默骗人的做法）：

- **只读已收盘的日线**。15:00 之前的当日 bar 只是盘中快照，用它判「站上」等于
  把还没发生的事说成发生了。一律以 ``core.sessions.latest_completed_session``
  为准，而不是裸 ``today``。
- **不猜**。窗口按**交易日会话数**算，就必须有可信的交易日历；拿不到基准时返回
  ``unavailable``，绝不把「下一根有数据的日线」当成下一交易日 —— 那会把停牌、
  长假、采集缺口统统悄悄平移掉。
- **停牌缺失日不跳过**。窗口是「未来 N 个交易日」，停牌日同样占一格；跳过它会让
  窗口整体向未来平移，把本不该覆盖的交易日算进来。缺 bar 的会话照常计入
  ``checked_days`` 并列进 ``missing_days``，且整窗无法裁定 → ``unavailable``
  （除非窗口内已有某一天全部命中，那是与缺失日无关的**正面证据**）。
- **复权/修订会让原阈值失效**。除权除息的前复权重算或数据修订会改写 ``as_of``
  之前的历史收盘价，此时「收盘价 ≥ 12.5」里的 12.5 与现在的价序不在同一口径上。
  冻结时保存重叠历史的收盘价指纹，核验时逐一比对，一旦不一致就 ``unverifiable``，
  而不是拿新口径的价去撞旧阈值。
- **窗口未走完 ≠ 未命中**。用 ``window_complete`` 显式区分「窗口已结束、全程未满足」
  与「窗口还没结束、已检查部分尚未满足」；把两者混成一句「未触发」，事后复盘的
  样本量就是错的。

口径：
- 条件支持 ``close``（收盘价）与 ``volume_ratio``（量比）两个指标，操作符
  ``>= / <= / > / <``。量比 = 当日成交量 / 之前 ``avg_days`` 个**有成交的**
  交易日成交量均值（A 股「量比」惯例用前 N 日均量，不含当日；停牌日没有成交量，
  记 0 会把基准系统性压低、让量比虚高，所以基准只取真实有量的 bar）。
- 一次判断的全部条件必须在**同一个交易日**同时成立才算命中（合取语义）。

只借 easy-stock ``stockanalysis/research_verification.go`` 的**思路**（该仓库为
PolyForm Noncommercial，代码不可复制）：实现、字段与口径均为 lquant 自有。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.sessions import CLOSE_TIME, is_session_complete, latest_completed_session
from lquant.core.types import now_cn_naive, parse_symbol

__all__ = [
    "Condition",
    "Evidence",
    "FrozenCondition",
    "VerifyResult",
    "freeze",
    "verify",
    "verify_and_record",
]

#: 裁决取值。four-way 而不是 bool：``unavailable``（拿不到基准/数据）与
#: ``unverifiable``（价序被改写，阈值失效）是两种完全不同的「不算数」，
#: 混成一个 False 会让「没数据」看起来像「没触发」。
VERDICT_TRIGGERED = "triggered"
VERDICT_NOT_TRIGGERED = "not_triggered"
VERDICT_UNAVAILABLE = "unavailable"
VERDICT_UNVERIFIABLE = "unverifiable"

_METRICS = frozenset({"close", "volume_ratio"})
_OPS = frozenset({">=", "<=", ">", "<"})

#: 默认窗口：未来 5 个交易日。
DEFAULT_WINDOW_DAYS = 5
#: 量比默认基准窗口（前 N 个有成交交易日）。
DEFAULT_VOLUME_AVG_DAYS = 5

#: 复权/修订检测容差。前复权调整通常以百分比计（远超这里），而浮点噪声远小于它；
#: 取相对 2e-4 + 绝对 0.01 的下限，避免把存储噪声误报成修订。
_REVISION_REL_TOL = 2e-4
_REVISION_ABS_TOL = 0.01
#: 冻结时保留多少个历史重叠收盘价用于修订检测（20 个交易日 ≈ 一个月，足够
#: 覆盖一次分红除权，又不至于把指纹撑大）。
_REVISION_LOOKBACK = 20

#: freeze 时回读多长的历史（自然日）。只用于兜住重叠指纹与未来量比基准，
#: 与窗口长度无关。
_FREEZE_LOOKBACK_DAYS = 120
#: verify 时回读多长的历史（自然日）下限；实际按条件里的量比窗口放大。
_VERIFY_MIN_LOOKBACK_DAYS = 90


@dataclass(frozen=True)
class Condition:
    """一个可自动判定的原子条件。

    ``avg_days`` 只对 ``volume_ratio`` 有意义（前 N 个有成交交易日的均量基准），
    对 ``close`` 忽略。
    """

    metric: str
    op: str
    threshold: float
    avg_days: int = DEFAULT_VOLUME_AVG_DAYS

    def __post_init__(self) -> None:
        if self.metric not in _METRICS:
            raise ValueError(
                f"不支持的指标 {self.metric!r}：目前只有 close / volume_ratio 可自动核验；"
                "公告语义、竞价、盘口之类需要人工核对，不该混进自动裁决"
            )
        if self.op not in _OPS:
            raise ValueError(f"不支持的操作符 {self.op!r}：只支持 {sorted(_OPS)}")
        if not math.isfinite(float(self.threshold)):
            raise ValueError(f"阈值必须是有限数，收到 {self.threshold!r}")
        if self.avg_days < 1:
            raise ValueError(f"avg_days 必须 >= 1，收到 {self.avg_days}")

    def to_json(self) -> dict:
        return {
            "metric": self.metric,
            "op": self.op,
            "threshold": float(self.threshold),
            "avg_days": int(self.avg_days),
        }


@dataclass(frozen=True)
class FrozenCondition:
    """冻结后的研究判断。锚点与历史指纹由 :func:`freeze` 程序化算出。

    字段 ``overlap_closes`` 是 ``as_of`` 及之前若干交易日的收盘价指纹；它不是
    冗余 —— 核验时正是靠它发现「历史被复权重算/修订过」，从而拒绝用原阈值裁决。
    """

    symbol: str
    as_of: date
    anchor: float
    conditions: tuple[Condition, ...]
    window_days: int = DEFAULT_WINDOW_DAYS
    note: str = ""
    overlap_closes: tuple[tuple[date, float], ...] = ()

    @property
    def condition_id(self) -> str:
        """内容哈希：同一判断重复冻结/核验落到同一行，幂等。"""
        payload = {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "anchor": round(float(self.anchor), 6),
            "window_days": int(self.window_days),
            "note": self.note,
            "conditions": [c.to_json() for c in self.conditions],
            "overlap_closes": [[d.isoformat(), round(float(v), 6)]
                               for d, v in self.overlap_closes],
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Evidence:
    """单日单条件的证据：哪一天、实际值、阈值、是否命中。"""

    trade_date: date
    metric: str
    op: str
    threshold: float
    observed: float | None
    matched: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyResult:
    """一次核验的完整裁决。

    ``window_complete=False`` 且 ``verdict=not_triggered`` 表示「窗口还没走完，
    已检查部分未命中」；``window_complete=True`` 才是「窗口结束、确实未满足」。
    两者绝不可混为一谈。
    """

    symbol: str
    as_of: date
    anchor: float
    window_days: int
    verdict: str
    window_complete: bool
    checked_days: int
    checked_through: date
    evidence: tuple[Evidence, ...] = ()
    missing_days: tuple[date, ...] = ()
    reason: str = ""


# --------------------------------------------------------------------------- #
# 核验
# --------------------------------------------------------------------------- #
def verify(cond: FrozenCondition, *, today: datetime | date | None = None) -> VerifyResult:
    """在 ``today``（默认现在）核验冻结条件，只读已收盘日线。

    ``today`` 是**时钟注入点**：传 ``datetime`` 表达「此刻」（盘中/盘后由
    ``core.sessions`` 的 15:00 口径判定，盘中当日的 bar 一律不算已完成）；传
    ``date`` 是「该日收盘后」的简写（日线平台的天然语义）。测试与回放都靠它
    冻结时间。
    """
    if not cond.conditions:
        raise ValueError("冻结条件里没有任何条件：核验会退化成恒真，拒绝执行")
    if cond.window_days < 1:
        raise ValueError(f"window_days 必须 >= 1，收到 {cond.window_days}")

    now = _as_now(today)
    # 最近一个**已收盘**交易日：盘中取到的当日 bar 是快照，不能算完成。
    lcs = latest_completed_session(now)

    if cond.as_of > lcs:
        # as_of 本身都还没收盘：连锚点都还只是盘中价，没有任何可核验的事实。
        return _result(
            cond, lcs, VERDICT_UNAVAILABLE, window_complete=False, checked_days=0,
            reason=(f"as_of={cond.as_of} 尚未收盘（最近已完成会话 {lcs}），"
                    "锚点与窗口都没有已定稿的日线可依；不猜"),
        )

    try:
        sessions = _sessions_after(cond.as_of, lcs)
    except Exception as e:  # noqa: BLE001 - 表未建/库不可用/超范围：一律降级为不可用
        return _result(
            cond, lcs, VERDICT_UNAVAILABLE, window_complete=False, checked_days=0,
            reason=(f"交易日历不可用（{type(e).__name__}: {e}）；"
                    "没有交易日基准就不能把某根日线当成「下一交易日」，不猜"),
        )

    if not sessions:
        if cond.as_of < lcs:
            # as_of 早于最新已收盘会话，日历却说这段没有任何交易日：日历为空或
            # 未覆盖。这是**基准缺失**，不是「窗口无交易日」。
            return _result(
                cond, lcs, VERDICT_UNAVAILABLE, window_complete=False, checked_days=0,
                reason=(f"交易日历在 ({cond.as_of}, {lcs}] 内没有任何交易日；"
                        "日历很可能为空或未覆盖该区间，基准缺失，不猜"),
            )
        return _result(
            cond, lcs, VERDICT_UNAVAILABLE, window_complete=False, checked_days=0,
            reason=(f"as_of={cond.as_of} 之后一个已收盘交易日都还没走完"
                    f"（最近已完成会话就是 {lcs}）；窗口尚无任何可核验会话，不猜"),
        )

    window = sessions[: cond.window_days]
    window_complete = len(sessions) >= cond.window_days
    checked_days = len(window)

    bars = _load_symbol_bars(_norm_symbol(cond.symbol), _lookback_start(cond), lcs)
    closes = _series_map(bars, "close")
    volumes = _series_map(bars, "volume")
    ordered = list(bars["trade_date"])

    # 复权/修订检测必须在评估之前：价序一旦被改写，用新价去撞旧阈值得到的
    # 「命中/未命中」都不可信，先否决掉比事后解释便宜。
    revision = _revision_reason(cond, closes)
    if revision:
        return _result(
            cond, lcs, VERDICT_UNVERIFIABLE, window_complete=window_complete,
            checked_days=0, reason=revision,
        )

    idx = {d: i for i, d in enumerate(ordered)}
    evidence: list[Evidence] = []
    missing: list[date] = []
    data_gap = False
    trigger_day: date | None = None

    for d in window:
        if d not in closes:
            # 停牌 / 采集缺失：不跳过，计入窗口并显式记录。
            missing.append(d)
            continue
        day_rows: list[Evidence] = []
        day_ok = True
        for c in cond.conditions:
            observed = _observed(c, d, closes, volumes, ordered, idx)
            if observed is None:
                # 量比基准样本不足：这一天的条件无法复算，不当成「未命中」。
                data_gap = True
                day_ok = False
                day_rows.append(Evidence(
                    d, c.metric, c.op, c.threshold, None, False,
                    f"前 {c.avg_days} 个有成交交易日不足或均量为 0，{c.metric} 无法复算；不猜",
                ))
                continue
            matched = _match(observed, c.op, c.threshold)
            day_rows.append(Evidence(d, c.metric, c.op, c.threshold, observed, matched))
            if not matched:
                day_ok = False
        evidence.extend(day_rows)
        if day_ok and trigger_day is None:
            trigger_day = d

    if trigger_day is not None:
        return _result(
            cond, lcs, VERDICT_TRIGGERED, window_complete=window_complete,
            checked_days=checked_days, evidence=tuple(evidence), missing_days=tuple(missing),
            reason=(f"{trigger_day} 全部条件同时满足；"
                    f"命中不等于成交或盈利，只是「当时写下的判据成立了」"),
        )

    if missing or data_gap:
        parts: list[str] = []
        if missing:
            parts.append(
                f"窗口内 {len(missing)} 个交易日缺 {cond.symbol} 日线（停牌或采集缺失）："
                + "、".join(d.isoformat() for d in missing)
                + "；窗口按交易会话数计，这些日子同样占位、不跳过，整窗无法裁定"
            )
        if data_gap:
            parts.append("部分会话的量比基准样本不足，无法复算")
        return _result(
            cond, lcs, VERDICT_UNAVAILABLE, window_complete=window_complete,
            checked_days=checked_days, evidence=tuple(evidence),
            missing_days=tuple(missing), reason="；".join(parts),
        )

    if not window_complete:
        return _result(
            cond, lcs, VERDICT_NOT_TRIGGERED, window_complete=False,
            checked_days=checked_days, evidence=tuple(evidence),
            reason=(f"窗口 {cond.window_days} 个交易日尚未走完（已走完 {checked_days} 个），"
                    "已检查部分未命中；窗口结束前不能判定为「未满足」"),
        )

    return _result(
        cond, lcs, VERDICT_NOT_TRIGGERED, window_complete=True,
        checked_days=checked_days, evidence=tuple(evidence),
        reason=f"窗口 {cond.window_days} 个交易日已全部走完，未被任一条件命中",
    )


# --------------------------------------------------------------------------- #
# 冻结与落库
# --------------------------------------------------------------------------- #
def freeze(
    symbol: str,
    conditions,
    *,
    as_of: datetime | date | str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    note: str = "",
    now: datetime | date | None = None,
) -> FrozenCondition:
    """冻结一次研究判断：程序化算出锚点与历史指纹，并写入 ``research_condition``。

    ``as_of`` 缺省 = 最近已收盘交易日。**不接受外部传入锚点** —— 锚点只能来自
    ``as_of`` 那根已定稿的日线，否则「冻结」本身就成了事后编造。``as_of`` 当日
    未收盘、不是交易日、或湖里没有该标的的 bar 时 fail loudly（不猜、不补）。
    """
    norm = _norm_symbol(symbol)
    conds = tuple(conditions)
    if not conds:
        raise ValueError("至少要有一个条件：空条件集会让核验变成没有意义的恒真")
    for c in conds:
        if not isinstance(c, Condition):
            raise TypeError(
                f"conditions 元素必须是 Condition，收到 {type(c).__name__}；"
                "字符串条件无法自动裁决，别让它悄悄降级"
            )
    if window_days < 1:
        raise ValueError(f"window_days 必须 >= 1，收到 {window_days}")

    now_dt = _as_now(now)
    lcs = latest_completed_session(now_dt)
    as_of_d = _as_date(as_of)
    if as_of_d is None:
        as_of_d = lcs
    if as_of_d > lcs:
        raise ValueError(
            f"as_of={as_of_d} 晚于最近已完成会话 {lcs}：锚点会用到未定稿的数据，拒绝冻结"
        )
    if not is_session_complete(as_of_d, now_dt):
        raise ValueError(
            f"as_of={as_of_d} 不是已收盘的交易日（或日历不可用/非交易日）："
            "不能用盘中价或不存在会话的价做锚点"
        )

    start = as_of_d - timedelta(days=_FREEZE_LOOKBACK_DAYS)
    bars = _load_symbol_bars(norm, start, as_of_d)
    row = bars.filter(pl.col("trade_date") == as_of_d)
    if row.height == 0:
        raise ValueError(f"as_of={as_of_d} 在日线湖里找不到 {norm} 的 bar：不能编造锚点")
    if row.height > 1:
        raise ValueError(f"as_of={as_of_d} 在日线湖里出现 {row.height} 行 {norm} 日线：主键被破坏")
    anchor = float(row["close"][0])
    if not math.isfinite(anchor) or anchor <= 0:
        raise ValueError(f"as_of={as_of_d} 的收盘价 {anchor!r} 不是有效价格，不能做锚点")

    dates = list(bars["trade_date"])[-_REVISION_LOOKBACK:]
    vals = list(bars["close"])[-_REVISION_LOOKBACK:]
    overlap = tuple((d, float(v)) for d, v in zip(dates, vals, strict=True))

    cond = FrozenCondition(
        symbol=norm,
        as_of=as_of_d,
        anchor=anchor,
        conditions=conds,
        window_days=int(window_days),
        note=note,
        overlap_closes=overlap,
    )
    _with_conn(lambda con: _upsert_condition(con, cond))
    return cond


def verify_and_record(
    cond: FrozenCondition, *, today: datetime | date | None = None
) -> VerifyResult:
    """核验并落库；同条件在同一 ``checked_through`` 上重复调用幂等。

    结果表主键是 ``(condition_id, checked_through)``：同一天重跑覆盖同一行（幂等），
    窗口推进到新的交易日则新增一行 —— 于是「当时怎么判的」有据可查，而不是被
    最后一次结果覆盖掉。
    """
    res = verify(cond, today=today)

    def _write(con) -> None:
        _upsert_condition(con, cond)
        con.execute(
            "INSERT OR REPLACE INTO research_verify_result "
            "(condition_id, checked_through, verdict, window_complete, checked_days, "
            " missing_days, evidence, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), ?, ?)",
            [
                cond.condition_id,
                res.checked_through,
                res.verdict,
                res.window_complete,
                res.checked_days,
                _dumps([d.isoformat() for d in res.missing_days]),
                _dumps([_evidence_json(e) for e in res.evidence]),
                res.reason,
                now_cn_naive(),
            ],
        )

    _with_conn(_write)
    return res


# --------------------------------------------------------------------------- #
# 内部：判定
# --------------------------------------------------------------------------- #
def _norm_symbol(symbol: str) -> str:
    """归一 ``600000`` / ``sh.600000`` 等写法到 ``600000.SH``（裸 6 位是歧义码）。"""
    return str(parse_symbol(str(symbol)))


def _sessions_after(as_of: date, through: date) -> list[date]:
    """``(as_of, through]`` 内的交易日（升序）。读不到日历会抛，由调用方降级。"""
    from lquant.core.calendar import trade_days  # noqa: PLC0415 - 惰性：不拖起 DB 栈

    if through <= as_of:
        return []
    return list(trade_days(as_of + timedelta(days=1), through))


def _lookback_start(cond: FrozenCondition) -> date:
    """verify 回读历史的起点：兜住量比基准与冻结时的重叠指纹。"""
    avgs = [c.avg_days for c in cond.conditions if c.metric == "volume_ratio"]
    span = max(_VERIFY_MIN_LOOKBACK_DAYS, (max(avgs) if avgs else 1) * 4)
    start = cond.as_of - timedelta(days=span)
    if cond.overlap_closes:
        start = min(start, min(d for d, _ in cond.overlap_closes) - timedelta(days=1))
    return start


def _load_symbol_bars(symbol: str, start: date, end: date) -> pl.DataFrame:
    """读单标的日线（生产口径 = parquet 湖 ``read_daily``），按交易日升序。

    schema 缺列时 fail loudly：静默按空帧继续会把「数据坏了」说成「没触发」。
    """
    from lquant.data.store.parquet import read_daily  # noqa: PLC0415 - 惰性加载

    df = read_daily(symbols=[symbol], start=start, end=end).collect()
    if df.height == 0:
        return df
    missing = {"trade_date", "close", "volume"} - set(df.columns)
    if missing:
        raise ValueError(
            f"日线湖 schema 漂移：{symbol} 缺列 {sorted(missing)}；核验必须 fail loudly"
        )
    return df.sort("trade_date")


def _series_map(bars: pl.DataFrame, col: str) -> dict[date, float]:
    """``{trade_date: 值}``。非数值（NULL）直接抛，不静默丢弃该日。"""
    return {d: float(v) for d, v in zip(list(bars["trade_date"]), list(bars[col]), strict=True)}


def _observed(
    c: Condition,
    d: date,
    closes: dict[date, float],
    volumes: dict[date, float],
    ordered: list[date],
    idx: dict[date, int],
) -> float | None:
    """某日某条件的实际值；无法复算返回 ``None``（调用方记为数据缺口，不当作未命中）。"""
    if c.metric == "close":
        return closes[d]
    i = idx[d]
    if i < c.avg_days:
        return None
    # 基准只取**真实有成交的** bar：停牌日没有量，补 0 会把均量压低、量比虚高。
    prior = [volumes[ordered[j]] for j in range(i - c.avg_days, i)]
    base = sum(prior) / c.avg_days
    if base <= 0:
        return None
    return volumes[d] / base


def _match(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    return value < threshold


def _revision_reason(cond: FrozenCondition, closes: dict[date, float]) -> str | None:
    """检测冻结后的历史重叠价是否被改写（除权/修订）；命中即返回原因。"""
    if not cond.overlap_closes:
        return (
            "冻结条件未携带历史重叠收盘价指纹，无法排除复权/数据修订改写了原价序；"
            "不猜（请用 freeze() 重新冻结）"
        )
    for d, old in cond.overlap_closes:
        cur = closes.get(d)
        if cur is None:
            return (
                f"重叠交易日 {d} 的日线在冻结后消失（原收盘 {old:g}）：价序已被改写，"
                "原价格阈值不可核验"
            )
        tol = max(_REVISION_ABS_TOL, abs(old) * _REVISION_REL_TOL)
        if abs(cur - old) > tol:
            return (
                f"重叠交易日 {d} 收盘价由冻结时的 {old:g} 变为 {cur:g}（容差 {tol:g}）："
                "通常由除权除息的复权重算或数据修订造成，新价序与冻结阈值不同口径，"
                "原条件不可核验"
            )
    return None


def _result(
    cond: FrozenCondition,
    checked_through: date,
    verdict: str,
    *,
    window_complete: bool,
    checked_days: int,
    evidence: tuple[Evidence, ...] = (),
    missing_days: tuple[date, ...] = (),
    reason: str = "",
) -> VerifyResult:
    return VerifyResult(
        symbol=cond.symbol,
        as_of=cond.as_of,
        anchor=cond.anchor,
        window_days=cond.window_days,
        verdict=verdict,
        window_complete=window_complete,
        checked_days=checked_days,
        checked_through=checked_through,
        evidence=evidence,
        missing_days=missing_days,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# 内部：时间与持久化
# --------------------------------------------------------------------------- #
def _as_now(v: datetime | date | None) -> datetime | None:
    """把 today/now 注入值规整成时钟。

    ``datetime`` 原样（naive 按上海墙钟，与 core.sessions 同口径）；``date``
    解释为「该日 15:00 收盘后」（日线平台的天然语义）；``None`` = 真实当前时刻。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, CLOSE_TIME.hour, CLOSE_TIME.minute)
    raise TypeError(f"today/now 只接受 datetime / date / None，收到 {type(v).__name__}")


def _as_date(v: datetime | date | str | None) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    raise TypeError(f"as_of 只接受 datetime / date / ISO 字符串 / None，收到 {type(v).__name__}")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _evidence_json(e: Evidence) -> dict:
    return {
        "trade_date": e.trade_date.isoformat(),
        "metric": e.metric,
        "op": e.op,
        "threshold": e.threshold,
        "observed": e.observed,
        "matched": e.matched,
        "detail": e.detail,
    }


def _with_conn(fn) -> None:
    """在写连接里执行 ``fn``，写前惰性补建两张表（老库/隔离库自愈）。"""
    from lquant.core.db import writer  # noqa: PLC0415 - 惰性：不拖起 DB 栈
    from lquant.data.store.ddl import ensure_research_verify_tables  # noqa: PLC0415

    with writer() as con:
        ensure_research_verify_tables(con)
        fn(con)


def _upsert_condition(con, cond: FrozenCondition) -> None:
    con.execute(
        "INSERT OR REPLACE INTO research_condition "
        "(condition_id, symbol, as_of, anchor, conditions, window_days, note, "
        " overlap_closes, created_at) "
        "VALUES (?, ?, ?, ?, CAST(? AS JSON), ?, ?, CAST(? AS JSON), ?)",
        [
            cond.condition_id,
            cond.symbol,
            cond.as_of,
            float(cond.anchor),
            _dumps([c.to_json() for c in cond.conditions]),
            int(cond.window_days),
            cond.note,
            _dumps([[d.isoformat(), v] for d, v in cond.overlap_closes]),
            now_cn_naive(),
        ],
    )
