"""跨源对拍（§3.8.4）：标记与降级，绝不用于取值。

抽检分层 + 分位离群检测。核心原则写在配置里：
「一个源为主，比对只用于标记与降级，绝不用于取值」。

对拍流程（无同行源时整体为 L0 / 跳过）：
  primary   ← 湖内（authoritative）
  peer      ← 同行 provider 实拉
  逐 (symbol, trade_date) 比较 fields，按最大相对偏差分档：
    L0  偏差 <= tolerance_pct                          → 一致
    L1  数据缺失 / 单字段小偏差                        → 提示
    L2  多字段 material（>容差×10）或价差>tolerance    → 打 CROSS_SRC_DIFF 标记
    L3  缺失方有值而另一方无 / 中位数共识判为离群       → 降级候选人

输出三样东西：
  1. CROSS_SRC_DIFF issue（落 data_quality_issue，可检索）
  2. 打 quality_flags 位3（CROSS_SRC_DIFF）的 primary 副本 —— 读取侧据此降级
  3. 汇总 dict（level 计数），供 CLI 打印

可离线单测的核心是纯函数 classify_* —— 网络拉取与本模块解耦。
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from lquant.data.quality.flags import CROSS_SOURCE_DIFF as CROSS_SRC_DIFF
from lquant.data.quality.issues import Issue

__all__ = ["L0", "L1", "L2", "L3", "CrossSourceResult", "classify_divergence",
           "flag_cross_source", "summarize"]

# 分档等级
L0 = "L0"   # 一致
L1 = "L1"   # 缺失 / 小偏差（提示）
L2 = "L2"   # material 偏差（打标）
L3 = "L3"   # 离群 / 共识冲突（降级候选人）

# 默认参与比对的数值字段（open/high/low/close 绝对值差最重要的 price）
_PRICE_FIELDS = ("open", "high", "low", "close")
_VOL_FIELDS = ("volume", "amount")


@dataclass(frozen=True)
class CrossSourceResult:
    """一次对拍的产出。"""
    level: str                       # 该 (symbol, date) 的汇总等级
    max_rel_diff: dict               # field -> 最大相对偏差（主/peer 有值处）
    n_missing_primary: int           # peer 有 / primary 无 的字段数
    n_missing_peer: int              # primary 有 / peer 无 的字段数
    flag: bool                       # 是否该打 CROSS_SRC_DIFF


def _rel_diff(a: float, b: float) -> float | None:
    if a is None or b is None or a == 0:
        return None
    return abs(a - b) / abs(a)


def _to_date(dtype, expr) -> pl.Expr:
    """把 str/datetime 都归一到 Date —— join key dtype 一致化（Date 本身不动）。"""
    if dtype == pl.Date:
        return expr
    if isinstance(dtype, pl.Datetime):
        return expr.dt.date()
    return expr.str.to_date()


def classify_divergence(primary: pl.DataFrame, peer: pl.DataFrame,
                        tolerance_pct: float = 0.1,
                        fields: tuple[str, ...] | None = None) -> pl.DataFrame:
    """primary × peer 逐 (symbol, trade_date, field) 分类。

    输出 schema：symbol / trade_date / field / primary / peer /
                 rel_diff / missing / level
    纯函数，离线可测。
    """
    if not fields:
        fields = _PRICE_FIELDS + _VOL_FIELDS
    key = ["symbol", "trade_date"]
    # 只在两侧都有的列上比对 —— 缺失的列跳过，避免 select 报错
    fields = tuple(f for f in fields
                   if f in primary.columns and f in peer.columns)
    p = primary.select([*key, *fields])
    q = peer.select([*key, *fields])
    # 防御：join key dtype 必须一致。湖内 trade_date 是 Date，provider 可能回
    # Datetime/String（polars 对 dtype 不一致的 join 抛 SchemaError）——统一到 Date。
    if "trade_date" in key:
        p = p.with_columns(_to_date(p["trade_date"].dtype, pl.col("trade_date")))
        q = q.with_columns(_to_date(q["trade_date"].dtype, pl.col("trade_date")))
    # how="full" 必须 coalesce=True，否则 peer 独有行落到 symbol_right 空列，
    # 左侧 symbol/trade_date 为 None —— 便无法再 join 回 primary 打标。§3.8.4
    # 的"缺失方有值而另一方无 → L3"就因此失效。
    joined = p.join(q, on=key, how="full", coalesce=True).sort(*key)

    tol = tolerance_pct / 100.0
    rows: list[dict] = []
    for r in joined.iter_rows(named=True):
        # 行级字段覆盖：判定"整行仅单侧存在"（→ L3 降级候选）。
        n_primary = sum(1 for f in fields if r.get(f) is not None)
        n_peer = sum(1 for f in fields if r.get(f + "_right") is not None)
        for f in fields:
            a, b = r.get(f), r.get(f + "_right")
            if a is None and b is None:
                continue
            if a is None:
                # 该字段仅 peer 有值。若本行 primary 一个共同字段都没有
                # （n_primary==0）→ 整行只在 peer 存在 → L3 降级候选；否则 L1 提示。
                lvl = L3 if n_primary == 0 else L1
                rows.append({**{k: r[k] for k in key}, "field": f,
                             "primary": None, "peer": b, "rel_diff": None,
                             "missing": "primary", "level": lvl})
                continue
            if b is None:
                lvl = L3 if n_peer == 0 else L1
                rows.append({**{k: r[k] for k in key}, "field": f,
                             "primary": a, "peer": None, "rel_diff": None,
                             "missing": "peer", "level": lvl})
                continue
            d = _rel_diff(float(a), float(b))
            lvl = L1 if (d is None or d <= tol) else L2
            if a > 0 and b > 0 and (a / max(b, 1e-9) > 1.2 or b / max(a, 1e-9) > 1.2):
                lvl = L3   # 数量级差 (>20%) → 离群候选
            rows.append({**{k: r[k] for k in key}, "field": f,
                         "primary": a, "peer": b, "rel_diff": d,
                         "missing": None, "level": lvl})
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def flag_cross_source(primary: pl.DataFrame,
                      issues: pl.DataFrame) -> pl.DataFrame:
    """按 L2/L3 的 (symbol, trade_date) 把 primary 打上 CROSS_SRC_DIFF 位。

    primary 副本（immutable）。比对结果只用于标记降级，不改动任何数值。
    """
    bad = issues.filter(pl.col("level").is_in([L2, L3])).select(
        "symbol", "trade_date").unique()
    if not len(bad):
        return primary.with_columns(quality_flags=pl.col("quality_flags")
                                    if "quality_flags" in primary.columns
                                    else pl.lit(0, dtype=pl.Int32))
    if "quality_flags" not in primary.columns:
        primary = primary.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
    flagged = primary.join(
        bad.with_columns(_flag=pl.lit(True)),
        on=["symbol", "trade_date"], how="left",
    ).with_columns(
        quality_flags=pl.when(pl.col("_flag").fill_null(False))
        .then(pl.col("quality_flags") | CROSS_SRC_DIFF)
        .otherwise(pl.col("quality_flags")),
    ).drop("_flag")
    return flagged


def summarize(issues: pl.DataFrame) -> dict:
    """等级计数汇总 → CLI 展示。"""
    if not len(issues):
        return {"L0": 1, "L1": 0, "L2": 0, "L3": 0, "checked": 0}
    cnt = {lv: int(issues.filter(pl.col("level") == lv).height)
           for lv in (L1, L2, L3)}
    return {"L1": cnt[L1], "L2": cnt[L2], "L3": cnt[L3],
            "checked": len(issues)}