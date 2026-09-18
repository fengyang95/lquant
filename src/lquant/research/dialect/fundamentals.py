"""get_fundamentals 的查询 DSL 与 PIT 解析器（M6b）。

提供聚宽风格的对象模型：

    from lquant.research.dialect.fundamentals import income, query, valuation

    q = query(income.net_profit, valuation.pe_ratio).filter(
        income.code.in_(["000001.SZ"])).order_by(income.net_profit).limit(5)
    df = get_fundamentals(q, date="2026-06-15")

防前视约束：
- income/balance/cashflow 走 financial_pit，且强制 pub_date <= t（无公告日的行永不返回）；
  每个 (symbol, item) 只取 pub_date <= t 中 stat_date 最新的那条报告。
- valuation 从日线派生（pe_ttm/pb_mrq/total_mv/...），取 trade_date <= t 的最近一根 bar。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date as _date

import polars as pl

# JQ 表名 → financial_pit 的 item 前缀（tushare 原生表名）。
# 实际落库前缀：income / balancesheet / cashflow / indicator
# （profit/balance/growth/operation 旧 BaoStock 前缀在全库为 0 行）。
_KIND_PREFIX = {
    "income": "income",
    "balance": "balancesheet",
    "cashflow": "cashflow",
    "indicator": "indicator",
    # growth/operation 在 financial_pit 无独立前缀行（全库 0 行），真实数据
    # 落在 indicator 前缀下：同比类（*_yoy）与周转类（*_turn）尾段。
    "growth": "indicator",
    "operation": "indicator",
}

# 兼容历史落库前缀：查数时额外带上旧前缀，老数据不丢。
_LEGACY_PREFIX = {"income": "profit", "balance": "balance"}

# JQ 财务字段名 → tushare 尾段（item = "<prefix>.<tushare列名>"）。
# 六张表全部走白名单校验；growth/operation 借 indicator 前缀的数据。
# 尾段经真实库 DISTINCT 勘察确认（2026-09-14）。
FIELD_MAP: dict[str, dict[str, str]] = {
    "income": {
        "net_profit": "n_income_attr_p",
        "nparent_netprofit": "n_income_attr_p",
        "total_operating_revenue": "total_revenue",
        "operating_profit": "operate_profit",
        "total_operating_cost": "total_cogs",
        "total_profit": "total_profit",
    },
    "balance": {
        "total_assets": "total_assets",
        "total_liability": "total_liab",
        "total_owner_equities": "total_hldr_eqy_exc_min_int",
        "equities_parent_company_owners": "total_hldr_eqy_exc_min_int",
    },
    "indicator": {
        "roe": "roe",
        "roa": "roa_yearly",
        "gross_income_ratio": "gross_margin",
        "net_profit_margin": "netprofit_margin",
        "adjusted_profit": "op_income",
        "net_profit_growth_rate": "netprofit_yoy",
    },
    "cashflow": {
        "net_operate_cash_flow": "n_cashflow_act",
        "net_invest_cash_flow": "n_cashflow_inv_act",
        "net_finance_cash_flow": "n_cash_flows_fnc_act",
        "net_increase_cash": "n_incr_cash_cash_equ",
        "cash_end_period": "c_cash_equ_end_period",
        "free_cashflow": "free_cashflow",
    },
    # JQ growth 表：同比增速字段 → indicator.<*_yoy> 尾段。
    # 或有备选：inc_net_profit_annual_year_on_year → dt_netprofit_yoy。
    "growth": {
        "inc_revenue_year_on_year": "or_yoy",
        "inc_net_profit_year_on_year": "netprofit_yoy",
        "inc_net_profit_annual_year_on_year": "dt_netprofit_yoy",
        "inc_operating_profit_year_on_year": "op_yoy",
        "inc_total_profit_year_on_year": "ebt_yoy",
        "inc_total_assets_year_on_year": "assets_yoy",
    },
    # JQ operation 表：营运能力字段 → indicator.<*_turn> 尾段。
    "operation": {
        "total_asset_turnover_rate": "assets_turn",
        "accounts_receivables_turnover_rate": "ar_turn",
        "current_asset_turnover_rate": "ca_turn",
        "fixed_asset_turnover_rate": "fa_turn",
    },
}

# JQ valuation 字段 → 日线列
_VALUATION_MAP = {
    "pe_ratio": "pe_ttm",
    "pb_ratio": "pb_mrq",
    "ps_ratio": "ps_ttm",
    "pcf_ratio": "pcf_ncf_ttm",
    "market_cap": "total_mv",
    "circulating_market_cap": "float_mv",
    "turnover_ratio": "turnover_rate",
}


def _norm(name: str) -> str:
    """item 名归一化：camelCase / snake_case 等价（netProfit == net_profit）。"""
    return name.lower().replace("_", "")


@dataclass(frozen=True)
class Column:
    table: str
    name: str

    def in_(self, values) -> _Cond:
        return _Cond(self, [str(v) for v in values])


@dataclass(frozen=True)
class _Cond:
    column: Column
    values: list[str]


class _Table:
    """fundamentals.<attr> → Column（未知名在解析期报错，不在访问期）。"""

    def __init__(self, table: str) -> None:
        self._table = table

    def __getattr__(self, name: str) -> Column:
        return Column(self._table, name)


fundamentals = _Table("fundamentals")
valuation = _Table("valuation")
income = _Table("income")
balance = _Table("balance")
cashflow = _Table("cashflow")
indicator = _Table("indicator")
growth = _Table("growth")
operation = _Table("operation")

TABLES = (fundamentals, valuation, income, balance, cashflow, indicator, growth, operation)


@dataclass(frozen=True)
class Query:
    cols: tuple[Column, ...]
    conds: tuple[_Cond, ...] = ()
    order: tuple[Column, bool] | None = None   # (col, ascending)
    limit_n: int | None = None

    def filter(self, *conds: _Cond) -> Query:
        return replace(self, conds=self.conds + tuple(conds))

    def order_by(self, col: Column, ascending: bool = True) -> Query:
        return replace(self, order=(col, ascending))

    def limit(self, n: int) -> Query:
        return replace(self, limit_n=int(n))


def query(*cols: Column) -> Query:
    if not cols:
        raise ValueError("query() 至少需要一个字段")
    for c in cols:
        if not isinstance(c, Column):
            raise ValueError(f"query() 只接受字段对象，收到 {type(c).__name__}")
        if c.name in ("day", "statDate"):
            raise ValueError(f"{c.table}.{c.name} 是保留列名，不能用字段引用")
    return Query(cols=tuple(cols))


# ---------- 解析 ----------


def _code_values(q: Query) -> set[str] | None:
    """提取 code 过滤值；返回 None 表示未按 code 过滤。多个 code 条件取交集。"""
    out: set[str] | None = None
    for cond in q.conds:
        if cond.column.name == "code":
            vals = set(cond.values)
            out = vals if out is None else out & vals
        else:
            raise ValueError(
                f"get_fundamentals 目前仅支持按 code 过滤，收到 "
                f"{cond.column.table}.{cond.column.name}")
    return out


def _resolve_symbol_scope(q: Query) -> list[str] | None:
    """显式 code 过滤生效；否则不设 scope（全市场，对齐聚宽语义）。"""
    codes = _code_values(q)
    if codes is not None:
        return sorted(codes)
    return None


def _financial_frame(items: dict[Column, str], symbols: list[str] | None,
                     day: _date) -> dict[Column, dict[str, tuple]]:
    """{Column: {symbol: (stat_date, value)}}，PIT：pub_date <= day，
    同 (symbol, item) 取 stat_date 最新。"""
    from lquant.data.store import catalog

    if not items:
        return {}
    prefixes = sorted({_KIND_PREFIX[c.table] for c in items}
                      | {_LEGACY_PREFIX[c.table] for c in items
                         if c.table in _LEGACY_PREFIX})
    conds = ["pub_date <= ?",
             "(" + " OR ".join("item LIKE ?" for _ in prefixes) + ")"]
    params: list = [day, *[f"{p}.%" for p in prefixes]]
    if symbols:
        conds.append("symbol IN (" + ", ".join("?" * len(symbols)) + ")")
        params.extend(symbols)
    sql = (f"SELECT symbol, item, stat_date, pub_date, value FROM financial_pit "
           f"WHERE {' AND '.join(conds)}")
    with catalog.reader() as con:
        rows = con.execute(sql, params).fetchall()
    # 每 (symbol, item) 保留 (stat_date, pub_date) 最新一条 —— 同报告期有修订
    # 公告时取后公告的那份，避免依赖物理行序
    best: dict[tuple[str, str], tuple] = {}
    for sym, item, stat_d, pub_d, value in rows:
        key = (sym, item)
        if key not in best or (stat_d, pub_d) >= best[key][:2]:
            best[key] = (stat_d, pub_d, value)
    # 匹配键 = 列自身的 _norm 名 ∪ 仅「映射到该列自己」的 FIELD_MAP 尾段。
    # 同一尾段可被多个列映射到（如 net_profit 与 nparent_netprofit →
    # n_income_attr_p），命中时全部填充，不得丢列；但整表尾段不得跨列
    # 污染（查 net_profit 不能被 income.total_revenue 的行填上）。
    # 键带表前缀（含 legacy 前缀）：income 与 cashflow 的同名尾段不互相污染。
    norm_items: dict[tuple[str, str], list[Column]] = {}
    for c in items:
        keys = {_norm(c.name)} | {
            _norm(v) for k, v in FIELD_MAP.get(c.table, {}).items()
            if _norm(k) == _norm(c.name)
        }
        for p in {_KIND_PREFIX[c.table], _LEGACY_PREFIX.get(c.table)} - {None}:
            for t in keys:
                norm_items.setdefault((p, t), []).append(c)
    out: dict[Column, dict[str, tuple]] = {c: {} for c in items}
    for (sym, item), (stat_d, _pub_d, value) in best.items():
        prefix, tail = item.split(".", 1)
        for target in norm_items.get((prefix, _norm(tail)), ()):
            out[target][sym] = (stat_d, value)
    return out


def _valuation_frame(cols: list[Column], symbols: list[str] | None,
                     day: _date) -> dict[Column, dict[str, float]]:
    """{Column: {symbol: value}}，从日线取 trade_date <= day 最近一根 bar。"""
    from lquant.data.store.parquet import lake_is_empty, read_daily

    if not cols:
        return {}
    # 全新 checkout：日线库为空，返回空表。用显式的湖空判定，而不是
    # 「读出来是空帧」—— 后者在湖有数据但该区间无标的时同样成立。
    if lake_is_empty("daily"):
        return {c: {} for c in cols}
    lf = read_daily(symbols=symbols, end=day)
    unknown = [c.name for c in cols
               if c.name not in _VALUATION_MAP]
    if unknown:
        raise ValueError(f"valuation.{unknown[0]} 未支持，可用："
                         f"{sorted(_VALUATION_MAP)}")
    colnames = sorted({_VALUATION_MAP[c.name] for c in cols})
    last = (lf.filter(pl.col("trade_date") <= day)
              .sort("trade_date")
              .group_by("symbol").last()
              .select(["symbol", *colnames])
              .collect())
    out: dict[Column, dict[str, float]] = {c: {} for c in cols}
    for row in last.iter_rows(named=True):
        for c in cols:
            v = row.get(_VALUATION_MAP[c.name])
            if v is not None:
                out[c][row["symbol"]] = float(v)
    return out


def _resolve_column(c: Column, day: _date, symbols: list[str] | None,
                    fin_cache: dict, val_cache: dict) -> dict[str, object]:
    if c.name == "code":
        base: set[str] = set()
        for cache in (*fin_cache.values(), *val_cache.values()):
            base.update(cache)
        if symbols:
            base.update(symbols)
        return {s: s for s in sorted(base)}

    if c.table == "valuation" or (c.table == "fundamentals"
                                  and c.name in _VALUATION_MAP):
        if c.name not in _VALUATION_MAP:
            raise ValueError(f"valuation.{c.name} 未支持，可用："
                             f"{sorted(_VALUATION_MAP)}")
        return dict(val_cache[c])

    prefix = _KIND_PREFIX.get(c.table)
    if prefix is None:
        raise ValueError(f"表 {c.table} 未支持，可用：{sorted(_KIND_PREFIX)} + valuation")
    return {sym: v for sym, (_, v) in fin_cache[c].items()}


def resolve(q: Query, day: _date) -> pl.DataFrame:
    """执行查询：返回带 code 列的宽表（外加 day 列）。

    同 (day, query) 的结果做进程内缓存，键 = (day, cols, conds 归一化,
    order, limit_n)。键含 day，跨日不命中；cols/conds/order/limit 均参与
    键，不同查询不互相污染。缓存帧视为只读，容量 4096 条 FIFO 淘汰。
    测试或数据更新后可用 clear_fundamentals_cache() 手动失效。
    """
    key = (day, tuple(q.cols),
           tuple((c.column, tuple(c.values)) for c in q.conds),
           q.order, q.limit_n)
    cached = _RESOLVE_CACHE.get(key)
    if cached is not None:
        return cached
    df = _resolve_uncached(q, day)
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVE_CACHE.pop(next(iter(_RESOLVE_CACHE)))   # FIFO 淘汰最早键
    _RESOLVE_CACHE[key] = df
    return df


_RESOLVE_CACHE: dict[tuple, pl.DataFrame] = {}
_RESOLVE_CACHE_MAX = 4096


def clear_fundamentals_cache() -> None:
    """清空 resolve 结果缓存（测试隔离 / 数据更新后手动失效）。"""
    _RESOLVE_CACHE.clear()


def _resolve_uncached(q: Query, day: _date) -> pl.DataFrame:
    symbols = _resolve_symbol_scope(q)
    fin_cols, val_cols = [], []
    fetch_cols = list(q.cols)
    if q.order is not None and q.order[0] not in fetch_cols:
        fetch_cols.append(q.order[0])   # 排序列参与取数，但不进入输出列
    fin_tables = {_KIND_PREFIX[c.table] for c in fetch_cols
                  if c.table in _KIND_PREFIX}
    if len(fin_tables) > 1:
        raise ValueError(
            "get_fundamentals 一次只能查一张财务表（income/balance/cashflow/"
            "indicator 不能混查，聚宽语义）；valuation 可任意混搭")
    for c in fetch_cols:
        if c.name == "code":
            continue
        if c.table == "valuation" or (c.table == "fundamentals"
                                      and c.name in _VALUATION_MAP):
            val_cols.append(c)
        else:
            fin_cols.append(c)

    for c in fin_cols:
        if c.table not in _KIND_PREFIX:
            raise ValueError(f"表 {c.table} 未支持，可用：{sorted(_KIND_PREFIX)}")
        if c.name == "code":
            continue
        fmap = FIELD_MAP.get(c.table, {})
        allowed = {_norm(k) for k in fmap} | {_norm(v) for v in fmap.values()}
        if _norm(c.name) not in allowed:
            raise ValueError(f"未知字段 {c.table}.{c.name}，可用："
                             f"{sorted(fmap)} 或 tushare 原生列名"
                             f"{sorted(set(fmap.values()))}")
    fin_items = {c: f"{_KIND_PREFIX[c.table]}.{c.name}" for c in fin_cols}

    fin_cache = _financial_frame(fin_items, symbols, day)
    val_cache = _valuation_frame(val_cols, symbols, day)

    # 统一 symbol 集合：有财务行的 ∪ 有估值行的 ∪ 显式 scope
    all_syms: set[str] = set()
    for cache in (*fin_cache.values(), *val_cache.values()):
        all_syms.update(cache)
    if symbols:
        all_syms.update(symbols)

    out_cols = [c for c in q.cols if c.name != "code"]
    rows: list[dict] = []
    for sym in sorted(all_syms):
        row: dict = {"code": sym}
        for c in fetch_cols:
            if c.name == "code":
                continue
            resolved = _resolve_column(c, day, symbols, fin_cache, val_cache)
            v = resolved.get(sym)
            if c in fin_cache and sym in fin_cache[c]:
                row[c.name] = fin_cache[c][sym][1]
                row["statDate"] = fin_cache[c][sym][0]
            elif v is not None:
                row[c.name] = v
            else:
                row[c.name] = None
        row["day"] = day
        rows.append(row)

    empty_schema = {"code": pl.Utf8,
                    **{c.name: pl.Float64 for c in out_cols},
                    "day": pl.Date}
    if fin_cols:
        empty_schema["statDate"] = pl.Date
    df = pl.DataFrame(rows) if rows else pl.DataFrame(schema=empty_schema)
    if fin_cols and "statDate" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Date).alias("statDate"))
    if q.order is not None:
        col, asc = q.order
        df = df.sort(col.name, descending=not asc, nulls_last=True)
    if q.limit_n is not None:
        df = df.head(q.limit_n)
    out_names = ["code", *(c.name for c in out_cols)]
    if fin_cols:
        out_names.append("statDate")
    out_names.append("day")
    return df.select(out_names)
