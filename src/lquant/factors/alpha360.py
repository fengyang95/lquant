"""Alpha360 原生重实现（360 个特征）—— 对照 qlib ``Alpha360DL`` 源码。

## 精确公式（以 qlib ``contrib/data/loader.py::Alpha360DL`` 为准）

6 个字段 × 60 个滞后 = 360 个特征。字段顺序固定：
``CLOSE, OPEN, HIGH, LOW, VWAP, VOLUME``；每个字段内部滞后从 59 降到 0。

**价格类**（CLOSE/OPEN/HIGH/LOW/VWAP）::

    FIELD{i} = Ref($field, i) / $close      # i = 59..1
    FIELD0   = $field / $close

**成交量类**（注意分母不是 close！）::

    VOLUME{i} = Ref($volume, i) / ($volume + 1e-12)   # i = 59..1
    VOLUME0   = $volume / ($volume + 1e-12)

三个容易写错的点，都已在源码里核对：

1. **VOLUME 用 volume 归一，不是 close** —— 价格比值与成交量比值混用会让
   量纲彻底错掉（qlib 源码里 volume 那一族单独走 ``($volume+1e-12)``）。
2. **``+1e-12``** 是为了避免停牌日 volume=0 时除零；lquant 侧 volume=0
   的行会得到 0/1e-12 = 0（不是 inf），与 qlib 一致。
3. **滞后方向**：``Ref($x, i)`` 是 **i 期之前**（过去），不是未来。
   ``CLOSE0`` 恒等于 1.0（``$close/$close``），可当作自检哨兵。

## 与 Alpha158 的关系

Alpha360 是「原始价量的近 60 日窗口」，几乎不做加工（只有归一化），
留给模型自己去学；Alpha158 是手工设计的因子。两者互补，不是替代。
本模块的 API 形状与 ``factors/qlib_alpha.py`` 完全一致（同样的
``has_factor`` / ``list_builtin`` / ``resolve_name`` / ``compute_all`` / ``compute``），
便于在同一个流程里切换。

## 无未来函数

只用 ``shift(i)``（i ≥ 0）与当日值，绝不引用未来。测试里有一条
「改未来价格不得改变过去特征」的断言钉死这一点。
"""
from __future__ import annotations

import polars as pl

__all__ = ["FIELDS", "LAGS", "FEATURE_COUNT", "has_factor", "list_builtin",
           "resolve_name", "compute_all", "compute", "feature_names"]

#: 字段顺序（与 qlib 一致，决定 360 列的顺序）
FIELDS: tuple[str, ...] = ("CLOSE", "OPEN", "HIGH", "LOW", "VWAP", "VOLUME")

#: 每个字段的滞后数（0..59）
LAGS = 60

#: 特征总数 = 6 × 60
FEATURE_COUNT = len(FIELDS) * LAGS

#: 字段 → 原始列（VWAP 需要现算）
_SOURCE = {"CLOSE": "close", "OPEN": "open", "HIGH": "high",
           "LOW": "low", "VWAP": "_vwap", "VOLUME": "volume"}

#: 成交量族的归一化分母（与价格族不同，见模块 docstring）
_VOLUME_EPS = 1e-12


def _prep(df: pl.DataFrame) -> pl.DataFrame:
    """排序 + 派生 ``_vwap``。要求含 OHLCV/amount。"""
    need = {"trade_date", "symbol", "open", "high", "low", "close",
            "volume", "amount"}
    miss = need - set(df.columns)
    if miss:
        raise KeyError(f"Alpha360 计算缺少列: {sorted(miss)}")
    d = df.sort(["symbol", "trade_date"])
    # vwap = amount/volume；停牌（volume<=0）置 null —— 与导出器同一口径
    return d.with_columns(
        pl.when(pl.col("volume") > 0)
        .then(pl.col("amount") / pl.col("volume"))
        .otherwise(None)
        .alias("_vwap")
    )


def feature_names() -> list[str]:
    """360 个特征名（顺序即 ``compute_all`` 的输出列序）。"""
    return [f"{f}{i}" for f in FIELDS for i in range(LAGS - 1, -1, -1)]


def resolve_name(name: str) -> tuple[str, int]:
    """``'CLOSE5'`` → ``('CLOSE', 5)``；不认识抛 ``KeyError``。"""
    n = name.upper()
    for f in FIELDS:
        if n.startswith(f):
            tail = n[len(f):]
            if tail.isdigit():
                i = int(tail)
                if 0 <= i < LAGS:
                    return f, i
            break
    raise KeyError(f"未知 Alpha360 特征 {name!r}（形如 CLOSE0..CLOSE59 / VOLUME0..VOLUME59）")


def has_factor(name: str) -> bool:
    """是否命中 Alpha360 目录；不抛错（供 API 探测用）。"""
    try:
        resolve_name(name)
        return True
    except KeyError:
        return False


def list_builtin() -> list[dict]:
    """全部 360 个特征清单（与 ``qlib_alpha.list_builtin`` 同结构）。"""
    out = []
    for f in FIELDS:
        src = _SOURCE[f]
        for i in range(LAGS - 1, -1, -1):
            if f == "VOLUME":
                formula = (f"Ref($volume, {i})/($volume+1e-12)" if i
                           else "$volume/($volume+1e-12)")
            else:
                formula = f"Ref(${src.strip('_')}, {i})/$close" if i else f"${src.strip('_')}/$close"
            out.append({"name": f"{f}{i}", "family": f.lower(), "window": i,
                        "formula": formula})
    return out


def _expr(field: str, lag: int) -> pl.Expr:
    """单个特征的表达式（价格族按 close 归一，成交量族按 volume 归一）。"""
    src = _SOURCE[field]
    if field == "VOLUME":
        # 分母是**当日** volume（不是 close），+eps 防停牌日除零
        denom = pl.col("volume") + _VOLUME_EPS
        if lag == 0:
            return pl.col("volume") / denom
        return pl.col("volume").shift(lag).over("symbol") / denom
    denom = pl.col("close")
    if lag == 0:
        return pl.col(src) / denom
    return pl.col(src).shift(lag).over("symbol") / denom


def compute_all(df: pl.DataFrame, *, families: set[str] | None = None,
                lags: tuple[int, ...] | None = None) -> pl.DataFrame:
    """批量计算。``families=None`` 算全部；``lags`` 指定要哪些滞后（默认 0..59）。

    返回原 df + 各特征列。未覆盖的滞后列不会出现 —— 调用方若需要固定 360 列，
    用 ``families=None, lags=None``。
    """
    d = _prep(df)
    want = [f for f in FIELDS if families is None or f in {x.upper() for x in families}]
    lag_list = sorted(lags) if lags is not None else list(range(LAGS))
    bad = [x for x in lag_list if not (0 <= x < LAGS)]
    if bad:
        raise ValueError(f"滞后超出 0..{LAGS - 1}: {bad}")
    exprs = [_expr(f, i).alias(f"{f}{i}") for f in want for i in lag_list]
    return d.with_columns(exprs) if exprs else d


def compute(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """算单个特征，返回带 ``_factor`` 列的 df（与 ``qlib_alpha.compute`` 同接口）。"""
    field, lag = resolve_name(name)
    d = _prep(df)
    return d.with_columns(_expr(field, lag).alias("_factor"))
