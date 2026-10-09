"""筹码分布 CYQ（Chip/筹码，业内通称）：成本分布与获利盘比例。

借鉴 InStock（14.7k star）``kline/cyq.py`` 的思路，按 lquant 风格重写：

**换手率衰减模型**（CYQ 的通行算法）：
    每个交易日，历史筹码按当日换手率 ``t = volume / float_shares`` 被部分换手
    —— 旧筹码留存 ``(1 - t)``，当日新筹码 ``t`` 落在成交代表价附近。
    迭代到底得到「按价格分布的持仓成本」：
        dist = dist * (1 - t) + t * day_bin

**换手率的三种口径**（按优先级，实际用了哪个都如实回传，evidence 不静默）：
    1. 调用方显式传入 ``float_shares``（标量或逐日列）—— 最准；
    2. ``df`` 自带的 ``turnover_rate`` 列（%，自由流通口径）—— 真实数据一直带这列
       （``read_daily`` 会 select 它），此前被完全忽略，导致生产链路 100% 落进代理口径；
    3. 代理：``volume_i / (window × 截至前一日的 window 日均量)`` —— 量级近似。
       此前是 ``volume_i / 截至前一日累积均量``：那是「当日量 / 自身均值」，与流通
       股本无关，量级恒在 1 附近 → ``1 - t ≈ 0`` → 历史筹码每天被清零（实测
       39.5% 的交易日 t 被 clip 到 1.0），它压根不是换手率。
    三种口径都严格因果（只用截至当日的信息）—— 这是滚动指标通过前缀不变性门禁的
    前提；代理口径会显式标注（``CYQ_PROXY_NOTE``）。

**无网格解析式 vs 直方图（两套口径，数值不等价，不可互相替代）**：
    - ``profit_ratio`` / 注册指标 ``cyq_profit_ratio`` 用**解析式滚动**：
      每日筹码视作单点（成交代表价 ``(H+L+2C)/4``），权重随衰减累积，
      获利比例 = Σ(成本 < 现价的权重) / Σ(总权重)。无全局网格 →
      每日值只依赖前缀数据，严格满足 ``assert_no_lookahead`` 门禁。
      **这是获利盘比例的唯一权威口径。**
    - ``cost_distribution`` 用**直方图**（bins 等宽价格格）：把当日筹码按
      代表价落进价格格，输出**bin 中心**，网格边界取全量数据的 min/max ——
      前缀重算会漂移，**只用于末端快照可视化**，不进滚动序列、不参与回测信号。
      用「bin 中心 < 现价」从直方图再反推一个「获利比例」是**粗粒度近似**：
      量化到 bin 中心后与解析式（用真实代表价逐点比较）必然有偏差，
      价格跨度大 / bins 少时甚至可能得到 1.0。**不要拿直方图导出的比例
      替代 ``profit_ratio``** —— 两者数值不等价是设计使然（精确 vs 量化），
      不是需要对齐的 bug。

输出：
    - ``cost_distribution(df, bins)``：末端成本分布（bin 中心 → 概率和 1）
    - ``profit_ratio(df)``：末端获利盘比例（0~1），evidence 带口径标注
    - 注册指标 ``cyq_profit_ratio``：逐日滚动获利盘比例列
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["cost_distribution", "profit_ratio", "CYQ_PROXY_NOTE", "CYQ_TURNOVER_NOTE"]

# 口径名（``_turnover_caliber`` 的返回）：调用方如实回传，不由入参反推。
CALIBER_EXPLICIT = "explicit"
CALIBER_TURNOVER = "turnover_rate"
CALIBER_PROXY = "proxy"

CYQ_EXPLICIT_NOTE = "float_shares 显式给定"
CYQ_TURNOVER_NOTE = "turnover_rate 列（自由流通口径，%）"
CYQ_PROXY_NOTE = (
    "既无 float_shares 也无 turnover_rate：换手率按「20 日均量 ≈ 一次完整换手」"
    "的量级代理估算（近似口径，非精确换手率）"
)
_CALIBER_NOTES = {
    CALIBER_EXPLICIT: CYQ_EXPLICIT_NOTE,
    CALIBER_TURNOVER: CYQ_TURNOVER_NOTE,
    CALIBER_PROXY: CYQ_PROXY_NOTE,
}

# 代理口径假设「window 个交易日的均量对应一次完整换手」。20 日 ≈ 日换手 5%，
# 落在 A 股常见的 1%~8% 区间；它只是量级近似，真实数据请走 turnover_rate。
_PROXY_TURNOVER_WINDOW = 20

# CYQ 依赖的输入列。注册指标的 inputs 与缺列防御共用这一份清单。
_REQUIRED = ("high", "low", "close", "volume")


def _price(df: pl.DataFrame) -> pl.Series:
    """当日成交代表价：经典 CYQ 用 ``(H+L+2C)/4`` —— 比单 close 更贴真实成交重心。"""
    return ((df["high"] + df["low"] + 2 * df["close"]) / 4).rename("_px")


def _usable_price(df: pl.DataFrame) -> bool:
    """价格列齐备且至少有一行能算出成交代表价。

    为什么「列存在但全 null」要和「缺列」走同一档显式降级：列在而值全空时，
    ``float(df["low"].min())`` 拿到 None、``np.float64(None)`` 直接抛裸
    ``TypeError`` —— 调用方看到的是实现细节崩溃，而不是「无数据」；
    缺列路径却能优雅降级。两条路径必须一致（issue 6）。
    """
    if any(c not in df.columns for c in _REQUIRED):
        return False
    return _price(df).drop_nulls().len() > 0


def _proxy_turnover(v: list[float], window: int = _PROXY_TURNOVER_WINDOW) -> list[float]:
    """代理口径换手率（**量级近似**，严格因果）。

    ``t_i = volume_i / (window × 截至前一日的 window 日均量)``。

    为什么不是 ``volume_i / expanding_mean(volume)``：那是「当日量 / 自身均值」，
    与流通股本无关，量级恒在 1 附近（A 股成交量平稳时 t≈1 → ``1-t≈0`` →
    历史筹码每天被清零，实测 39.5% 的交易日被 clip 到 1.0），压根不是换手率。
    除以 ``window × 均量`` 相当于假设「window 个交易日的均量对应一次完整换手」，
    得到 A 股常见的百分位量级 —— 仍是近似，但不再自我清零。
    """
    ts: list[float] = []
    for i, vi in enumerate(v):
        if vi <= 0:
            ts.append(0.0)  # 当日无成交：不产生新筹码
            continue
        hist = v[max(0, i - window):i]      # 严格不含当日 → 前缀重算不变
        mean = (sum(hist) / len(hist)) if hist else 0.0
        if mean <= 0:
            ts.append(1.0)  # 首日/此前累计无成交：全部筹码落在当日（初始条件）
        else:
            ts.append(min(vi / (window * mean), 1.0))
    return ts


def _rate_from_column(df: pl.DataFrame) -> list[float] | None:
    """``df`` 自带 ``turnover_rate``（%）时的逐日换手率；列不可用返回 None。

    缺失/非有限/非正的当日按 t=0 处理（不产生新筹码），**不**回落到代理口径 ——
    同一列里混两种口径会让序列口径不可复现。
    """
    if "turnover_rate" not in df.columns:
        return None
    col = df["turnover_rate"].cast(pl.Float64, strict=False).to_list()
    if all(x is None for x in col):
        return None
    out: list[float] = []
    for x in col:
        if x is None or not math.isfinite(x) or x <= 0:
            out.append(0.0)
        else:
            out.append(min(x / 100.0, 1.0))
    return out


def _fallback_caliber(df: pl.DataFrame) -> tuple[list[float], str]:
    """无显式股本时的兜底口径：优先真实 ``turnover_rate``，否则量级代理。"""
    rate = _rate_from_column(df)
    if rate is not None:
        return rate, CALIBER_TURNOVER
    return _proxy_turnover(df["volume"].to_list()), CALIBER_PROXY


def _turnover_caliber(df: pl.DataFrame, float_shares) -> tuple[list[float], str]:
    """每日换手率 t ∈ [0, 1] + **实际使用的口径名**（``CALIBER_*``）。

    ``float_shares`` 三档：

    - ``pl.Series``（逐日股本列）：逐行 ``t_i = volume_i / 有效股本_i``。有效股本
      按**前向沿用**语义取（只在"看到"某个值之后才沿用 → 严格因果）：送转/增发
      当日之后换手率随之变化，而个别日 null 不会凭空产生 100× 的换手尖峰。
      首个观测值之前的行才退回兜底口径。
      **必须逐行**：把整列折成一个标量会让早期行的 t 依赖后面才出现的股本，
      前缀重算就变（未来函数）。
    - 正数标量：``t = volume / float_shares``。非有限或 ≤0 **显式报错** ——
      此前静默退回代理口径，却仍对外宣称「float_shares 显式给定」，即 evidence 说谎。
    - ``None``：走 :func:`_fallback_caliber`（turnover_rate 列 → 量级代理）。

    返回口径名是硬要求：``profit_ratio`` 要把它如实回传，不能由入参的
    truthiness 反推实际口径。
    """
    v = df["volume"].to_list()
    if isinstance(float_shares, pl.Series):
        fs = float_shares.cast(pl.Float64, strict=False).to_list()
        if len(fs) != len(v):
            raise ValueError(f"float_shares 长度 {len(fs)} 与面板 {len(v)} 不一致")
        out: list[float] = []
        last: float | None = None
        used_explicit = False
        fallback: tuple[list[float], str] | None = None
        for i, vi in enumerate(v):
            cur = fs[i]
            if cur is not None and math.isfinite(cur) and cur > 0:
                last = cur  # 只在看到之后才沿用 → 严格因果
            if last is not None:
                used_explicit = True
                out.append(min(vi / last, 1.0) if vi > 0 else 0.0)
            else:
                if fallback is None:
                    fallback = _fallback_caliber(df)
                out.append(fallback[0][i])
        if used_explicit:
            return out, CALIBER_EXPLICIT
        return fallback if fallback is not None else _fallback_caliber(df)
    if float_shares is not None:
        if not isinstance(float_shares, (int, float)) or not math.isfinite(float_shares):
            raise ValueError(f"float_shares 必须是正的有限股数，收到 {float_shares!r}")
        if float_shares <= 0:
            raise ValueError(f"float_shares 必须为正，收到 {float_shares!r}")
        return [min(vi / float_shares, 1.0) if vi > 0 else 0.0 for vi in v], CALIBER_EXPLICIT
    return _fallback_caliber(df)


def _rolling_ratios(
    df: pl.DataFrame, float_shares, ts: list[float] | None = None
) -> list[float | None]:
    """逐日获利盘比例（解析式滚动，严格前缀不变）。

    第 i 日新筹码 ``t_i`` 落在 ``p_i``，此后每过一天留存 ``(1 - t_j)``。
    终点 t 的获利比例 = Σ_{i≤t, p_i < close_t} w_i / Σ_{i≤t} w_i。

    ``ts`` 可由调用方预先算好（并据此拿到口径名），避免同一份数据算两遍口径；
    传 None 时本函数内部走 ``_turnover_caliber``。

    实现为预分配数组逐日推进：每日 O(size)，总 O(n²) 但全是 NumPy 向量操作
    （n=250 时约 6 万次元素级运算，微秒级）；代价换正确性 —— 排序前缀和的
    O(n log n) 写法会把 t 之后的筹码算进 t 时刻，那才是真未来函数。
    """
    n = df.height
    px = _price(df).to_list()
    closes = df["close"].to_list()
    if ts is None:
        ts, _ = _turnover_caliber(df, float_shares)
    ps = np.empty(n)
    ws = np.empty(n)
    out: list[float | None] = []
    size = 0
    for i in range(n):
        if size:
            ws[:size] *= 1.0 - ts[i]
        p_i = px[i]
        c_i = closes[i]
        if p_i is None or c_i is None:
            # 价格缺失：该日筹码落点/现价不可知 → 显式 None（数据缺失 ≠ 0/1），
            # 且不新增筹码。已有筹码仍按当日换手率衰减（换手真实发生了）。
            # 不打补丁的话 ``ps[size] = None`` 会抛裸 TypeError。
            out.append(None)
            continue
        ps[size] = p_i
        ws[size] = ts[i]
        size += 1
        total = float(ws[:size].sum())
        if total <= 0:
            # 历史清零且当日无成交：无筹码信息，显式 None，不伪造 0 或 1
            out.append(None)
            continue
        profit = float(ws[:size][ps[:size] < c_i].sum())
        out.append(round(profit / total, 4))
    return out


def cost_distribution(
    df: pl.DataFrame, bins: int = 60, float_shares: float | None = None
) -> list[tuple[float, float]]:
    """末端成本分布。返回 [(bin 中心价, 概率)]，概率和为 1。

    **仅用于末端快照可视化**：网格边界取全量数据的 min(low)..max(high)，
    换一段前缀网格就变 —— 不满足前缀不变性，绝不能把它的逐日值当信号列。

    迭代：每根 K 线后 ``dist = dist*(1-t) + t*day_bin``。bins 是常数量级
    （默认 60），纯 Python 循环足够，性能瓶颈不在这里。

    **口径警告**：权重记在 bin 中心，用「bin 中心 < 现价」反推的获利比例
    与 :func:`profit_ratio` 的解析式**不等价**（量化 vs 精确），不可互相替代
    —— 详见模块 docstring。

    **降级语义**：缺列、全 null 价格列、空表一律返回 ``[]``（空分布），
    与 `compute` 缺列分支的显式降级一致，不抛裸 TypeError；个别行价格
    缺失时该日不落筹码（换手衰减照常），不崩溃。
    """
    if df.is_empty() or not _usable_price(df):
        return []
    px = df.with_columns(_price(df))
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        hi = lo + 1e-9
    width = (hi - lo) / bins
    dist = [0.0] * bins
    ts, _ = _turnover_caliber(df, float_shares)

    for row, t in zip(px.iter_rows(named=True), ts, strict=True):
        for i in range(bins):
            dist[i] *= 1 - t
        p = row["_px"]
        if p is None:
            continue  # 价格缺失的交易日不落筹码：不把 None 当成某个价格格
        idx = min(int((p - lo) / width), bins - 1)
        dist[idx] += t
    total = sum(dist) or 1.0
    return [(lo + (i + 0.5) * width, dist[i] / total) for i in range(bins) if dist[i] > 0]


def profit_ratio(df: pl.DataFrame, float_shares=None) -> dict:
    """末端获利盘比例：成本低于现价的筹码占比（0~1）。>0.9 常被视为「高位获利盘重」。

    与滚动指标 ``cyq_profit_ratio`` 同一解析式实现（同一 ``_turnover_caliber``
    口径解析），两者对同一输入给同一答案。**它是获利盘比例的权威口径**：
    从 :func:`cost_distribution` 直方图（bin 中心）反推的比例是量化近似，
    数值不等价，不可替代。

    ``proxy`` / ``note`` 由**实际使用的口径**决定，不由入参反推 —— 此前
    ``float_shares=0`` 会静默走代理口径却宣称「显式给定」，恰好在本函数
    唯一的意义（把口径诚实回传给调用方）上说谎。

    **降级语义**：空表、缺列、全 null 价格列一律返回 ``profit_ratio=None``
    且 ``note="无数据"``，与 `compute` 缺列分支一致，不抛裸 TypeError。
    """
    if df.is_empty() or not _usable_price(df):
        return {"profit_ratio": None, "note": "无数据", "proxy": False, "caliber": None}
    ts, caliber = _turnover_caliber(df, float_shares)
    ratios = _rolling_ratios(df, float_shares, ts)
    return {
        "profit_ratio": ratios[-1] if ratios else None,
        "note": _CALIBER_NOTES[caliber],
        "proxy": caliber == CALIBER_PROXY,
        "caliber": caliber,
    }


# 指标注册：获利盘比例作为「预计算信号列」反哺因子层（registry docstring 的口径）。
# 输出 cyq_profit_ratio ∈ [0,1]；category 归 oscillator（0~1 比率画子图，与 RSI 同类），
# registry.CATEGORIES 没有 "chip" 类别，不为此单开类别污染前端枚举。
@register_indicator(
    "cyq_profit_ratio",
    label="获利盘比例",
    category="oscillator",
    min_window=5,
    inputs=_REQUIRED,
    outputs=("cyq_profit_ratio",),
)
def add_cyq_profit_ratio(df: pl.DataFrame) -> pl.DataFrame:
    """逐日滚动获利盘比例（0~1）。解析式实现，严格前缀不变（无未来函数）。

    缺输入列时输出全 null 占位（保持行数）—— outputs 声明了这列，下游
    （前端/批量链）期望它存在，所以补 null 列而不是原样返回；
    与 ``add_turnover_ma`` 的「缺列 noop」同一防御哲学，取更保守的一档。

    换手率口径与 ``profit_ratio`` **完全同源**（都走 ``_turnover_caliber``）：
    逐行股本列按前向沿用取有效股本、真实 ``turnover_rate`` 列优先于量级代理。
    此前只有本注册链路做 ``forward_fill()``，而 ``profit_ratio`` 拿原始列、
    于是同一份数据两个入口给出不同答案（实测 0.9616 vs 0.0，单行 null 甚至
    把换手率从 0.01 放大 100 倍），与「两者口径一致」的 docstring 矛盾。
    """
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("cyq_profit_ratio"))
    # 这里**不做** forward_fill：前向沿用语义已收口在 _turnover_caliber，
    # 与 profit_ratio(df, float_shares=<同一列>) 走同一条代码路径。
    fs: float | pl.Series | None = None
    if "float_shares" in df.columns:
        col = df["float_shares"].cast(pl.Float64, strict=False)
        if col.drop_nulls().len() > 0:
            fs = col
    ratios = _rolling_ratios(df, fs)
    return df.with_columns(pl.Series("cyq_profit_ratio", ratios, dtype=pl.Float64))
