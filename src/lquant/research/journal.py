"""B4 研究研判闭环：判断 → 留痕 → 次日对账 → 教训沉淀。

lquant 有因子评价、样本外、ML 版本管理与监控，但**没有任何「研究判断 → 未来
事实 → 命中率」的闭环**：每日研判说完就没了，无法回答「我这套判断最近准不准」。
本模块把一次结构化研判（市场状态 / 情景 / 方向 / 明日焦点 / 验证清单）落成
**不可变快照**，并在之后用**已收盘的真实日线**逐项对账，给出

* 逐项 ``correct / partial / wrong / unverified`` 裁决；
* 加权总分 + 覆盖率（未判定项**留在覆盖率分母里**，另单独统计，绝不悄悄消失）；
* 可从错判项归纳的 ``lessons`` 与风险项兑现的 ``realized_risks``；
* 按时间窗口汇总的 ``outlook_stats``（命中率 / 覆盖率 / 各维度得分）。

## 与 B5 的关系：不另造一套判定

清单项（:class:`ChecklistItem`）的 ``check`` 优先复用 ``research/verify.py`` 的
:class:`~lquant.research.verify.FrozenCondition`（价格 / 量条件）。录入时可以直接
给 ``PendingCondition``，由 :func:`record_outlook` 在**研判交易日收盘后**调用
``verify.freeze`` 冻结锚点与历史指纹 —— 锚点只能来自已定稿的日线，事后无法编造。
另一类清单项是显式可判定的布尔判断（如「涨停家数 > 50」），用
:class:`BooleanCheck` 表达，同样只读日线。

## 不变量（每一条都对应一种会静默骗人的做法）

- **快照不可变**。一个 ``trade_date`` 只允许一条研判：同内容重复记录幂等返回，
  不同内容直接报错。改判必须另起一个交易日的新研判，旧快照永不被覆盖 —— 否则
  「当时到底怎么说的」就再也查不到了。
- **未判定 ≠ 判错**。``unverified`` 只有一个来源：窗口没走完，或数据缺失 / 基准
  缺失 / 复权改写。它**不进分数的分母**（不把「还不知道」算成失败），但**留在
  覆盖率的分母里**（覆盖率因此下降），并且单独统计、单独列出。把两者混成一句
  「未命中」，事后复盘的样本量就是错的。
- **只在窗口走完后才给最终结论**。``final=False`` 表示至少有一个清单项还在等
  未来交易日；此时给出的是阶段性分数，结果里显式标注。
- **计分只认可核验的清单项**。``market_state`` / ``scenarios`` / ``directions`` /
  ``focus_next`` 是研判的叙述内容（一并冻结留痕），但不会因为"文字看着对"而被
  计成命中。要把一条方向判断计入分数，就把它落成 ``ChecklistItem``（见
  :func:`direction_item` / :func:`stock_item`）。
- **不猜**。交易日历不可用、日线缺失、价序被修订时一并记为 ``unverified`` 并
  在 reason 里点名，绝不把「下一根有数据的日线」当成下一交易日。

## 权重依据（可配置）

默认维度权重 ``市场状态 25 / 情景 20 / 方向 25 / 个股 20``，量级取自参考实现
（easy-stock ``review/daily_validation.go`` 的 ``scoreValidation``：market 25、
scenario 20、directions 25 分摊、stocks 20 分摊；该仓库为 PolyForm
Noncommercial，**只借量级，不抄代码**）。参考实现另有 10 分给验证清单，本模块
把清单项按维度归档、不再单列一维，故默认权重合计 90（分数按已判定权重归一，
绝对值无关；覆盖率按「已出现的维度」为分母，故合计不必为 100）。
``validate_outlook(..., weights=...)`` 与 ``DailyOutlook(weights=...)`` 都可覆盖。

维度内权重默认均分；``ChecklistItem.weight`` 可给相对份额（同一维度内归一）。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import NamedTuple

import polars as pl

from lquant.core.sessions import CLOSE_TIME, is_session_complete, latest_completed_session
from lquant.core.types import SecType, now_cn_naive, parse_symbol
from lquant.market.regime import REGIME_STATES, RegimeResult, valid_state_set
from lquant.research.verify import DEFAULT_WINDOW_DAYS, Condition, FrozenCondition, verify

__all__ = [
    "BOOLEAN_MARKET_METRICS",
    "BOOLEAN_SYMBOL_METRICS",
    "DEFAULT_DIMENSION_WEIGHTS",
    "DIMENSIONS",
    "DIMENSION_DIRECTION",
    "DIMENSION_MARKET",
    "DIMENSION_SCENARIO",
    "DIMENSION_STOCK",
    "VERDICT_CORRECT",
    "VERDICT_PARTIAL",
    "VERDICT_UNVERIFIED",
    "VERDICT_WRONG",
    "BooleanCheck",
    "ChecklistItem",
    "DailyOutlook",
    "DimensionScore",
    "Direction",
    "ItemVerdict",
    "OutlookStats",
    "OutlookValidation",
    "PendingCondition",
    "Scenario",
    "StockFocus",
    "direction_item",
    "load_outlook",
    "market_item",
    "outlook_stats",
    "record_outlook",
    "scenario_item",
    "stock_item",
    "validate_outlook",
]

#: 四路裁决。``unverified`` 是「还没走完 / 拿不到事实」，与 ``wrong``（事实相反）
#: 是两件事；混成一个会让「数据缺失」看起来像「判断错了」。
VERDICT_CORRECT = "correct"
VERDICT_PARTIAL = "partial"
VERDICT_WRONG = "wrong"
VERDICT_UNVERIFIED = "unverified"

#: 各项对总分的贡献系数（按权重加权后再除以已判定权重）。unverified 不在其中。
_CREDIT: dict[str, float] = {VERDICT_CORRECT: 1.0, VERDICT_PARTIAL: 0.5, VERDICT_WRONG: 0.0}

DIMENSION_MARKET = "market"
DIMENSION_SCENARIO = "scenario"
DIMENSION_DIRECTION = "direction"
DIMENSION_STOCK = "stock"
#: 计分维度。``ChecklistItem.dimension`` 必须是其中之一。
DIMENSIONS: tuple[str, ...] = (
    DIMENSION_MARKET,
    DIMENSION_SCENARIO,
    DIMENSION_DIRECTION,
    DIMENSION_STOCK,
)

#: 默认维度权重（量级依据见模块 docstring）。可配置。
DEFAULT_DIMENSION_WEIGHTS: dict[str, float] = {
    DIMENSION_MARKET: 25.0,
    DIMENSION_SCENARIO: 20.0,
    DIMENSION_DIRECTION: 25.0,
    DIMENSION_STOCK: 20.0,
}

_OPS = frozenset({">=", "<=", ">", "<"})

#: 全市场截面指标（从当天所有个股日线聚合；与单标的无关）。
BOOLEAN_MARKET_METRICS: frozenset[str] = frozenset({
    "up_count",           # 上涨家数（涨幅 > 0）
    "down_count",         # 下跌家数（涨幅 < 0）
    "flat_count",         # 平盘家数
    "up_ratio",           # 上涨家数占比（0~1）
    "median_change_pct",  # 全市场中位涨跌幅（百分数）
    "limit_up_count",     # 涨停家数（板性涨跌幅阈值，口径见 _market_metrics）
})

#: 单标的指标（需要 ``BooleanCheck.symbol``）。
BOOLEAN_SYMBOL_METRICS: frozenset[str] = frozenset({
    "change_pct",      # 当日涨跌幅（百分数，close/pre_close-1）
    "abs_change_pct",  # 涨跌幅绝对值（百分数）：表达「横盘」用它
    "close",           # 收盘价
    "volume",          # 成交量（股）
    "amount",          # 成交额（元）
})

#: 单标的历史回读窗口（自然日）。只用于「pre_close 缺失时用上一根收盘兜底」。
_SYMBOL_LOOKBACK_DAYS = 60

#: 板性涨跌幅限制（非 ST）。口径与 data/quality/validators.py 一致，但那边
#: 是**校验越界**、这里是**计涨停家数**，是两件事，故各自持有一份常量。
_LIMIT_BY_BOARD: dict[str, float] = {"main": 0.10, "gem": 0.20, "star": 0.20, "bse": 0.30}
#: 板性未知时按主板（保守少报，不虚增涨停家数）。
_DEFAULT_LIMIT = 0.10
#: 涨停判定的百分点容差（涨停价四舍五入到分带来的浮动）。
_LIMIT_TOL_PP = 0.5


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _finite(v: object) -> float | None:
    """能转成有限浮点就返回，否则 ``None``（NULL / NaN / 非数值都算缺失）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _norm_symbol(symbol: str) -> str:
    """归一 ``600000`` / ``sh.600000`` 等到 ``600000.SH``（裸 6 位是歧义码）。"""
    return str(parse_symbol(str(symbol)))


