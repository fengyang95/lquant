"""基准多因子策略:基本面(低 pe × 净利同比)× 技术(20 日动量/波动)。

供两条路径消费:
- ``composite_score``:polars 评分函数,可单测(本模块职责的核心)。
- ``STRATEGY_CODE``:聚宽风格策略源码常量,由 JQRunner 沙箱执行;
  评分逻辑与 composite_score 同构(纯 python 版),有对拍测试防漂移。

PIT 红线:基本面取数只经沙箱注入的 get_fundamentals(由 jq_fundamentals
桥绑定当日交易日,resolve 保证 pub_date <= t),策略源码不携带/不覆盖日期。
"""
from __future__ import annotations

import polars as pl

# JQRunner factor_formulas 需携带这两个公式,策略内 get_factor_values 取值。
FACTOR_FORMULAS = ["pct_change_20", "rolling_std_20"]

# 月度调仓:top 20 等权;持仓跌出 top 50 卖出。
TOP_N, EXIT_N = 20, 50

# 上市不满 60 个交易日不入选。
MIN_LISTED_DAYS = 60

# 净利润同比字段:financial_pit 的 item(tushare fina_indicator netprofit_yoy,
# 归一化匹配 indicator 前缀)。Task 4 实跑前用 DISTINCT item 查询确认。
NET_PROFIT_YOY_ITEM = "indicator.netprofit_yoy"


def _zscore(s: pl.Series) -> pl.Series:
    """横截面 z 分数;std 为 0/None 时整列置 0(缺失因子按 0 合成)。"""
    std = s.std()
    if std is None or std != std or std == 0:  # noqa: PLR0124 - NaN 判别
        return s * 0.0
    return (s - s.mean()) / std


def composite_score(pe: pl.DataFrame, yoy: pl.DataFrame, mom: pl.DataFrame,
                    vol: pl.DataFrame) -> pl.DataFrame:
    """四因子合成,返回按 score 降序的 ``code, score``。

    缺失因子(缺行或 NaN)按 0 计,不丢行:
    - 外连接补齐 code 宇宙;
    - fill_null(0) 后 z 分数;
    - score = -z_pe + z_yoy + z_mom - z_vol(pe 取倒数视角、波动反向)。
    """
    df = (pe.select("code", "pe")
          .join(yoy.select("code", "yoy"), on="code", how="full", coalesce=True)
          .join(mom.select("code", "mom"), on="code", how="full", coalesce=True)
          .join(vol.select("code", "vol"), on="code", how="full", coalesce=True))
    z = pl.lit(0.0)
    expr = z
    sign = {"pe": -1.0, "yoy": 1.0, "mom": 1.0, "vol": -1.0}
    for c in ("pe", "yoy", "mom", "vol"):
        filled = pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0)
        df = df.with_columns(filled.alias(c))
    for c in ("pe", "yoy", "mom", "vol"):
        expr = expr + pl.lit(sign[c]) * pl.col(c).map_batches(_zscore, return_dtype=pl.Float64)
    df = df.with_columns(expr.alias("score"))
    return df.sort("score", descending=True).select("code", "score")


