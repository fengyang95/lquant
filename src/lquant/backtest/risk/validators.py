"""事前风控校验器链（P1-3）。

竞品调研（RQAlpha `validators/`）的机制：**独立校验器各自返回一条可读拒单原因**，
而不是在下单逻辑里散落一堆 `if`。lquant 原来没有这一层 —— broker 只管撮合
（滑点/涨跌停/资金），退出规则管平仓，组合约束只在 optimizer 里，**下单前
没有任何「这批单子该不该发」的检查**。

设计取舍（都写在这里，避免以后被"优化"掉）：

1. **只能剔除，不能改单**。校验器不许缩减数量或改价 —— 那等于在下单路径里
   偷偷插入第二层组合构建，出了偏差没人查得出来。要按风险预算缩仓，那是
   optimizer 的职责。
2. **默认只开「结构正确性」规则**。`valid_order` / `duplicate_side` /
   `insufficient_cash` 在正确运行的默认配置下**不可能触发**，开了等于免费；
   而 `sector_exposure` / `turnover_cap` / `drawdown_breaker` 会真的改变
   回测结果（RQAlpha 原话「事前风控宜松不宜紧」），一律默认关、显式开启。
3. **拿不到输入就报错，不静默跳过**。开了 `sector_exposure` 却没有行业数据
   → 抛错说明，而不是"检查了个寂寞"。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from lquant.backtest.events import Order, Side
from lquant.core.registry import Registry

__all__ = [
    "RISK_RULES",
    "GateResult",
    "PreTradeGate",
    "RiskContext",
    "RiskViolation",
    "risk_rule",
]

RISK_RULES: Registry = Registry("risk_rules")


@dataclass(frozen=True)
class RiskViolation:
    """一条可读的拒单/拦截原因。"""

    rule: str
    reason: str
    symbol: str = ""
    scope: str = "order"          # order（剔除该单）| batch（整批拦截，只留卖单）
    order_id: str = ""            # 精确到单：给了它就只剔这一单，不按 symbol 连坐

    def to_row(self, d: date | str) -> dict:
        return {"trade_date": str(d), "rule": self.rule, "symbol": self.symbol,
                "scope": self.scope, "reason": self.reason}


@dataclass
class RiskContext:
    """校验器看到的全部上下文 —— 刻意做窄，避免校验器回头去猜引擎内部状态。"""

    trade_date: date
    nav: float
    cash: float                              # 可用现金（含本轮卖出释放）
    prices: dict[str, float]
    orders: list[Order]
    targets: dict[str, float] = field(default_factory=dict)     # 目标权重
    held_qty: dict[str, float] = field(default_factory=dict)
    sector: dict[str, str] = field(default_factory=dict)        # symbol → 行业代码
    peak_nav: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def drawdown(self) -> float:
        """当前回撤（正数，0.05 = 回撤 5%）。没有峰值信息时返回 0（不误触发熔断）。"""
        if self.peak_nav <= 0 or self.nav >= self.peak_nav:
            return 0.0
        return 1.0 - self.nav / self.peak_nav

    def notional(self, o: Order) -> float:
        return float(o.qty) * float(self.prices.get(o.symbol, 0.0))

    def param(self, rule: str, key: str, default: Any) -> Any:
        """取规则参数：``params[rule][key]`` 优先，其次 ``params[key]``。"""
        scoped = self.params.get(rule)
        if isinstance(scoped, dict) and key in scoped:
            return scoped[key]
        return self.params.get(key, default)


def risk_rule(name: str, label: str = "", *, default_on: bool = False,
              batch: bool = False, per_order: bool = True, doc: str = "") -> Callable:
    """注册一条校验器。

    batch     : 可能返回 scope="batch" 的违规（整批拦截）。
    per_order : 在**看不到整批**的场景（模拟盘逐单提交）里是否仍然成立。
                需要整批才能判断的（换手、行业暴露、单票目标权重）必须显式
                声明 per_order=False —— 否则模拟盘会拿着一条单去算「本轮换手」，
                算出来的数字看着有、其实是错的。
    """
    def deco(fn: Callable[[RiskContext], list[RiskViolation]]) -> Callable:
        # 注意 Registry.register 本身就是「装饰器工厂」：只调用它不会注册，
        # 必须把它返回的装饰器作用在 fn 上（这里直接调用，因为我们已经拿到了 fn）。
        RISK_RULES.register(name, {"name": name, "label": label or name,
                                   "default_on": default_on, "batch": batch,
                                   "per_order": per_order,
                                   "doc": doc or (fn.__doc__ or "").strip()})(fn)
        return fn

    return deco


def _ensure_self_named(name: str,
                       found: list[RiskViolation]) -> list[RiskViolation]:
    """规则必须署自己的名 —— 否则报告里认不出是谁拦的，事后没法归因。"""
    for v in found:
        if v.rule != name:
            raise ValueError(f"规则 {name} 返回了归属 {v.rule} 的违规")
    return found


@dataclass
class GateResult:
    orders: list[Order]
    violations: list[RiskViolation] = field(default_factory=list)

    @property
    def dropped(self) -> list[str]:
        return [v.symbol for v in self.violations if v.scope == "order"]


# --------------------------------------------------------------------------- #
# 默认开：结构正确性（默认配置下不可能触发，触发即说明上游算错了）
# --------------------------------------------------------------------------- #

@risk_rule("valid_order", "订单合法性", default_on=True,
           doc="价格/数量/行情缺失的下单请求直接剔除（上游算错的第一道网）")
def _valid_order(ctx: RiskContext) -> list[RiskViolation]:
    out = []
    for o in ctx.orders:
        px = ctx.prices.get(o.symbol)
        if o.qty is None or float(o.qty) <= 0:
            out.append(RiskViolation("valid_order", f"数量非正（{o.qty}）", o.symbol,
                                     order_id=o.order_id))
        elif px is None or not (float(px) > 0):
            out.append(RiskViolation("valid_order", f"无有效价格（{px}）", o.symbol,
                                     order_id=o.order_id))
    return out


@risk_rule("duplicate_side", "同标的多空并存", default_on=True,
           doc="同一批里对同一标的既买又卖 → 剔除买单（卖出是降低风险的，保留）")
def _duplicate_side(ctx: RiskContext) -> list[RiskViolation]:
    sells = {o.symbol for o in ctx.orders if o.side == Side.SELL}
    return [RiskViolation("duplicate_side", "同批同时买卖同一标的，已剔除买单",
                          o.symbol, order_id=o.order_id)
            for o in ctx.orders if o.side == Side.BUY and o.symbol in sells]


@risk_rule("insufficient_cash", "买入资金超限", default_on=True,
           doc="累计买入名义金额超过可用现金（含容差）→ 剔除溢出的买单")
def _insufficient_cash(ctx: RiskContext) -> list[RiskViolation]:
    tol = float(ctx.param("insufficient_cash", "cash_tolerance_pct", 0.01))
    budget = max(0.0, ctx.cash) * (1.0 + tol)
    used = 0.0
    out = []
    for o in ctx.orders:
        if o.side != Side.BUY:
            continue
        amt = ctx.notional(o)
        if used + amt > budget:
            out.append(RiskViolation(
                "insufficient_cash",
                f"累计买入 {used + amt:.0f} 超可用资金 {budget:.0f}", o.symbol,
                order_id=o.order_id))
            continue
        used += amt
    return out


# --------------------------------------------------------------------------- #
# 默认关：组合级约束（会改变回测结果，必须显式开启）
# --------------------------------------------------------------------------- #

@risk_rule("max_position_weight", "单票权重上限", per_order=False,
           doc="目标权重超过上限 → 剔除该买单；与 EngineConfig.max_position_weight "
               "的区别是这里作用在**下单前**且可单独配置")
def _max_position_weight(ctx: RiskContext) -> list[RiskViolation]:
    cap = float(ctx.param("max_position_weight", "max_weight", 0.1))
    return [RiskViolation("max_position_weight",
                          f"目标权重 {ctx.targets.get(o.symbol, 0.0):.3f} 超上限 {cap:.3f}",
                          o.symbol, order_id=o.order_id)
            for o in ctx.orders if o.side == Side.BUY
            and ctx.targets.get(o.symbol, 0.0) > cap + 1e-9]


@risk_rule("sector_exposure", "行业暴露上限", batch=True, per_order=False,
           doc="建仓后单行业合计权重超上限 → 从超配行业里按买入金额从大到小剔除")
def _sector_exposure(ctx: RiskContext) -> list[RiskViolation]:
    cap = float(ctx.param("sector_exposure", "max_sector_weight", 0.3))
    buys = [o for o in ctx.orders if o.side == Side.BUY]
    if not buys:
        return []
    unknown = [o.symbol for o in buys if not ctx.sector.get(o.symbol)]
    if unknown:
        raise ValueError(
            "sector_exposure 已启用但缺少行业归属: "
            f"{sorted(set(unknown))[:5]}"
            "（先跑 `lq data reference` 灌 industry_classify，或关掉该规则）")
    # 建仓后各行业权重 = 现有持仓市值 + 本轮买入
    weights: dict[str, float] = {}
    for sym, qty in ctx.held_qty.items():
        sec = ctx.sector.get(sym)
        if sec and qty:
            weights[sec] = weights.get(sec, 0.0) + qty * ctx.prices.get(sym, 0.0)
    for o in buys:
        sec = ctx.sector[o.symbol]
        weights[sec] = weights.get(sec, 0.0) + ctx.notional(o)
    if ctx.nav > 0:
        weights = {k: v / ctx.nav for k, v in weights.items()}

    out: list[RiskViolation] = []
    for sec, w in weights.items():
        if w <= cap + 1e-9:
            continue
        excess = w - cap
        # 该行业里买入金额从大到小剔，直到降到上限以内
        legs = sorted((o for o in buys if ctx.sector[o.symbol] == sec),
                      key=lambda o: ctx.notional(o), reverse=True)
        for o in legs:
            if excess <= 1e-9:
                break
            amt = ctx.notional(o) / ctx.nav if ctx.nav > 0 else 0.0
            out.append(RiskViolation(
                "sector_exposure",
                f"行业 {sec} 建仓后权重 {w:.3f} 超上限 {cap:.3f}，剔除 {o.symbol}",
                o.symbol, order_id=o.order_id))
            excess -= amt
    return out


@risk_rule("turnover_cap", "单轮换手上限", batch=True, per_order=False,
           doc="本轮成交名义金额 / NAV 超上限 → 整批不下单（避免一次性换仓）")
def _turnover_cap(ctx: RiskContext) -> list[RiskViolation]:
    cap = float(ctx.param("turnover_cap", "max_turnover", 1.0))
    total = sum(ctx.notional(o) for o in ctx.orders)
    if ctx.nav <= 0 or total / ctx.nav <= cap + 1e-9:
        return []
    return [RiskViolation("turnover_cap",
                          f"本轮换手 {total / ctx.nav:.3f} 超上限 {cap:.3f}，整批不下单",
                          scope="batch")]


@risk_rule("drawdown_breaker", "回撤熔断", batch=True,
           doc="组合回撤达到阈值后**只许减仓**：剔除整批买单（卖单照常）")
def _drawdown_breaker(ctx: RiskContext) -> list[RiskViolation]:
    cap = float(ctx.param("drawdown_breaker", "max_drawdown", 0.2))
    dd = ctx.drawdown
    if dd < cap or not any(o.side == Side.BUY for o in ctx.orders):
        return []
    return [RiskViolation("drawdown_breaker",
                          f"当前回撤 {dd:.3f} 已达熔断阈值 {cap:.3f}，本轮禁止加仓",
                          scope="batch")]


# --------------------------------------------------------------------------- #
# 校验器链
# --------------------------------------------------------------------------- #

class PreTradeGate:
    """按注册表顺序跑校验器；每轮把被剔除的单子摘掉再交给下一条规则。"""

    def __init__(self, names: list[str] | tuple[str, ...] | None = None,
                 params: dict[str, Any] | None = None, *, scope: str = "batch") -> None:
        """scope="batch"：整批下单前（回测）；scope="order"：逐单（模拟盘）。

        scope="order" 只跑声明了 per_order=True 的规则。**显式点名了不适用于
        该场景的规则会报错**，而不是静默少跑一条 —— 那种「以为开了其实没开」
        正是风控最不能有的失败模式。
        """
        self.params = dict(params or {})
        self.scope = scope
        if scope not in ("batch", "order"):
            raise ValueError(f"未知 scope {scope!r}（batch | order）")
        if names is None:
            names = [k for k in RISK_RULES if RISK_RULES.meta(k).get("default_on")]
        unknown = [n for n in names if n not in RISK_RULES]
        if unknown:
            raise ValueError(f"未知风控规则 {unknown}；可用: {sorted(RISK_RULES.keys())}")
        if scope == "order":
            bad = [n for n in names if not RISK_RULES.meta(n).get("per_order", True)]
            if bad:
                raise ValueError(
                    f"这些规则需要整批上下文，逐单场景（模拟盘）无法评估: {bad}")
        self.names = list(names)

    def describe(self) -> list[dict]:
        return [RISK_RULES.meta(n) for n in self.names]

    def apply(self, ctx: RiskContext) -> GateResult:
        orders = list(ctx.orders)
        violations: list[RiskViolation] = []
        # 闸门自己的参数要合进上下文 —— 只存在 self.params 里的话规则读不到，
        # 表现是「开了规则却像没开」（静默失效，最难查的那种）。
        params = {**self.params, **ctx.params}
        for name in self.names:
            fn = RISK_RULES.get(name)
            sub = RiskContext(**{**ctx.__dict__, "orders": orders, "params": params})
            found = _ensure_self_named(name, fn(sub) or [])
            if not found:
                continue
            violations.extend(found)
            if any(v.scope == "batch" for v in found):
                # 整批拦截：只保留卖单（降风险方向）—— turnover_cap 与
                # drawdown_breaker 的共同语义
                orders = [o for o in orders if o.side == Side.SELL]
                continue
            ids = {v.order_id for v in found if v.scope == "order" and v.order_id}
            syms = {v.symbol for v in found
                    if v.scope == "order" and v.symbol and not v.order_id}
            if ids or syms:
                orders = [o for o in orders
                          if o.order_id not in ids and o.symbol not in syms]
        return GateResult(orders=orders, violations=violations)
