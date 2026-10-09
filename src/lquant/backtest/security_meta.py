"""从 security 表读 per-instrument 撮合元数据。

回测引擎需要的 per-instrument 属性（是否 ST、退市日、上市日、名称）在数据层，
不在行情里。**任何异常都必须退化为空字典** —— 合成数据 / 无库沙箱下回测
要照常跑，绝不因为元数据缺失而抛错（宁可退化成「无 ST、无退市」，也不能崩）。

历史上这条查询只存在于 JQRunner 内部（`_load_security_meta`），导致原生
Engine 路径拿不到 is_st / fund_type —— ST 股按 10% 涨跌停、QDII/黄金/债券
ETF 按 T+1，两条路径口径分叉。现在两条路径共用本模块。
"""
from __future__ import annotations

from datetime import date

__all__ = ["load_security_meta", "load_sector_map", "merge_meta"]


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def load_security_meta() -> dict[str, dict]:
    """{symbol: {is_st, delist_date, list_date, name}}；失败返回 {}。"""
    try:
        from lquant.data.store import catalog
        with catalog.reader() as con:
            rows = con.execute(
                "SELECT symbol, is_st, delist_date, list_date, name FROM security"
            ).fetchall()
    except Exception:                      # noqa: BLE001 - 元数据缺失不致命
        return {}
    out: dict[str, dict] = {}
    for sym, is_st, delist, listd, name in rows:
        m: dict = {"is_st": bool(is_st)}
        if (d := _as_date(delist)) is not None:
            m["delist_date"] = d
        if (d := _as_date(listd)) is not None:
            m["list_date"] = d
        if name:
            m["name"] = str(name)
        out[str(sym)] = m
    return out


def load_sector_map(std: str = "sw1") -> dict[str, str]:
    """{symbol: 行业代码}，用于事前风控的行业暴露约束。

    口径：同一标的可能有多条带生效日的记录，取 ``std_date <= 今天`` 里最新的
    一条（**不能用未来的行业归属**，否则调仓时用了事后信息）。
    ``industry_classify`` 读不到时返回 {} —— 由调用方决定是报错还是跳过：
    风控侧的选择是**报错**（开了行业约束却没有行业数据 = 检查了个寂寞）。
    """
    try:
        from lquant.data.store import catalog
        with catalog.reader() as con:
            rows = con.execute(
                "SELECT symbol, std, code, name, std_date FROM industry_classify"
            ).fetchall()
    except Exception:                      # noqa: BLE001 - 元数据缺失不致命
        return {}
    today = date.today()
    best: dict[str, tuple[date, str]] = {}
    for sym, row_std, code, name, std_date in rows:
        if std and row_std and str(row_std) != std:
            continue
        d = _as_date(std_date)
        if d is not None and d > today:      # 未来生效的归属不参与当前判断
            continue
        key = code or name
        if not key:
            continue
        cur = best.get(str(sym))
        if cur is None or (d is not None and (cur[0] is None or d > cur[0])):
            best[str(sym)] = (d, str(key))
    return {k: v[1] for k, v in best.items()}


def merge_meta(base: dict[str, dict] | None,
               override: dict[str, dict] | None) -> dict[str, dict]:
    """逐标的合并元数据：override 的键优先，未提到的键保留 base。

    显式注入只覆盖它声明过的字段 —— 否则调用方给一个 {"is_st": True}
    就会把 DB 里的 delist_date 一并抹掉。
    """
    out: dict[str, dict] = {k: dict(v) for k, v in (base or {}).items()}
    for sym, m in (override or {}).items():
        out.setdefault(sym, {}).update(m or {})
    return out