def _clock(v: datetime | date | None) -> datetime | None:
    """注入时钟：``datetime`` 原样；``date`` = 该日 15:00 收盘后；``None`` = 现在。

    与 ``research/verify.py`` 的 ``_as_now`` 同口径（盘中当日 bar 一律不算已完成）。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, CLOSE_TIME.hour, CLOSE_TIME.minute)
    raise TypeError(f"today/now 只接受 datetime / date / None，收到 {type(v).__name__}")


def _to_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    raise TypeError(f"trade_date 只接受 date / datetime / ISO 字符串，收到 {type(v).__name__}")


def _dumps(obj: object) -> str:
    """JSON 序列化。不可序列化的证据直接抛 —— 宁可失败也不要静默丢证据。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _check_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """校验维度权重：四维齐全、有限正数。返回 float 副本。"""
    unknown = set(weights) - set(DIMENSIONS)
    if unknown:
        raise ValueError(f"未知维度权重 {sorted(unknown)}；只认 {list(DIMENSIONS)}")
    missing = set(DIMENSIONS) - set(weights)
    if missing:
        raise ValueError(f"缺少维度权重 {sorted(missing)}；四个维度必须齐全")
    out: dict[str, float] = {}
    for k in DIMENSIONS:
        v = _finite(weights[k])
        if v is None or v <= 0:
            raise ValueError(f"维度 {k} 的权重必须是正有限数，收到 {weights[k]!r}")
        out[k] = v
    return out


def _match(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    return value < threshold


def _sessions_after(as_of: date, through: date) -> list[date]:
    """``(as_of, through]`` 内的交易日（升序）。日历读不到会抛，调用方降级。"""
    from lquant.core.calendar import trade_days  # noqa: PLC0415 - 惰性：不拖起 DB 栈

    if through <= as_of:
        return []
    return list(trade_days(as_of + timedelta(days=1), through))


# --------------------------------------------------------------------------- #
# 清单项的条件
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BooleanCheck:
    """一个显式可判定的布尔条件，例如「涨停家数 > 50」。

    ``metric`` 要么取自 :data:`BOOLEAN_MARKET_METRICS`（全市场截面，``symbol``
    必须为 ``None``），要么取自 :data:`BOOLEAN_SYMBOL_METRICS`（单标的，``symbol``
    必填）。``window_days`` 是「未来 N 个交易日内任一天满足即命中」。

    ``limit_up_count`` 的口径与已知偏差（**必须知道**）：按板性涨跌幅（主板
    10% / 双创 20% / 北交所 30%）加 0.5 个百分点取整容差判定，**不识别 ST**
    （ST 为 5%，会被漏计），也不识别上市初期无涨跌幅限制的新股（可能被多计）。
    所以它是「涨停家数的近似值」；要精确的涨停口径应走 ``market.regime`` 的
    涨停池派生（那需要 ``limit_up_pool`` 采集，不是纯日线可得）。
    """

    metric: str
    op: str
    threshold: float
    symbol: str | None = None
    window_days: int = 1

    def __post_init__(self) -> None:
        if self.metric in BOOLEAN_MARKET_METRICS:
            if self.symbol is not None:
                raise ValueError(
                    f"{self.metric} 是全市场截面指标，不接受 symbol={self.symbol!r}；"
                    "混用会让判定悄悄串到单标的上"
                )
        elif self.metric in BOOLEAN_SYMBOL_METRICS:
            if self.symbol is None:
                raise ValueError(f"{self.metric} 是单标的指标，必须给 symbol")
            object.__setattr__(self, "symbol", _norm_symbol(self.symbol))
        else:
            raise ValueError(
                f"不支持的布尔指标 {self.metric!r}；"
                f"截面用 {sorted(BOOLEAN_MARKET_METRICS)}，单标的用 {sorted(BOOLEAN_SYMBOL_METRICS)}"
            )
        if self.op not in _OPS:
            raise ValueError(f"不支持的操作符 {self.op!r}：只支持 {sorted(_OPS)}")
        if _finite(self.threshold) is None:
            raise ValueError(f"阈值必须是有限数，收到 {self.threshold!r}")
        if int(self.window_days) < 1:
            raise ValueError(f"window_days 必须 >= 1，收到 {self.window_days}")

    def to_json(self) -> dict:
        return {
            "metric": self.metric,
            "op": self.op,
            "threshold": float(self.threshold),
            "symbol": self.symbol,
            "window_days": int(self.window_days),
        }

    @staticmethod
    def from_json(d: Mapping[str, object]) -> BooleanCheck:
        return BooleanCheck(
            metric=str(d["metric"]),
            op=str(d["op"]),
            threshold=float(d["threshold"]),  # type: ignore[arg-type]
            symbol=None if d.get("symbol") is None else str(d["symbol"]),
            window_days=int(d.get("window_days", 1)),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PendingCondition:
    """尚未冻结的价格/量条件。由 :func:`record_outlook` 在录入时调 ``verify.freeze``。

    直接接受已冻结的 :class:`FrozenCondition` 也行；用 ``PendingCondition`` 的
    好处是冻结发生在**研判交易日**（锚点只能来自那根已定稿日线），而不是调用方
    随手挑的时刻。
    """

    symbol: str
    conditions: tuple[Condition, ...]
    window_days: int = DEFAULT_WINDOW_DAYS
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _norm_symbol(self.symbol))
        conds = tuple(self.conditions)
        if not conds:
            raise ValueError("至少要有一个条件：空条件集会让核验变成没有意义的恒真")
        for c in conds:
            if not isinstance(c, Condition):
                raise TypeError(
                    f"conditions 元素必须是 Condition，收到 {type(c).__name__}；"
                    "字符串条件无法自动裁决，别让它悄悄降级"
                )
        object.__setattr__(self, "conditions", conds)
        if int(self.window_days) < 1:
            raise ValueError(f"window_days 必须 >= 1，收到 {self.window_days}")

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol,
            "conditions": [c.to_json() for c in self.conditions],
            "window_days": int(self.window_days),
            "note": self.note,
        }


def _frozen_to_json(c: FrozenCondition) -> dict:
    return {
        "symbol": c.symbol,
        "as_of": c.as_of.isoformat(),
        "anchor": float(c.anchor),
        "conditions": [x.to_json() for x in c.conditions],
        "window_days": int(c.window_days),
        "note": c.note,
        "overlap_closes": [[d.isoformat(), float(v)] for d, v in c.overlap_closes],
    }


def _frozen_from_json(d: Mapping[str, object]) -> FrozenCondition:
    conds = tuple(
        Condition(
            metric=str(c["metric"]),
            op=str(c["op"]),
            threshold=float(c["threshold"]),  # type: ignore[arg-type]
            avg_days=int(c.get("avg_days", 5)),  # type: ignore[arg-type]
        )
        for c in d["conditions"]  # type: ignore[union-attr]
    )
    overlap = tuple(
        (date.fromisoformat(str(pair[0])), float(pair[1]))
        for pair in d.get("overlap_closes", [])  # type: ignore[union-attr]
    )
    return FrozenCondition(
        symbol=str(d["symbol"]),
        as_of=date.fromisoformat(str(d["as_of"])),
        anchor=float(d["anchor"]),  # type: ignore[arg-type]
        conditions=conds,
        window_days=int(d.get("window_days", DEFAULT_WINDOW_DAYS)),  # type: ignore[arg-type]
        note=str(d.get("note", "")),
        overlap_closes=overlap,
    )