def _sandbox_score_source() -> str:
    """STRATEGY_CODE 内嵌评分函数的源码(与 composite_score 同构的纯 python 版)。"""
    yoy_table, yoy_attr = NET_PROFIT_YOY_ITEM.split(".", 1)
    yoy_field = yoy_attr          # 沙箱返回列名即 query 属性名(如 netprofit_yoy)
    return f'''

def _zscore_xs(values):
    n = len(values)
    if n == 0:
        return [0.0] * n
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    std = var ** 0.5
    if std <= 0:
        return [0.0] * n
    return [(v - mean) / std for v in values]


def _composite_score_xs(codes, pe_map, yoy_map, mom_map, vol_map):
    """与模块级 composite_score 同构:缺失因子按 0,z 分数加权合成。"""
    universe = list(codes)
    pe_vals = [pe_map.get(c, 0.0) or 0.0 for c in universe]
    yoy_vals = [yoy_map.get(c, 0.0) or 0.0 for c in universe]
    mom_vals = [mom_map.get(c, 0.0) or 0.0 for c in universe]
    vol_vals = [vol_map.get(c, 0.0) or 0.0 for c in universe]
    z_pe = _zscore_xs(pe_vals)
    z_yoy = _zscore_xs(yoy_vals)
    z_mom = _zscore_xs(mom_vals)
    z_vol = _zscore_xs(vol_vals)
    scored = [(c, -zp + zy + zm - zv)
              for c, zp, zy, zm, zv in zip(universe, z_pe, z_yoy, z_mom, z_vol)]
    scored.sort(key=lambda x: -x[1])
    return scored


def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0.001,
                   open_commission=0.0003, close_commission=0.0003,
                   min_commission=5)
    run_monthly(rebalance, monthday=1, time="open")


def rebalance(context):
    # 1) 基本面:pe_ratio(valuation)+ 净利润同比({yoy_table}.{yoy_attr})。
    #    不传 date,由沙箱绑定当日交易日 → PIT(pub_date <= t)由 resolve 保证。
    fund = get_fundamentals(query(valuation.pe_ratio, {yoy_table}.{yoy_attr}))
    pe_map = {{}}
    yoy_map = {{}}
    if fund is not None and len(fund) > 0:
        for _, row in fund.iterrows():
            code = row["code"]
            if "pe_ratio" in fund.columns:
                pe_map[code] = row["pe_ratio"]
            if "{yoy_field}" in fund.columns:
                yoy_map[code] = row["{yoy_field}"]

    codes = list(pe_map.keys())
    if not codes:
        record(n_positions=0, mv=0.0)
        return

    # 2) 技术面:20 日动量 / 20 日滚动波动(严格截至当日)。
    mom_raw = get_factor_values("pct_change_20", security_list=codes, count=1)
    vol_raw = get_factor_values("rolling_std_20", security_list=codes, count=1)
    mom_map = {{s: v[0] for s, v in mom_raw.items() if v}}
    vol_map = {{s: v[0] for s, v in vol_raw.items() if v}}

    # 3) 剔除:ST、当日停牌(无 bar)、上市不满 60 日。
    cur = get_current_data()
    tradable = []
    for c in codes:
        sec = cur[c]
        if sec.is_st:
            continue
        if sec.paused or sec.last_price != sec.last_price:  # 停牌(无当日 bar)
            continue
        hist = attribute_history(c, {MIN_LISTED_DAYS}, unit="1d", fields=("close",))
        if hist is None or len(hist) < {MIN_LISTED_DAYS}:
            continue                              # 上市不满 60 交易日
        tradable.append(c)

    if not tradable:
        record(n_positions=0, mv=0.0)
        return

    # 4) 合成 → top {TOP_N} 等权买入;持仓跌出 top {EXIT_N} 卖出。
    ranked = _composite_score_xs(
        tradable,
        {{c: pe_map.get(c) for c in tradable}},
        {{c: yoy_map.get(c) for c in tradable}},
        {{c: mom_map.get(c) for c in tradable}},
        {{c: vol_map.get(c) for c in tradable}},
    )
    top = [c for c, _ in ranked[:{TOP_N}]]
    keep = [c for c, _ in ranked[:{EXIT_N}]]

    for s, pos in list(context.portfolio.positions.items()):
        if s not in keep:
            order_target_value(s, 0)

    total = context.portfolio.total_value
    per = total / max(len(top), 1)
    for s in top:
        order_target_value(s, per)

    # 5) 观测:持仓数、持仓总市值、可见同比最大值(防前视判别用)。
    mv = sum(p.value for p in context.portfolio.positions.values())
    record(n_positions=len([p for p in context.portfolio.positions.values() if p.value > 0]),
           mv=mv,
           yoy_max=max((v for v in yoy_map.values() if v is not None), default=0.0))
'''
# 沙箱策略源码 = 嵌入评分函数 + 调仓主体(NET_PROFIT_YOY_ITEM 值注入)
STRATEGY_CODE: str = _sandbox_score_source()
