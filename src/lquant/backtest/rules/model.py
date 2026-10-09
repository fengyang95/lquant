"""规则模型。

设计要点（多处来自 akquant / rqalpha 源码对账）：
1. T+N 是 per-instrument 属性（sellable_after_days），不是按 sec_type
2. 印花税有生效区间**和买卖方向**：2008-09-19 起才改单边征收（仅卖方），
   此前双边都收 —— 早期回测一律按单边会系统性低估一半成本
3. 最低佣金按订单累计，不是按成交
4. 涨跌停价必须**按最小变动价位取整**：前收 3.63 × 1.1 = 3.993，
   交易所挂牌涨停价是 3.99。用 3.993 判定会把「开盘即涨停」放行，
   等于把买不进去的收益算进回测
5. ST 的涨跌幅不是一律 5%：创业板/科创板 ST 仍 20%，北交所 ST 仍 30%
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from lquant.core.errors import RuleNotFound
from lquant.core.types import Board, SecType, Symbol

__all__ = [
    "Commission", "InstrumentRules", "PriceLimit", "RuleSet", "TaxSchedule",
    "TransferFeeSchedule", "infer_fund_type", "round_tick",
]

_SELL_ONLY = frozenset({"sell"})
_BOTH_SIDES = frozenset({"buy", "sell"})

_SIDE_ALIASES: dict[str, frozenset[str]] = {
    "sell": _SELL_ONLY,
    "sell_only": _SELL_ONLY,
    "buy": frozenset({"buy"}),
    "both": _BOTH_SIDES,
    "all": _BOTH_SIDES,
    "double": _BOTH_SIDES,
}


def _side_key(side) -> str:
    """把 Side 枚举 / 字符串统一成 'buy' / 'sell'。"""
    return str(getattr(side, "value", side)).strip().lower()


def _parse_sides(raw) -> frozenset[str]:
    """解析税档适用方向；缺省 = 仅卖方（现行 A 股口径）。"""
    if raw is None:
        return _SELL_ONLY
    if isinstance(raw, str):
        key = raw.strip().lower()
        return _SIDE_ALIASES.get(key, frozenset({key}))
    return frozenset(_side_key(x) for x in raw)


def round_tick(px: float, tick: float) -> float:
    """把价格四舍五入到最小变动价位（交易所挂牌价口径）。

    股票 tick=0.01、基金 tick=0.001。浮点噪声用 tick 的小数位再 round 一次清掉，
    否则 3.99 会变成 3.9900000000000002，与 bar.open 的相等比较失守。
    """
    if tick is None or tick <= 0 or not math.isfinite(px):
        return px
    nd = max(0, -int(math.floor(math.log10(tick))))
    return round(round(px / tick) * tick, nd)


@dataclass
class TaxSchedule:
    """按日期区间 + 买卖方向取税率。

    历史回测若统一用当前税率，2023 年前成本被低估一半；
    若统一按单边，2008-09-19 前又被低估一半。

    构造兼容 3 元组 (from, to, rate) —— 缺省方向 = 仅卖方（现行 A 股口径），
    这样既有调用方（单测 / selfcheck）不需要改。
    """

    schedule: list[tuple]

    def __post_init__(self) -> None:
        norm: list[tuple[date, date, float, frozenset[str]]] = []
        for item in self.schedule:
            s, e, r = item[0], item[1], item[2]
            sides = item[3] if len(item) > 3 else None
            norm.append((s, e, float(r),
                         _SELL_ONLY if sides is None else _parse_sides(sides)))
        self.schedule = norm

    def rate_at(self, d: date, side=None) -> float:
        for s, e, r, sides in self.schedule:
            if s <= d <= e:
                if side is None:
                    return r
                return r if _side_key(side) in sides else 0.0
        raise RuleNotFound(f"无匹配的税率区间: {d}")


@dataclass
class TransferFeeSchedule:
    """过户费按日期区间取（无方向维度，双向都收）。

    为什么必须区间化：中国结算两次调整过户费 —— 2015-08-01 起沪深统一按成交金额
    0.02‰（双向），2022-04-29 起再下调 50% 至 0.01‰。规则表原来只写一个常数
    0.00001（现行费率），于是 **2022-04-29 之前的回测把过户费少算一半** ——
    静默偏差，不报错。

    为什么表从 2015-08-01 开始：此前沪市按**成交面额** 0.3‰、深市按成交金额
    0.0255‰ 收取。面额口径相当于「股数 × 1 元」，折算成成交额费率会依赖股价
    （面额费率 / 价格），本模型按成交额线性计费，**表达不了**。
    所以早于 2015-08-01 的日期**查不到区间就抛错**，而不是静默套用现行费率 ——
    宁可让长回测显式失败，也不要给出一个错的成本数字。

    构造兼容纯 float 的费率表（``schedule`` 为空时退化为常数，见
    :func:`InstrumentRules.transfer_fee_rate_on`），既有调用方不需要改。
    """

    schedule: list[tuple] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.schedule = [(item[0], item[1], float(item[2])) for item in self.schedule]

    def rate_at(self, d: date) -> float:
        for s, e, r in self.schedule:
            if s <= d <= e:
                return r
        raise RuleNotFound(
            f"无匹配的过户费区间: {d}（区间表 {self.schedule[0][0]} 起；"
            "更早的日期过户费按成交面额计，本模型不支持，需显式配置 rate）")


@dataclass
class Commission:
    rate: float
    min: float
    per_order: bool = True      # 订单分多次成交，最低佣金只收一次


@dataclass
class PriceLimit:
    """涨跌幅比例表。

    mode=by_board：按板块取值（个股）。
    mode=by_track_index：ETF 取决于跟踪指数，指数未知时按代码段/代码兜底 ——
    数据层没有「跟踪指数」字段，所以只能显式登记（见 cn_a_share.yaml）。
    """

    mode: str                    # by_board | by_track_index
    values: dict[str, float] = field(default_factory=dict)
    st_by_board: dict[str, float] = field(default_factory=dict)
    by_code_prefix: dict[str, float] = field(default_factory=dict)
    by_code: dict[str, float] = field(default_factory=dict)

    def for_symbol(self, sym: Symbol, board: Board, is_st: bool,
                   track_index_limit: float | None = None) -> float:
        if self.mode == "by_track_index":
            if track_index_limit is not None:
                return float(track_index_limit)
            hit = self.by_code.get(sym.code)
            if hit is None:
                # 长前缀优先，避免 "58" 抢在 "588" 前面
                for pref in sorted(self.by_code_prefix, key=len, reverse=True):
                    if sym.code.startswith(pref):
                        hit = self.by_code_prefix[pref]
                        break
            if hit is not None:
                return float(hit)
        if is_st:
            # 主板 ST 5%，但创业板/科创板 ST 仍 20%、北交所 ST 仍 30%
            return float(self.st_by_board.get(board.value,
                                              self.values.get("st", 0.05)))
        return float(self.values.get(board.value, self.values.get("main", 0.10)))


def infer_fund_type(name: str, keywords: dict[str, list[str]]) -> str | None:
    """按基金名称关键字推断 fund_type（qdii/commodity/bond/money）。

    数据层没有 fund_type 字段，T+0 判定只能靠名称 —— 关键词表在 rules yaml 里，
    可随新基金补充，不需要改代码。
    """
    if not name:
        return None
    upper = str(name).upper()
    for ftype, kws in (keywords or {}).items():
        for kw in kws or []:
            if str(kw).upper() in upper:
                return str(ftype)
    return None


@dataclass
class InstrumentRules:
    """per-instrument 覆盖，优先于类型默认值。"""

    symbol: Symbol
    sec_type: SecType
    commission: Commission
    tax: TaxSchedule
    transfer_fee_rate: float
    price_limit: PriceLimit
    lot_size: int
    sellable_after_days: int      # T+0 for QDII/黄金/债券/货币 ETF
    #: 过户费区间表（空 = 用 transfer_fee_rate 常数）。见 TransferFeeSchedule。
    transfer_fee_schedule: TransferFeeSchedule | None = None
    is_st: bool = False           # 涨跌停 5% 判定依据（PriceLimit.for_symbol）
    price_tick: float = 0.01      # 最小变动价位：股票 0.01 / 基金 0.001
    # 静态（per-instrument）豁免：IPO 首日 / 复牌首日 / ST 变更日。
    # 真正的逐日判定走 limit_ratio/limit_up/limit_down 的 no_price_limit 参数
    # （IPO 上市初期窗口是「某一天」的事实，静态布尔表达不了），这里只作
    # 调用方未给逐日值时的兜底，保持既有 build_rules(meta=...) 语义不变。
    no_price_limit: bool = False
    track_index_limit: float | None = None   # ETF 跟踪指数涨跌幅

    def tax_rate(self, d: date, side=None) -> float:
        return self.tax.rate_at(d, side)

    def transfer_fee_rate_on(self, d: date) -> float:
        """当日过户费率。

        有区间表时按区间取，**查不到就抛 RuleNotFound**（早于 2015-08-01 的
        日期会走到这里，见 TransferFeeSchedule 的口径说明）；没有区间表则退化为
        ``transfer_fee_rate`` 常数 —— 单测与 selfcheck 仍可直接构造常数规则。
        """
        if self.transfer_fee_schedule is not None:
            return self.transfer_fee_schedule.rate_at(d)
        return self.transfer_fee_rate

    def limit_ratio(self, is_st: bool | None = None,
                    no_price_limit: bool | None = None) -> float | None:
        """当日涨跌幅比例；None = 不设涨跌停约束。

        is_st / no_price_limit 都是**逐日**参数，语义一致：
        传 None 时用本规则的静态值（security 表 / build_rules(meta=...)）；
        传 True/False 时按当日的真实状态覆盖。

        - is_st：ST 会随戴帽/摘帽变化，逐日判定才正确。
        - no_price_limit：IPO 上市初期窗口 / 复牌首日 / ST 变更日都是
          「某一天」的 per-day 事实，静态布尔表达不了。静态值保留是为了
          向后兼容（调用方显式注入的 per-instrument 豁免仍然生效）。
        """
        eff_no_limit = self.no_price_limit if no_price_limit is None \
            else bool(no_price_limit)
        if eff_no_limit:
            return None
        eff_st = self.is_st if is_st is None else bool(is_st)
        return self.price_limit.for_symbol(
            self.symbol, self.symbol.board, is_st=eff_st,
            track_index_limit=self.track_index_limit)

    def limit_up(self, pre_close: float, is_st: bool | None = None,
                 no_price_limit: bool | None = None) -> float | None:
        """涨停价（已按 tick 取整）；None = 无涨跌停约束。"""
        r = self.limit_ratio(is_st, no_price_limit)
        if r is None:
            return None
        return round_tick(pre_close * (1.0 + r), self.price_tick)

    def limit_down(self, pre_close: float, is_st: bool | None = None,
                   no_price_limit: bool | None = None) -> float | None:
        """跌停价（已按 tick 取整）；None = 无涨跌停约束。"""
        r = self.limit_ratio(is_st, no_price_limit)
        if r is None:
            return None
        return round_tick(pre_close * (1.0 - r), self.price_tick)


@dataclass
class RuleSet:
    market: str
    currency: str
    default: dict
    etf: dict
    exceptions: dict
    fund_type_keywords: dict = field(default_factory=dict)

    def for_symbol(
        self,
        symbol: str,
        sec_type: SecType,
        board: Board,
        is_st: bool = False,
        fund_type: str | None = None,
        sellable_after_days: int | None = None,
        track_index_limit: float | None = None,
        no_price_limit: bool = False,
        name: str | None = None,
    ) -> InstrumentRules:
        base = self.etf if sec_type in (SecType.ETF, SecType.LOF) else self.default
        comm = base.get("commission", {})
        tax_sched = [
            (date.fromisoformat(str(x["from"])), date.fromisoformat(str(x["to"])),
             float(x["rate"]), _parse_sides(x.get("side", x.get("sides"))))
            for x in base.get("tax", {}).get("schedule", [])
        ] or [(date(2000, 1, 1), date(9999, 12, 31),
               float(base.get("tax", {}).get("rate", 0.0)),
               _parse_sides(base.get("tax", {}).get("side")))]

        t_plus = base.get("t_plus", 1)
        if isinstance(t_plus, dict):
            # 名称推断只在调用方没显式给 fund_type 时兜底
            ft = fund_type or infer_fund_type(name or "", self.fund_type_keywords)
            t_plus = t_plus.get(ft or "", t_plus.get("default", 1))
        if sellable_after_days is not None:
            t_plus = sellable_after_days      # per-instrument 优先

        pl_raw = base.get("price_limit", {}) or {}
        values = pl_raw.get("values") or {
            k: float(v) for k, v in pl_raw.items() if isinstance(v, (int, float))}

        tf_raw = base.get("transfer_fee", {}) or {}
        tf_sched = [
            (date.fromisoformat(str(x["from"])), date.fromisoformat(str(x["to"])),
             float(x["rate"]))
            for x in (tf_raw.get("schedule") or [])
        ]
        has_flat = "rate" in tf_raw
        if tf_sched and has_flat:
            # 常数必须等于区间表最后一段 —— 否则「无日期上下文的估算路径」
            # （paper 建仓资金预估）会与逐日口径悄悄分叉。
            tail = max(tf_sched, key=lambda t: t[1])[2]
            if abs(float(tf_raw["rate"]) - tail) > 1e-15:
                raise ValueError(
                    f"transfer_fee.rate={tf_raw['rate']} 与 schedule 末段 {tail} 不一致；"
                    "常数应为现行（最新区间）费率")
        tf_flat = float(tf_raw["rate"]) if has_flat else (
            max(tf_sched, key=lambda t: t[1])[2] if tf_sched else 0.0)

        return InstrumentRules(
            symbol=Symbol(*symbol.split(".")),
            sec_type=sec_type,
            commission=Commission(rate=comm.get("rate", 0.0), min=comm.get("min", 0.0),
                                  per_order=comm.get("per_order", True)),
            tax=TaxSchedule(tax_sched),
            transfer_fee_rate=tf_flat,
            transfer_fee_schedule=TransferFeeSchedule(tf_sched) if tf_sched else None,
            price_limit=PriceLimit(
                mode=pl_raw.get("mode", "by_board"),
                values={str(k): float(v) for k, v in values.items()},
                st_by_board={str(k): float(v)
                             for k, v in (pl_raw.get("st_by_board") or {}).items()},
                by_code_prefix={str(k): float(v)
                                for k, v in (pl_raw.get("by_code_prefix") or {}).items()},
                by_code={str(k): float(v)
                         for k, v in (pl_raw.get("by_code") or {}).items()},
            ),
            lot_size=base.get("lot_size", 100),
            sellable_after_days=int(t_plus),
            is_st=is_st,
            price_tick=float(base.get("price_tick", 0.01)),
            no_price_limit=no_price_limit,
            track_index_limit=track_index_limit,
        )