def _check_to_json(check: object) -> dict:
    if check is None:
        return {"kind": "none"}
    if isinstance(check, FrozenCondition):
        return {"kind": "frozen", **_frozen_to_json(check)}
    if isinstance(check, PendingCondition):
        return {"kind": "pending", **check.to_json()}
    if isinstance(check, BooleanCheck):
        return {"kind": "boolean", **check.to_json()}
    raise TypeError(f"未知清单条件类型 {type(check).__name__}")


def _check_from_json(d: Mapping[str, object]):
    kind = str(d.get("kind", ""))
    if kind == "none":
        return None
    if kind == "frozen":
        return _frozen_from_json(d)
    if kind == "boolean":
        return BooleanCheck.from_json(d)
    if kind == "pending":
        conds = tuple(
            Condition(
                metric=str(c["metric"]),
                op=str(c["op"]),
                threshold=float(c["threshold"]),  # type: ignore[arg-type]
                avg_days=int(c.get("avg_days", 5)),  # type: ignore[arg-type]
            )
            for c in d["conditions"]  # type: ignore[union-attr]
        )
        return PendingCondition(
            symbol=str(d["symbol"]),
            conditions=conds,
            window_days=int(d.get("window_days", DEFAULT_WINDOW_DAYS)),  # type: ignore[arg-type]
            note=str(d.get("note", "")),
        )
    raise ValueError(f"未知清单条件 kind={kind!r}")


@dataclass(frozen=True)
class ChecklistItem:
    """一条**可核验**的研判清单项。

    ``check`` 为 ``None`` 时该项永久不可自动核验（计 ``unverified`` 并显式列出），
    不会被当成命中或未命中 —— 它没有窗口可等，因此不阻塞 ``final``。
    ``risk=True`` 标记「这是一条风险假设」：条件成立 = 风险兑现（进
    ``realized_risks``），不成立 = 风险未兑现（进 ``lessons``）。
    ``weight`` 是**同一维度内**的相对份额（``None`` = 1），不是绝对分。
    """

    text: str
    dimension: str = DIMENSION_MARKET
    check: FrozenCondition | BooleanCheck | PendingCondition | None = None
    risk: bool = False
    weight: float | None = None

    def __post_init__(self) -> None:
        if not str(self.text).strip():
            raise ValueError("清单项必须有文字：空条目无法复盘也无法对账")
        if self.dimension not in DIMENSIONS:
            raise ValueError(
                f"未知维度 {self.dimension!r}；只认 {list(DIMENSIONS)}"
            )
        if not isinstance(self.check, (FrozenCondition, BooleanCheck, PendingCondition, type(None))):
            raise TypeError(
                f"check 必须是 FrozenCondition / BooleanCheck / PendingCondition / None，"
                f"收到 {type(self.check).__name__}"
            )
        if self.weight is not None:
            w = _finite(self.weight)
            if w is None or w <= 0:
                raise ValueError(f"清单项权重份额必须是正有限数，收到 {self.weight!r}")

    def to_json(self) -> dict:
        return {
            "text": self.text,
            "dimension": self.dimension,
            "risk": bool(self.risk),
            "weight": None if self.weight is None else float(self.weight),
            "check": _check_to_json(self.check),
        }

    @staticmethod
    def from_json(d: Mapping[str, object]) -> ChecklistItem:
        return ChecklistItem(
            text=str(d["text"]),
            dimension=str(d.get("dimension", DIMENSION_MARKET)),
            check=_check_from_json(d.get("check") or {"kind": "none"}),  # type: ignore[arg-type]
            risk=bool(d.get("risk", False)),
            weight=None if d.get("weight") is None else float(d["weight"]),  # type: ignore[arg-type]
        )


def market_item(text: str, check: BooleanCheck, *, risk: bool = False,
                weight: float | None = None) -> ChecklistItem:
    """构造市场维度清单项（要求 ``check`` 是截面指标，否则下游会报错）。"""
    return ChecklistItem(text=text, dimension=DIMENSION_MARKET, check=check,
                         risk=risk, weight=weight)


def scenario_item(text: str, check: BooleanCheck, *, risk: bool = False,
                  weight: float | None = None) -> ChecklistItem:
    """构造情景维度清单项。情景本身不可自动裁决，能计分的只有它落成的条件。"""
    return ChecklistItem(text=text, dimension=DIMENSION_SCENARIO, check=check,
                         risk=risk, weight=weight)


def direction_item(symbol: str, view: str, *, threshold_pct: float = 1.0, text: str = "",
                   risk: bool = False, weight: float | None = None,
                   window_days: int = 1) -> ChecklistItem:
    """把一条方向判断落成可核验清单项（次日涨跌幅阈值判定）。

    ``view``：``up``（涨过 +threshold）、``down``（跌过 -threshold）、``flat``
    （|涨跌幅| ≤ threshold，横盘）。方向只有在落成条件后才计分 —— 纯文字的方向
    叙述不会因为"看着对"而被算成命中。
    """
    v = str(view).strip().lower()
    if v == "up":
        check = BooleanCheck("change_pct", ">=", threshold_pct,
                             symbol=symbol, window_days=window_days)
    elif v == "down":
        check = BooleanCheck("change_pct", "<=", -threshold_pct,
                             symbol=symbol, window_days=window_days)
    elif v == "flat":
        check = BooleanCheck("abs_change_pct", "<=", threshold_pct,
                             symbol=symbol, window_days=window_days)
    else:
        raise ValueError(f"view 只支持 up / down / flat，收到 {view!r}")
    label = text or f"{_norm_symbol(symbol)} 次日 {'看涨' if v == 'up' else '看跌' if v == 'down' else '看横盘'} ±{threshold_pct}%"
    return ChecklistItem(text=label, dimension=DIMENSION_DIRECTION, check=check,
                         risk=risk, weight=weight)


def stock_item(symbol: str, *, text: str = "", dimension: str = DIMENSION_STOCK,
               risk: bool = False, weight: float | None = None,
               change_pct: float | None = None, window_days: int = 1) -> ChecklistItem:
    """把一只「明日焦点个股」落成可核验清单项。

    给 ``change_pct`` = 要求次日涨幅至少该值（百分数）；不给则要求收红（> 0）。
    """
    threshold = 0.0 if change_pct is None else float(change_pct)
    check = BooleanCheck("change_pct", ">", threshold, symbol=symbol, window_days=window_days)
    label = text or f"{_norm_symbol(symbol)} 次日涨幅 > {threshold}%"
    return ChecklistItem(text=label, dimension=dimension, check=check,
                         risk=risk, weight=weight)


# --------------------------------------------------------------------------- #
# 结构化研判
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    """一个情景假设（叙述内容，冻结留痕；能否计分取决于是否落成清单项）。"""

    name: str
    probability: float | None = None
    expectation: str = ""

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("情景必须有名字")
        if self.probability is not None:
            p = _finite(self.probability)
            if p is None or not (0.0 <= p <= 1.0):
                raise ValueError(f"情景概率必须落在 [0,1]，收到 {self.probability!r}")

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "probability": None if self.probability is None else float(self.probability),
            "expectation": self.expectation,
        }

    @staticmethod
    def from_json(d: Mapping[str, object]) -> Scenario:
        return Scenario(
            name=str(d["name"]),
            probability=None if d.get("probability") is None else float(d["probability"]),  # type: ignore[arg-type]
            expectation=str(d.get("expectation", "")),
        )


@dataclass(frozen=True)
class Direction:
    """一条方向判断（叙述内容；计分版见 :func:`direction_item`）。"""

    symbol: str
    view: str
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _norm_symbol(self.symbol))
        v = str(self.view).strip().lower()
        if v not in {"up", "down", "flat"}:
            raise ValueError(f"view 只支持 up / down / flat，收到 {self.view!r}")
        object.__setattr__(self, "view", v)

    def to_json(self) -> dict:
        return {"symbol": self.symbol, "view": self.view, "note": self.note}

    @staticmethod
    def from_json(d: Mapping[str, object]) -> Direction:
        return Direction(symbol=str(d["symbol"]), view=str(d["view"]),
                         note=str(d.get("note", "")))


@dataclass(frozen=True)
class StockFocus:
    """明日焦点个股（叙述内容；计分版见 :func:`stock_item`）。"""

    symbol: str
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _norm_symbol(self.symbol))

    def to_json(self) -> dict:
        return {"symbol": self.symbol, "note": self.note}

    @staticmethod
    def from_json(d: Mapping[str, object]) -> StockFocus:
        return StockFocus(symbol=str(d["symbol"]), note=str(d.get("note", "")))


@dataclass(frozen=True)
class DailyOutlook:
    """一次研判的**不可变快照**（解释所需字段全部自带，可脱离实时源复现）。

    ``market_state`` 可直接接 ``market.regime.classify_regime`` 的
    :class:`RegimeResult`（会取 ``state`` 并把全量输出并进 ``evidence``）；
    也可以给状态字符串（必须是 ``regime.valid_state_set()`` 里的值）。给
    ``RegimeResult`` 时若 ``evidence`` 已有同名键，报错而不是静默覆盖。
    """

    trade_date: date
    market_state: str = ""
    scenarios: tuple[Scenario, ...] = ()
    directions: tuple[Direction, ...] = ()
    focus_next: tuple[StockFocus, ...] = ()
    checklist: tuple[ChecklistItem, ...] = ()
    notes: str = ""
    evidence: tuple[tuple[str, object], ...] = ()
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_DIMENSION_WEIGHTS))

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_date", _to_date(self.trade_date))

        state = self.market_state
        evidence = tuple((str(k), v) for k, v in self.evidence)
        if isinstance(state, RegimeResult):
            keys = {k for k, _ in evidence}
            if "regime" in keys:
                raise ValueError("evidence 里已有 'regime' 键：不接受 RegimeResult，避免静默覆盖证据")
            evidence = (("regime", state.as_dict()), *evidence)
            state = state.state
        elif state is None:
            state = ""
        elif not isinstance(state, str):
            raise TypeError(
                f"market_state 只接受 str / RegimeResult / None，收到 {type(state).__name__}"
            )
        if state and state not in valid_state_set():
            raise ValueError(
                f"未知市场状态 {state!r}；regime 口径只认 {sorted(REGIME_STATES)}"
                "（若要用中文标签，请传 RegimeResult 或直接用状态码）"
            )
        object.__setattr__(self, "market_state", state)
        object.__setattr__(self, "evidence", evidence)

        for name, cls in (("scenarios", Scenario), ("directions", Direction),
                          ("focus_next", StockFocus), ("checklist", ChecklistItem)):
            seq = tuple(getattr(self, name))
            for x in seq:
                if not isinstance(x, cls):
                    raise TypeError(f"{name} 元素必须是 {cls.__name__}，收到 {type(x).__name__}")
            object.__setattr__(self, name, seq)

        object.__setattr__(self, "weights", _check_weights(self.weights))

    @property
    def outlook_id(self) -> str:
        """内容哈希：同内容重复记录落到同一行，幂等。"""
        blob = _dumps(self.to_json())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> dict:
        return {
            "trade_date": self.trade_date.isoformat(),
            "market_state": self.market_state,
            "scenarios": [s.to_json() for s in self.scenarios],
            "directions": [d.to_json() for d in self.directions],
            "focus_next": [f.to_json() for f in self.focus_next],
            "checklist": [c.to_json() for c in self.checklist],
            "notes": self.notes,
            "evidence": [[k, v] for k, v in self.evidence],
            "weights": {k: float(v) for k, v in sorted(self.weights.items())},
        }

    @classmethod
    def from_json(cls, d: Mapping[str, object]) -> DailyOutlook:
        return cls(
            trade_date=date.fromisoformat(str(d["trade_date"])),
            market_state=str(d.get("market_state", "")),
            scenarios=tuple(Scenario.from_json(x) for x in d.get("scenarios", [])),  # type: ignore[union-attr]
            directions=tuple(Direction.from_json(x) for x in d.get("directions", [])),  # type: ignore[union-attr]
            focus_next=tuple(StockFocus.from_json(x) for x in d.get("focus_next", [])),  # type: ignore[union-attr]
            checklist=tuple(ChecklistItem.from_json(x) for x in d.get("checklist", [])),  # type: ignore[union-attr]
            notes=str(d.get("notes", "")),
            evidence=tuple((str(k), v) for k, v in d.get("evidence", [])),  # type: ignore[union-attr]
            weights=dict(d.get("weights") or DEFAULT_DIMENSION_WEIGHTS),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# 记录（留痕，不可变）
# --------------------------------------------------------------------------- #
def _resolve_pending(outlook: DailyOutlook, *, now: datetime | date | None) -> DailyOutlook:
    """把 ``PendingCondition`` 冻结成 ``FrozenCondition``；锚点来自 ``trade_date``。

    已冻结的条件必须是**同一个研判交易日**的：拿别的日子的锚点来对账，窗口
    起点就错了，会静默多算或少算交易日。
    """
    items: list[ChecklistItem] = []
    changed = False
    for it in outlook.checklist:
        check = it.check
        if isinstance(check, PendingCondition):
            # 锚点只能来自已定稿日线：由 verify.freeze 在 trade_date 上程序化算出。
            from lquant.research.verify import freeze  # noqa: PLC0415 - 惰性：只在录入时用

            cond = freeze(
                check.symbol,
                check.conditions,
                as_of=outlook.trade_date,
                window_days=check.window_days,
                note=check.note,
                now=now,
            )
            it = replace(it, check=cond)
            changed = True
        elif isinstance(check, FrozenCondition) and check.as_of != outlook.trade_date:
            raise ValueError(
                f"清单项「{it.text}」的冻结条件 as_of={check.as_of} 与研判交易日 "
                f"{outlook.trade_date} 不一致：窗口起点必须就是做判断的那一天"
            )
        items.append(it)
    if not changed:
        return outlook
    return replace(outlook, checklist=tuple(items))


def _with_conn(fn):
    """写连接里执行 ``fn``；写前惰性补建两张表（老库/隔离库自愈）。"""
    from lquant.core.db import writer  # noqa: PLC0415 - 惰性：不拖起 DB 栈
    from lquant.data.store.ddl import ensure_research_journal_tables  # noqa: PLC0415

    with writer() as con:
        ensure_research_journal_tables(con)
        return fn(con)


def record_outlook(
    outlook: DailyOutlook, *, now: datetime | date | None = None
) -> DailyOutlook:
    """留痕一条研判：落库后**不可修改**，返回落库采用的规范化快照。

    幂等与不可变的边界（本功能的核心契约）：

    - 同一 ``trade_date`` + **同内容**重复记录 → 直接返回（``outlook_id`` 相同，
      不新增行）；
    - 同一 ``trade_date`` + **不同内容** → ``ValueError``。改判必须另起一个交易日
      的新研判，旧快照永不被覆盖 —— 否则「当时到底怎么说的」就没了。
      DDL 里的 ``UNIQUE (trade_date)`` 让并发写入也守同一条线。

    ``trade_date`` 必须是**已收盘**的交易日（``core.sessions`` 口径）：盘中录入
    会把还没定稿的当日 bar 当成研判依据。``PendingCondition`` 在这里冻结。
    """
    resolved = _resolve_pending(outlook, now=now)
    ts = _clock(now)
    if not is_session_complete(resolved.trade_date, ts):
        raise ValueError(
            f"trade_date={resolved.trade_date} 不是已收盘的交易日（或日历不可用/非交易日）："
            "研判必须在收盘后录入，否则依据的是盘中快照"
        )
    oid = resolved.outlook_id

    def _write(con) -> bool:
        rows = con.execute(
            "SELECT outlook_id FROM research_outlook WHERE trade_date = ?",
            [resolved.trade_date],
        ).fetchall()
        if rows:
            if oid in {r[0] for r in rows}:
                return False
            raise ValueError(
                f"交易日 {resolved.trade_date} 已有不可变研判快照 "
                f"(outlook_id={sorted(r[0] for r in rows)})：拒绝写入改判版本。"
                "研判快照不可变，改判请另起一个交易日的新研判"
            )
        con.execute(
            "INSERT INTO research_outlook "
            "(outlook_id, trade_date, market_state, evidence, scenarios, directions, "
            " focus_next, checklist, notes, weights, created_at) "
            "VALUES (?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON), "
            " CAST(? AS JSON), CAST(? AS JSON), ?, CAST(? AS JSON), ?)",
            [
                oid,
                resolved.trade_date,
                resolved.market_state,
                _dumps([[k, v] for k, v in resolved.evidence]),
                _dumps([s.to_json() for s in resolved.scenarios]),
                _dumps([d.to_json() for d in resolved.directions]),
                _dumps([f.to_json() for f in resolved.focus_next]),
                _dumps([c.to_json() for c in resolved.checklist]),
                resolved.notes,
                _dumps({k: float(v) for k, v in resolved.weights.items()}),
                now_cn_naive(),
            ],
        )
        return True

    _with_conn(_write)
    return resolved


def load_outlook(trade_date: date | datetime | str) -> DailyOutlook:
    """读回某交易日的研判快照。没有快照时 fail loudly（不返回空对象）。"""
    d = _to_date(trade_date)
    from lquant.core.db import reader  # noqa: PLC0415 - 惰性

    with reader() as con:
        rows = con.execute(
            "SELECT outlook_id, market_state, evidence, scenarios, directions, "
            " focus_next, checklist, notes, weights FROM research_outlook "
            "WHERE trade_date = ?",
            [d],
        ).fetchall()
    if not rows:
        raise ValueError(f"交易日 {d} 没有研判快照：无法对账（先 record_outlook）")
    if len(rows) > 1:
        raise ValueError(
            f"交易日 {d} 出现 {len(rows)} 条研判快照：不可变契约被破坏，先查库"
        )
    oid, market_state, evidence, scenarios, directions, focus_next, checklist, notes, weights = rows[0]

    def _loads(v, default):
        if v is None:
            return default
        return json.loads(v) if isinstance(v, str) else v

    return DailyOutlook(
        trade_date=d,
        market_state=market_state or "",
        scenarios=tuple(Scenario.from_json(x) for x in _loads(scenarios, [])),
        directions=tuple(Direction.from_json(x) for x in _loads(directions, [])),
        focus_next=tuple(StockFocus.from_json(x) for x in _loads(focus_next, [])),
        checklist=tuple(ChecklistItem.from_json(x) for x in _loads(checklist, [])),
        notes=notes or "",
        evidence=tuple((str(k), v) for k, v in _loads(evidence, [])),
        weights=_loads(weights, dict(DEFAULT_DIMENSION_WEIGHTS)),
    )


# --------------------------------------------------------------------------- #
# 次日对账
# --------------------------------------------------------------------------- #
class _Outcome(NamedTuple):
    """单条清单项的判定结果（内部用）。"""

    verdict: str
    window_complete: bool
    evidence: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ItemVerdict:
    """一条清单项的逐项裁决。``credit`` 为 ``None`` 表示未判定（不进分数分母）。"""

    index: int
    text: str
    dimension: str
    verdict: str
    weight: float
    credit: float | None
    window_complete: bool
    risk: bool
    evidence: tuple[str, ...]
    reason: str

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "dimension": self.dimension,
            "verdict": self.verdict,
            "weight": float(self.weight),
            "credit": None if self.credit is None else float(self.credit),
            "window_complete": bool(self.window_complete),
            "risk": bool(self.risk),
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DimensionScore:
    """单个维度的汇总。``score`` 的分母只含已判定权重。"""

    dimension: str
    weight: float
    items: int
    judged: int
    correct: int
    partial: int
    wrong: int
    unverified: int
    judged_weight: float
    score: float | None
    coverage: float

    def to_json(self) -> dict:
        return {
            "dimension": self.dimension,
            "weight": float(self.weight),
            "items": self.items,
            "judged": self.judged,
            "correct": self.correct,
            "partial": self.partial,
            "wrong": self.wrong,
            "unverified": self.unverified,
            "judged_weight": float(self.judged_weight),
            "score": None if self.score is None else float(self.score),
            "coverage": float(self.coverage),
        }


@dataclass(frozen=True)
class OutlookValidation:
    """一次对账的完整结果。

    ``final=False`` 表示至少有一个清单项还在等未来交易日 —— 此时 ``score`` /
    ``coverage`` 是**阶段性**数字，不能当成最终结论。
    ``coverage = 已判定权重 / 全部权重``（未判定留在分母里，因此覆盖率会下降，
    不会"消失"）；``score`` 的分母只含已判定权重（不把"还不知道"算成失败）。
    """

    trade_date: date
    checked_through: date
    final: bool
    score: float | None
    coverage: float
    total_weight: float
    judged_weight: float
    correct: int
    partial: int
    wrong: int
    unverified: int
    items: tuple[ItemVerdict, ...]
    unverified_items: tuple[ItemVerdict, ...]
    dimensions: tuple[DimensionScore, ...]
    lessons: tuple[str, ...]
    realized_risks: tuple[str, ...]
    weights: tuple[tuple[str, float], ...]
    reason: str

    @property
    def status(self) -> str:
        return "final" if self.final else "provisional"

    def to_json(self) -> dict:
        return {
            "trade_date": self.trade_date.isoformat(),
            "checked_through": self.checked_through.isoformat(),
            "final": bool(self.final),
            "status": self.status,
            "score": None if self.score is None else float(self.score),
            "coverage": float(self.coverage),
            "total_weight": float(self.total_weight),
            "judged_weight": float(self.judged_weight),
            "correct": self.correct,
            "partial": self.partial,
            "wrong": self.wrong,
            "unverified": self.unverified,
            "items": [i.to_json() for i in self.items],
            "unverified_items": [i.to_json() for i in self.unverified_items],
            "dimensions": [d.to_json() for d in self.dimensions],
            "lessons": list(self.lessons),
            "realized_risks": list(self.realized_risks),
            "weights": {k: float(v) for k, v in self.weights},
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# 事实读取（只读已收盘日线）
# --------------------------------------------------------------------------- #
def _load_bars(symbol: str, start: date, end: date) -> pl.DataFrame:
    from lquant.data.store.parquet import read_daily  # noqa: PLC0415 - 惰性

    df = read_daily(symbols=[symbol], start=start, end=end).collect()
    if df.height == 0:
        return df
    missing = {"trade_date", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"日线湖 schema 漂移：{symbol} 缺列 {sorted(missing)}；对账必须 fail loudly")
    return df.sort("trade_date")


def _symbol_change_pct(df: pl.DataFrame, day: date) -> tuple[float | None, str]:
    """某日涨跌幅（百分数）；``pre_close`` 缺失/非法时用上一根 bar 的收盘兜底。"""
    row = df.filter(pl.col("trade_date") == day)
    if row.height == 0:
        return None, "无当日 bar（停牌或采集缺失）"
    close = _finite(row["close"][0])
    if close is None or close <= 0:
        return None, f"当日收盘价非法（{row['close'][0]!r}）"
    pre = _finite(row["pre_close"][0]) if "pre_close" in row.columns else None
    if pre is None or pre <= 0:
        prev = df.filter(pl.col("trade_date") < day)
        if prev.height == 0:
            return None, "pre_close 缺失且无更早 bar 可兜底"
        pre = _finite(prev["close"][-1])
    if pre is None or pre <= 0:
        return None, f"前收盘价非法（{pre!r}）"
    return (close / pre - 1.0) * 100.0, ""


def _limit_pct(board: str) -> float:
    return _LIMIT_BY_BOARD.get(board, _DEFAULT_LIMIT)


def _market_metrics(day: date) -> tuple[dict[str, float] | None, str]:
    """当日全市场截面指标。数据/口径不可用时返回 ``(None, 原因)`` —— 不猜。

    只统计 ``SecType.STOCK``（指数 / ETF 混进"上涨家数"会让结论偏向无意义）；
    缺 ``pre_close``/``close`` 的行被**剔除并计数**，若一行都算不出则整体不可用。
    """
    from lquant.data.store.parquet import read_daily  # noqa: PLC0415 - 惰性

    df = read_daily(symbols=None, start=day, end=day).collect()
    if df.height == 0:
        return None, f"{day} 全市场日线为空"
    if "close" not in df.columns:
        return None, f"{day} 全市场日线缺 close 列"
    pre_col = df["pre_close"] if "pre_close" in df.columns else None

    rows: list[tuple[str, float, float]] = []  # (board, change_pct, close)
    skipped = 0
    for i, sym in enumerate(df["symbol"]):
        try:
            s = parse_symbol(str(sym))
        except ValueError:
            skipped += 1
            continue
        if s.sec_type is not SecType.STOCK:
            continue
        c = _finite(df["close"][i])
        p = _finite(pre_col[i]) if pre_col is not None else None
        if c is None or c <= 0 or p is None or p <= 0:
            skipped += 1
            continue
        rows.append((s.board.value, (c / p - 1.0) * 100.0, c))
    if not rows:
        return None, f"{day} 全市场没有可计算涨跌幅的股票（跳过 {skipped} 行）"

    changes = [r[1] for r in rows]
    up = sum(1 for x in changes if x > 0)
    down = sum(1 for x in changes if x < 0)
    limit_up = sum(
        1 for board, chg, _ in rows if chg >= _limit_pct(board) * 100.0 - _LIMIT_TOL_PP
    )
    return {
        "up_count": float(up),
        "down_count": float(down),
        "flat_count": float(len(changes) - up - down),
        "up_ratio": up / len(changes),
        "median_change_pct": float(statistics.median(changes)),
        "limit_up_count": float(limit_up),
    }, ""


def _observed(check: BooleanCheck, day: date) -> tuple[float | None, str]:
    """某日某布尔条件的实际值；拿不到返回 ``(None, 原因)``（记为数据缺口，不判错）。"""
    if check.metric in BOOLEAN_MARKET_METRICS:
        metrics, why = _market_metrics(day)
        if metrics is None:
            return None, why
        return metrics[check.metric], ""
    assert check.symbol is not None  # __post_init__ 已保证
    df = _load_bars(check.symbol, day - timedelta(days=_SYMBOL_LOOKBACK_DAYS), day)
    if df.height == 0:
        return None, f"{check.symbol} 在 {day} 无日线"
    if check.metric == "close":
        row = df.filter(pl.col("trade_date") == day)
        if row.height == 0:
            return None, "无当日 bar（停牌或采集缺失）"
        return _finite(row["close"][0]), ""
    if check.metric in {"volume", "amount"}:
        row = df.filter(pl.col("trade_date") == day)
        if row.height == 0:
            return None, "无当日 bar（停牌或采集缺失）"
        if check.metric not in row.columns:
            return None, f"日线缺 {check.metric} 列"
        return _finite(row[check.metric][0]), ""
    chg, why = _symbol_change_pct(df, day)
    if chg is None:
        return None, why
    return (abs(chg) if check.metric == "abs_change_pct" else chg), ""


def _judge_frozen(cond: FrozenCondition, now: datetime | None) -> _Outcome:
    """复用 B5 的 ``verify``：triggered→correct，窗口走完未命中→wrong，其余→unverified。"""
    res = verify(cond, today=now)
    if res.verdict == "triggered":
        verdict = VERDICT_CORRECT
    elif res.verdict == "not_triggered" and res.window_complete:
        verdict = VERDICT_WRONG
    else:
        verdict = VERDICT_UNVERIFIED
    evidence = [
        f"锚点 {cond.as_of}={cond.anchor:g}（{len(cond.conditions)} 个条件，"
        f"窗口 {cond.window_days} 个交易日）",
        f"核验至 {res.checked_through}：{res.verdict}"
        f"（window_complete={res.window_complete}, checked_days={res.checked_days}）",
    ]
    evidence += [
        f"{e.trade_date} {e.metric}{e.op}{e.threshold:g} "
        f"实际={'缺失' if e.observed is None else format(e.observed, 'g')} "
        f"{'命中' if e.matched else '未命中'}"
        for e in res.evidence
    ]
    return _Outcome(verdict, bool(res.window_complete), tuple(evidence), res.reason)


def _judge_boolean(check: BooleanCheck, trade_date: date, lcs: date) -> _Outcome:
    """按交易日窗口逐日判定布尔条件；窗口内任一天命中即 correct。

    「窗口没走完」与「窗口走完仍未命中」严格区分：前者 unverified，后者 wrong。
    数据缺失的日子**照常占窗口一格**，不跳过 —— 跳过会把窗口整体向未来平移。
    """
    if trade_date > lcs:
        return _Outcome(
            VERDICT_UNVERIFIED, False, (),
            f"研判日 {trade_date} 尚未收盘（最近已完成会话 {lcs}）：没有任何已定稿日线可对账；不猜",
        )
    try:
        sessions = _sessions_after(trade_date, lcs)
    except Exception as e:  # noqa: BLE001 - 表未建/库不可用：降级为不可用，不猜
        return _Outcome(
            VERDICT_UNVERIFIED, False, (),
            f"交易日历不可用（{type(e).__name__}: {e}）；没有交易日基准就不能把某根日线"
            "当成「下一交易日」，不猜",
        )
    if not sessions:
        return _Outcome(
            VERDICT_UNVERIFIED, False, (),
            f"{trade_date} 之后一个已收盘交易日都没有（最近已完成会话就是 {lcs}）："
            "窗口尚未开始，不猜",
        )
    window_complete = len(sessions) >= check.window_days
    window = sessions[: check.window_days]

    evidence: list[str] = []
    gaps: list[date] = []
    hit_day: date | None = None
    for d in window:
        obs, why = _observed(check, d)
        if obs is None:
            gaps.append(d)
            evidence.append(f"{d} 数据缺失：{why}")
            continue
        ok = _match(obs, check.op, check.threshold)
        evidence.append(
            f"{d} {check.metric}={format(obs, 'g')} {check.op} {check.threshold:g} "
            f"→ {'命中' if ok else '未命中'}"
        )
        if ok and hit_day is None:
            hit_day = d

    if hit_day is not None:
        return _Outcome(
            VERDICT_CORRECT, window_complete, tuple(evidence),
            f"{hit_day} 满足 {check.metric} {check.op} {check.threshold:g}；"
            "命中不等于成交或盈利，只是「当时写下的判据成立了」",
        )
    if window_complete and not gaps:
        return _Outcome(
            VERDICT_WRONG, True, tuple(evidence),
            f"窗口 {check.window_days} 个交易日已全部走完，{check.metric} "
            f"{check.op} {check.threshold:g} 全程未成立",
        )
    if not window_complete:
        reason = (
            f"窗口 {check.window_days} 个交易日尚未走完（已走完 {len(window)} 个）："
            "已检查部分未命中，窗口结束前不能判定为「未满足」"
        )
    else:
        reason = (
            f"窗口内 {len(gaps)} 个交易日缺数据（"
            + "、".join(d.isoformat() for d in gaps)
            + "）；这些日子同样占窗口一格、不跳过，整窗无法裁定"
        )
    return _Outcome(VERDICT_UNVERIFIED, window_complete, tuple(evidence), reason)


def _judge_item(item: ChecklistItem, trade_date: date, lcs: date,
                now: datetime | None) -> _Outcome:
    check = item.check
    if isinstance(check, FrozenCondition):
        if check.as_of != trade_date:
            raise ValueError(
                f"清单项「{item.text}」冻结条件 as_of={check.as_of} 与研判日 {trade_date} 不一致"
            )
        return _judge_frozen(check, now)
    if isinstance(check, BooleanCheck):
        return _judge_boolean(check, trade_date, lcs)
    return _Outcome(
        VERDICT_UNVERIFIED, True, ("未附可自动裁决的条件（FrozenCondition / BooleanCheck）",),
        "该清单项是人工判断，没有可自动核验的判据：永久计为未核验，不计入已判定",
    )


def _item_weights(items: Sequence[ChecklistItem], dim_weights: Mapping[str, float]) -> list[float]:
    """维度权重 → 逐项权重：维度内按 ``weight`` 份额归一，未给份额的算 1。"""
    out = [0.0] * len(items)
    for dim in DIMENSIONS:
        idxs = [i for i, it in enumerate(items) if it.dimension == dim]
        if not idxs:
            continue
        shares = [(_finite(items[i].weight) or 1.0) for i in idxs]
        total = sum(shares)
        for i, share in zip(idxs, shares, strict=True):
            out[i] = dim_weights[dim] * share / total
    return out


def _round1(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def validate_outlook(
    trade_date: date | datetime | str,
    *,
    today: datetime | date | None = None,
    weights: Mapping[str, float] | None = None,
    persist: bool = True,
) -> OutlookValidation:
    """用实际日线逐项对账某交易日的研判，并把结果落库（同一 ``checked_through`` 幂等）。

    权重默认取 ``DailyOutlook.weights``（记录时的口径），可用 ``weights`` 覆盖以
    做敏感性分析。``final=False`` 时给出的是阶段性结果并显式标注。
    """
    outlook = load_outlook(trade_date)
    eff_weights = _check_weights(weights if weights is not None else outlook.weights)
    now = _clock(today)
    lcs = latest_completed_session(now)

    items = outlook.checklist
    weights_per_item = _item_weights(items, eff_weights)
    outcomes = [_judge_item(it, outlook.trade_date, lcs, now) for it in items]

    verdicts = tuple(
        ItemVerdict(
            index=i,
            text=it.text,
            dimension=it.dimension,
            verdict=oc.verdict,
            weight=w,
            credit=_CREDIT.get(oc.verdict),
            window_complete=oc.window_complete,
            risk=bool(it.risk),
            evidence=oc.evidence,
            reason=oc.reason,
        )
        for i, (it, oc, w) in enumerate(zip(items, outcomes, weights_per_item, strict=True))
    )

    total_weight = sum(weights_per_item)
    judged_weight = sum(v.weight for v in verdicts if v.credit is not None)
    raw = sum(v.credit * v.weight for v in verdicts if v.credit is not None)  # type: ignore[operator]
    score = _round1(raw / judged_weight * 100.0) if judged_weight > 0 else None
    coverage = (judged_weight / total_weight) if total_weight > 0 else 0.0

    counts = {
        VERDICT_CORRECT: sum(1 for v in verdicts if v.verdict == VERDICT_CORRECT),
        VERDICT_PARTIAL: sum(1 for v in verdicts if v.verdict == VERDICT_PARTIAL),
        VERDICT_WRONG: sum(1 for v in verdicts if v.verdict == VERDICT_WRONG),
        VERDICT_UNVERIFIED: sum(1 for v in verdicts if v.verdict == VERDICT_UNVERIFIED),
    }

    dimensions: list[DimensionScore] = []
    for dim in DIMENSIONS:
        group = [v for v in verdicts if v.dimension == dim]
        if not group:
            continue
        dim_total = sum(v.weight for v in group)
        dim_judged = sum(v.weight for v in group if v.credit is not None)
        dim_raw = sum(v.credit * v.weight for v in group if v.credit is not None)  # type: ignore[operator]
        dimensions.append(DimensionScore(
            dimension=dim,
            weight=eff_weights[dim],
            items=len(group),
            judged=sum(1 for v in group if v.credit is not None),
            correct=sum(1 for v in group if v.verdict == VERDICT_CORRECT),
            partial=sum(1 for v in group if v.verdict == VERDICT_PARTIAL),
            wrong=sum(1 for v in group if v.verdict == VERDICT_WRONG),
            unverified=sum(1 for v in group if v.verdict == VERDICT_UNVERIFIED),
            judged_weight=dim_judged,
            score=_round1(dim_raw / dim_judged * 100.0) if dim_judged > 0 else None,
            coverage=(dim_judged / dim_total) if dim_total > 0 else 0.0,
        ))

    lessons: list[str] = []
    realized_risks: list[str] = []
    for v in verdicts:
        if v.risk and v.verdict == VERDICT_CORRECT:
            realized_risks.append(f"{v.text}｜证据：{v.reason}")
            continue
        if v.verdict == VERDICT_WRONG:
            if v.risk:
                lessons.append(f"风险未兑现（可视为正面）：{v.text}｜{v.reason}")
            else:
                lessons.append(f"判断未兑现：{v.text}｜{v.reason}")
        elif v.verdict == VERDICT_PARTIAL:
            lessons.append(f"只对一半：{v.text}｜{v.reason}")
    lessons.append("方法论：优先复盘触发条件与失效条件本身，不把单日涨跌直接等同于观点质量")

    unverified = tuple(v for v in verdicts if v.verdict == VERDICT_UNVERIFIED)
    final = all(v.window_complete for v in verdicts) if verdicts else False
    if not verdicts:
        reason = "研判没有任何清单项：无法对账，请先补可核验的清单项"
    elif final:
        reason = "全部清单项窗口已走完：结果为最终结论"
    else:
        pending = [v.text for v in verdicts if not v.window_complete]
        reason = f"有 {len(pending)} 个清单项窗口未走完，结果为阶段性：{'；'.join(pending)}"

    result = OutlookValidation(
        trade_date=outlook.trade_date,
        checked_through=lcs,
        final=final,
        score=score,
        coverage=coverage,
        total_weight=total_weight,
        judged_weight=judged_weight,
        correct=counts[VERDICT_CORRECT],
        partial=counts[VERDICT_PARTIAL],
        wrong=counts[VERDICT_WRONG],
        unverified=counts[VERDICT_UNVERIFIED],
        items=verdicts,
        unverified_items=unverified,
        dimensions=tuple(dimensions),
        lessons=tuple(lessons),
        realized_risks=tuple(realized_risks),
        weights=tuple((k, eff_weights[k]) for k in DIMENSIONS),
        reason=reason,
    )
    if persist:
        _persist_validation(outlook.outlook_id, result)
    return result


def _persist_validation(outlook_id: str, res: OutlookValidation) -> None:
    """落库。主键 ``(outlook_id, checked_through)``：同日重跑覆盖同一行（幂等），
    窗口推进到新交易日则新增一行 —— 「当时怎么判的」有据可查。"""
    counts = {
        VERDICT_CORRECT: res.correct,
        VERDICT_PARTIAL: res.partial,
        VERDICT_WRONG: res.wrong,
        VERDICT_UNVERIFIED: res.unverified,
        "items": len(res.items),
        "judged": res.correct + res.partial + res.wrong,
    }

    def _write(con) -> None:
        con.execute(
            "INSERT OR REPLACE INTO research_outlook_validation "
            "(outlook_id, checked_through, final, score, coverage, counts, dimensions, "
            " items, lessons, realized_risks, created_at) "
            "VALUES (?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON), "
            " CAST(? AS JSON), CAST(? AS JSON), ?)",
            [
                outlook_id,
                res.checked_through,
                res.final,
                res.score,
                res.coverage,
                _dumps(counts),
                _dumps([d.to_json() for d in res.dimensions]),
                _dumps([i.to_json() for i in res.items]),
                _dumps(list(res.lessons)),
                _dumps(list(res.realized_risks)),
                now_cn_naive(),
            ],
        )

    _with_conn(_write)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DimensionStat:
    """跨研判汇总的单个维度统计。"""

    dimension: str
    items: int
    judged: int
    correct: int
    partial: int
    wrong: int
    unverified: int
    judged_weight: float
    total_weight: float
    score: float | None
    coverage: float

    def to_json(self) -> dict:
        return {
            "dimension": self.dimension,
            "items": self.items,
            "judged": self.judged,
            "correct": self.correct,
            "partial": self.partial,
            "wrong": self.wrong,
            "unverified": self.unverified,
            "judged_weight": float(self.judged_weight),
            "total_weight": float(self.total_weight),
            "score": None if self.score is None else float(self.score),
            "coverage": float(self.coverage),
        }


@dataclass(frozen=True)
class OutlookStats:
    """时间窗口内的研判质量汇总：回答「我这套判断最近准不准」。

    ``hit_rate`` = 严格命中（correct / 已判定项），``weighted_hit_rate`` =
    (correct + 0.5·partial) / 已判定项；两者的分母**都不含 unverified**（未判定
    单独统计、单独列出），而 ``coverage`` = 已判定权重 / 全部权重，未判定留在
    分母里 —— 于是「准不准」与「判了多少」是两个可以分别看的数字。
    """

    start: date | None
    end: date | None
    outlooks: int
    final_outlooks: int
    items: int
    judged: int
    correct: int
    partial: int
    wrong: int
    unverified: int
    hit_rate: float | None
    weighted_hit_rate: float | None
    coverage: float | None
    score: float | None
    dimensions: tuple[DimensionStat, ...]

    def to_json(self) -> dict:
        return {
            "start": None if self.start is None else self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
            "outlooks": self.outlooks,
            "final_outlooks": self.final_outlooks,
            "items": self.items,
            "judged": self.judged,
            "correct": self.correct,
            "partial": self.partial,
            "wrong": self.wrong,
            "unverified": self.unverified,
            "hit_rate": self.hit_rate,
            "weighted_hit_rate": self.weighted_hit_rate,
            "coverage": self.coverage,
            "score": self.score,
            "dimensions": [d.to_json() for d in self.dimensions],
        }


def _load_latest_validations(start: date | None, end: date | None,
                             lcs: date | None) -> list[tuple[date, bool, list[dict]]]:
    """每个研判取 ``checked_through`` 最大的一条对账行（窗口推进的最新结果）。

    同一 ``checked_through`` 上重复对账是幂等的（覆盖同一行），所以「最大
    checked_through」在同一天内唯一。``lcs`` 非 None 时只取不晚于它的行，
    保证回放/测试可复现。
    """
    from lquant.core.db import reader  # noqa: PLC0415 - 惰性

    sql = [
        "SELECT o.trade_date, v.final, v.items, v.checked_through",
        "FROM research_outlook o",
        "JOIN research_outlook_validation v ON v.outlook_id = o.outlook_id",
        "WHERE v.checked_through = (",
        "  SELECT max(v2.checked_through) FROM research_outlook_validation v2",
        "  WHERE v2.outlook_id = o.outlook_id",
        ")",
    ]
    params: list[object] = []
    if start is not None:
        sql.append("AND o.trade_date >= ?")
        params.append(start)
    if end is not None:
        sql.append("AND o.trade_date <= ?")
        params.append(end)
    if lcs is not None:
        sql.append("AND v.checked_through <= ?")
        params.append(lcs)
    sql.append("ORDER BY o.trade_date")
    with reader() as con:
        rows = con.execute(" ".join(sql), params).fetchall()
    out: list[tuple[date, bool, list[dict]]] = []
    for td, final, items_json, _ct in rows:
        items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
        out.append((td, bool(final), list(items)))
    return out


def outlook_stats(
    *,
    start: date | datetime | str | None = None,
    end: date | datetime | str | None = None,
    now: datetime | date | None = None,
) -> OutlookStats:
    """按时间窗口（按研判交易日 ``trade_date`` 过滤）汇总命中率 / 覆盖率 / 各维度得分。

    每个研判只取**最新一次**对账结果（窗口推进后的那个），避免早期阶段性结果
    与最终结果被重复计数。``now`` 非 None 时只统计核验日不晚于该时刻最近已完成
    交易日的行，便于回放。
    """
    d_start = _to_date(start) if start is not None else None
    d_end = _to_date(end) if end is not None else None
    if d_start is not None and d_end is not None and d_start > d_end:
        raise ValueError(f"start={d_start} 晚于 end={d_end}：窗口非法")
    lcs = latest_completed_session(_clock(now)) if now is not None else None
    rows = _load_latest_validations(d_start, d_end, lcs)

    items = 0
    correct = partial = wrong = unverified = 0
    judged_weight = total_weight = raw = 0.0
    agg: dict[str, dict[str, float]] = {d: {"items": 0.0, "correct": 0.0, "partial": 0.0,
                                            "wrong": 0.0, "unverified": 0.0, "judged": 0.0,
                                            "judged_weight": 0.0, "total_weight": 0.0,
                                            "raw": 0.0}
                                        for d in DIMENSIONS}
    for _td, _final, item_rows in rows:
        for it in item_rows:
            dim = str(it.get("dimension", ""))
            if dim not in DIMENSIONS:
                raise ValueError(f"对账结果里出现未知维度 {dim!r}：不可静默忽略该项")
            w = _finite(it.get("weight"))
            if w is None:
                raise ValueError(f"对账结果缺 weight：{it!r}")
            verdict = str(it.get("verdict", ""))
            credit = _CREDIT.get(verdict)
            items += 1
            total_weight += w
            agg[dim]["items"] += 1
            agg[dim]["total_weight"] += w
            if credit is None:
                unverified += 1
                agg[dim]["unverified"] += 1
                continue
            judged_weight += w
            raw += credit * w
            agg[dim]["judged"] += 1
            agg[dim]["judged_weight"] += w
            agg[dim]["raw"] += credit * w
            if verdict == VERDICT_CORRECT:
                correct += 1
                agg[dim]["correct"] += 1
            elif verdict == VERDICT_PARTIAL:
                partial += 1
                agg[dim]["partial"] += 1
            else:
                wrong += 1
                agg[dim]["wrong"] += 1

    judged = correct + partial + wrong
    dim_stats: list[DimensionStat] = []
    for dim in DIMENSIONS:
        a = agg[dim]
        if not a["items"]:
            continue
        a_judged = int(a["judged"])
        dim_stats.append(DimensionStat(
            dimension=dim,
            items=int(a["items"]),
            judged=a_judged,
            correct=int(a["correct"]),
            partial=int(a["partial"]),
            wrong=int(a["wrong"]),
            unverified=int(a["unverified"]),
            judged_weight=a["judged_weight"],
            total_weight=a["total_weight"],
            score=_round1(a["raw"] / a["judged_weight"] * 100.0) if a["judged_weight"] else None,
            coverage=(a["judged_weight"] / a["total_weight"]) if a["total_weight"] else 0.0,
        ))

    return OutlookStats(
        start=d_start,
        end=d_end,
        outlooks=len(rows),
        final_outlooks=sum(1 for _, final, _ in rows if final),
        items=items,
        judged=judged,
        correct=correct,
        partial=partial,
        wrong=wrong,
        unverified=unverified,
        hit_rate=(correct / judged) if judged else None,
        weighted_hit_rate=((correct + 0.5 * partial) / judged) if judged else None,
        coverage=(judged_weight / total_weight) if total_weight else None,
        score=_round1(raw / judged_weight * 100.0) if judged_weight else None,
        dimensions=tuple(dim_stats),
    )
