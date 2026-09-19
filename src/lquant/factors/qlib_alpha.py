"""Qlib Alpha158 内置因子 —— 纯 Polars/NumPy 实现。

公式逐条对照 microsoft/qlib `qlib/contrib/data/loader.py::Alpha158DL`（已核对源码）：
- kbar 9 个（无窗口）+ price 4 个（window=0）+ rolling 29 族 × 窗口 {5,10,20,30,60} = 158 个。

与 qlib 原版的差异（都写在注释里）：
- qlib 数据源的 $vwap 我们没有 → 用 amount/volume 作 VWAP 代理（tick 聚合口径的近似）；
- Slope/Rsquare/Resi 用 numpy 滑窗闭式解，等价于 qlib 的 Slope/Rsquare/Resi 算子；
- Resi 定义为残差标准差 sqrt((1-R²)·Var(y))，与 qlib Resi 一致；
- IdxMax/IdxMin 窗口内位置以「0=最老一根」计，再除以窗口长度。

入口：
- list_builtin()        → [{name, family, window, formula}]，驱动 API 与前端枚举
- compute(df, name)     → 单因子，返回带 _factor 列的 df（因子评价管线直接吃）
- compute_all(df, ...)  → 批量算全部（ML 数据集 / 相关性分析用）
"""
from __future__ import annotations

import re

import numpy as np
import polars as pl

WINDOWS: tuple[int, ...] = (5, 10, 20, 30, 60)
EPS = 1e-12

# ---------------------------------------------------------------- 注册表

# rolling 族：name 前缀 → (表达式模板, qlib 公式串模板)
_ROLLING: dict[str, str] = {
    "ROC": "Ref($close,{d})/$close",
    "MA": "Mean($close,{d})/$close",
    "STD": "Std($close,{d})/$close",
    "BETA": "Slope($close,{d})/$close",
    "RSQR": "Rsquare($close,{d})",
    "RESI": "Resi($close,{d})/$close",
    "MAX": "Max($high,{d})/$close",
    "MIN": "Min($low,{d})/$close",
    "QTLU": "Quantile($close,{d},0.8)/$close",
    "QTLD": "Quantile($close,{d},0.2)/$close",
    "RANK": "Rank($close,{d})",
    "RSV": "($close-Min($low,{d}))/(Max($high,{d})-Min($low,{d})+1e-12)",
    "IMAX": "IdxMax($high,{d})/{d}",
    "IMIN": "IdxMin($low,{d})/{d}",
    "IMXD": "(IdxMax($high,{d})-IdxMin($low,{d}))/{d}",
    "CORR": "Corr($close,Log($volume+1),{d})",
    "CORD": "Corr($close/Ref($close,1),Log($volume/Ref($volume,1)+1),{d})",
    "CNTP": "Mean($close>Ref($close,1),{d})",
    "CNTN": "Mean($close<Ref($close,1),{d})",
    "CNTD": "Mean($close>Ref($close,1),{d})-Mean($close<Ref($close,1),{d})",
    "SUMP": "Sum(Greater($close-Ref($close,1),0),{d})/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)",
    "SUMN": "Sum(Greater(Ref($close,1)-$close,0),{d})/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)",
    "SUMD": "(Sum(Greater($close-Ref($close,1),0),{d})-Sum(Greater(Ref($close,1)-$close,0),{d}))/(Sum(Abs($close-Ref($close,1)),{d})+1e-12)",
    "VMA": "Mean($volume,{d})/($volume+1e-12)",
    "VSTD": "Std($volume,{d})/($volume+1e-12)",
    "WVMA": "Std(Abs($close/Ref($close,1)-1)*$volume,{d})/(Mean(Abs($close/Ref($close,1)-1)*$volume,{d})+1e-12)",
    "VSUMP": "Sum(Greater($volume-Ref($volume,1),0),{d})/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)",
    "VSUMN": "Sum(Greater(Ref($volume,1)-$volume,0),{d})/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)",
    "VSUMD": "(Sum(Greater($volume-Ref($volume,1),0),{d})-Sum(Greater(Ref($volume,1)-$volume,0),{d}))/(Sum(Abs($volume-Ref($volume,1)),{d})+1e-12)",
}

# numpy 滑窗族（slope / r² / 残差 / 窗口内位置 / 滚动秩）
_NUMPY_FAMILIES = {"BETA", "RSQR", "RESI", "IMAX", "IMIN", "IMXD", "RANK"}

_KBAR: dict[str, str] = {
    "KMID": "($close-$open)/$open",
    "KLEN": "($high-$low)/$open",
    "KMID2": "($close-$open)/($high-$low+1e-12)",
    "KUP": "($high-Greater($open,$close))/$open",
    "KUP2": "($high-Greater($open,$close))/($high-$low+1e-12)",
    "KLOW": "(Less($open,$close)-$low)/$open",
    "KLOW2": "(Less($open,$close)-$low)/($high-$low+1e-12)",
    "KSFT": "(2*$close-$high-$low)/$open",
    "KSFT2": "(2*$close-$high-$low)/($high-$low+1e-12)",
}

_PRICE: dict[str, str] = {
    "OPEN0": "$open/$close",
    "HIGH0": "$high/$close",
    "LOW0": "$low/$close",
    "VWAP0": "$vwap/$close   # vwap=amount/volume 代理",
}

_NAME_RE = re.compile(r"^(?P<fam>[A-Z]+)(?P<d>\d+)$")


def has_factor(name: str) -> bool:
    """formula 是否命中内置因子目录（大小写不敏感）；不抛错。

    供 API 探测用：先查白名单再调用 compute，避免 except KeyError 把
    数据缺列错误一并吞掉（缺陷 #1）。
    """
    try:
        resolve_name(name)
        return True
    except KeyError:
        return False


def list_builtin() -> list[dict]:
    """全部内置因子清单（158 个）。"""
    out = [{"name": n, "family": "kbar", "window": None, "formula": f}
           for n, f in _KBAR.items()]
    out += [{"name": n, "family": "price", "window": 0, "formula": f}
            for n, f in _PRICE.items()]
    for fam, tpl in _ROLLING.items():
        out += [{"name": f"{fam}{d}", "family": fam.lower(), "window": d,
                 "formula": tpl.format(d=d)} for d in WINDOWS]
    return out


def resolve_name(name: str) -> tuple[str, int | None]:
    """'MA20' → ('MA', 20)；'KMID' → ('KBAR', None)。不认识抛 KeyError。"""
    name = name.upper()
    if name in _KBAR:
        return "KBAR", None
    if name in _PRICE:
        return "PRICE", 0
    m = _NAME_RE.match(name)
    if m and m["fam"] in _ROLLING and int(m["d"]) in WINDOWS:
        return m["fam"], int(m["d"])
    known = [x["name"] for x in list_builtin()]
    raise KeyError(f"未知内置因子 {name!r}，示例: {known[:8]}...（共 {len(known)} 个）")


# ---------------------------------------------------------------- 计算

def _prep(df: pl.DataFrame) -> pl.DataFrame:
    """排序 + 派生中间列。要求含 OHLCV/amount。"""
    need = {"trade_date", "symbol", "open", "high", "low", "close", "volume", "amount"}
    miss = need - set(df.columns)
    if miss:
        raise KeyError(f"Alpha158 计算缺少列: {sorted(miss)}")
    return (df.sort(["symbol", "trade_date"])
            .with_columns(
                # $vwap 代理：amount/volume（无成交量时退回 close）
                pl.when(pl.col("volume") > 0)
                .then(pl.col("amount") / pl.col("volume"))
                .otherwise(pl.col("close")).alias("_vwap"),
                # shift 必须按 symbol 分组：整列 shift 在 symbol-major 排序下
                # 会让每只股票首日吃到上一只股票末日的 close/volume（跨股票泄漏）
                (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_ret"),
                (pl.col("volume") / pl.col("volume").shift(1).over("symbol") - 1).abs().alias("_vchg"),
                (pl.col("close") - pl.col("close").shift(1).over("symbol")).alias("_pc"),
                (pl.col("volume") - pl.col("volume").shift(1).over("symbol")).alias("_pv"),
            ))


def _expr(fam: str, d: int) -> pl.Expr:
    """单族单窗口的 Polars 表达式（均按 symbol 分组）。"""
    c, h, l, _o, v = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("open"), pl.col("volume")
    pc, pv, ret = pl.col("_pc"), pl.col("_pv"), pl.col("_ret")
    g = lambda e: e.clip(0.0)  # noqa: E731  Greater(x,0)

    match fam:
        case "ROC":   return (c.shift(d) / c).over("symbol")
        case "MA":    return (c.rolling_mean(d) / c).over("symbol")
        case "STD":   return (c.rolling_std(d) / c).over("symbol")
        case "MAX":   return (h.rolling_max(d) / c).over("symbol")
        case "MIN":   return (l.rolling_min(d) / c).over("symbol")
        case "QTLU":  return (c.rolling_quantile(0.8, window_size=d) / c).over("symbol")
        case "QTLD":  return (c.rolling_quantile(0.2, window_size=d) / c).over("symbol")
        case "RSV":
            lo = l.rolling_min(d).over("symbol")
            hi = h.rolling_max(d).over("symbol")
            return (c - lo) / (hi - lo + EPS)
        case "CORR":
            lv = (v + 1).log()
            return pl.rolling_corr(c, lv, window_size=d).over("symbol")
        case "CORD":
            cr = c / c.shift(1)
            lvc = (v / v.shift(1) + 1).log()
            return pl.rolling_corr(cr, lvc, window_size=d).over("symbol")
        case "CNTP":  return (pc > 0).cast(pl.Float64).rolling_mean(d).over("symbol")
        case "CNTN":  return (pc < 0).cast(pl.Float64).rolling_mean(d).over("symbol")
        case "CNTD":  return ((pc > 0).cast(pl.Float64) - (pc < 0).cast(pl.Float64)).rolling_mean(d).over("symbol")
        case "SUMP":  return (g(pc).rolling_sum(d) / (pc.abs().rolling_sum(d) + EPS)).over("symbol")
        case "SUMN":  return ((-pc).clip(0.0).rolling_sum(d) / (pc.abs().rolling_sum(d) + EPS)).over("symbol")
        case "SUMD":  return ((g(pc) - (-pc).clip(0.0)).rolling_sum(d) / (pc.abs().rolling_sum(d) + EPS)).over("symbol")
        case "VMA":   return (v.rolling_mean(d) / (v + EPS)).over("symbol")
        case "VSTD":  return (v.rolling_std(d) / (v + EPS)).over("symbol")
        case "WVMA":
            x = (ret.abs() * v)
            return (x.rolling_std(d) / (x.rolling_mean(d) + EPS)).over("symbol")
        case "VSUMP": return (g(pv).rolling_sum(d) / (pv.abs().rolling_sum(d) + EPS)).over("symbol")
        case "VSUMN": return ((-pv).clip(0.0).rolling_sum(d) / (pv.abs().rolling_sum(d) + EPS)).over("symbol")
        case "VSUMD": return ((g(pv) - (-pv).clip(0.0)).rolling_sum(d) / (pv.abs().rolling_sum(d) + EPS)).over("symbol")
        case _:
            raise KeyError(fam)


def _kbar_expr(name: str) -> pl.Expr:
    c, h, l, o = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("open")
    body = (c - o) / o
    rng = h - l + EPS
    match name:
        case "KMID":  return body
        case "KLEN":  return (h - l) / o
        case "KMID2": return (c - o) / rng
        case "KUP":   return (h - pl.max_horizontal(o, c)) / o
        case "KUP2":  return (h - pl.max_horizontal(o, c)) / rng
        case "KLOW":  return (pl.min_horizontal(o, c) - l) / o
        case "KLOW2": return (pl.min_horizontal(o, c) - l) / rng
        case "KSFT":  return (2 * c - h - l) / o
        case "KSFT2": return (2 * c - h - l) / rng
        case _:
            raise KeyError(name)


def _numpy_block(gdf: pl.DataFrame, windows: tuple[int, ...]) -> dict[str, np.ndarray]:
    """BETA/RSQR/RESI/IMAX/IMIN/IMXD/RANK —— numpy 滑窗闭式解（单 symbol 组）。"""
    n = len(gdf)
    close = np.asarray(gdf["close"].cast(pl.Float64).to_numpy(), dtype=np.float64)
    high = np.asarray(gdf["high"].cast(pl.Float64).to_numpy(), dtype=np.float64)
    low = np.asarray(gdf["low"].cast(pl.Float64).to_numpy(), dtype=np.float64)
    out: dict[str, np.ndarray] = {}
    for d in windows:
        if n < d:
            for f in ("BETA", "RSQR", "RESI", "IMAX", "IMIN", "IMXD", "RANK"):
                out[f"{f}{d}"] = np.full(n, np.nan)
            continue
        t = np.arange(d, dtype=np.float64)
        tc = t - t.mean()
        var_t = (tc**2).sum()
        std_t = np.sqrt(var_t / d)

        def roll(a: np.ndarray, _d: int = d) -> np.ndarray:  # noqa: ANN001  绑定循环变量（B023）
            return np.lib.stride_tricks.sliding_window_view(a, _d)

        sw_c = roll(close)
        ybar = sw_c.mean(axis=1, keepdims=True)
        yc = sw_c - ybar
        var_y = (yc**2).mean(axis=1)
        std_y = np.sqrt(var_y)
        cov_ty = (yc * tc).mean(axis=1)
        slope = cov_ty / (var_t / d)
        r2 = np.where(std_y > 0, (cov_ty / (std_t * std_y + EPS)) ** 2, 0.0)
        resi = np.sqrt(np.clip(1.0 - r2, 0.0, None) * var_y)

        sw_h = roll(high)
        sw_l = roll(low)
        imax = sw_h.argmax(axis=1) / d
        imin = sw_l.argmin(axis=1) / d
        # Rank：窗口内 <= 当前值的比例（qlib Rank 语义）
        rank = (sw_c <= close[d - 1:, None]).sum(axis=1) / d

        def pad(x: np.ndarray, _d: int = d) -> np.ndarray:
            full = np.full(n, np.nan)
            full[_d - 1:] = x
            return full

        out[f"BETA{d}"] = pad(slope / close[d - 1:])
        out[f"RSQR{d}"] = pad(r2)
        out[f"RESI{d}"] = pad(resi / close[d - 1:])
        out[f"IMAX{d}"] = pad(imax)
        out[f"IMIN{d}"] = pad(imin)
        out[f"IMXD{d}"] = pad(imax - imin)
        out[f"RANK{d}"] = pad(rank)
    return out


def compute_all(df: pl.DataFrame, *, families: set[str] | None = None,
                windows: tuple[int, ...] = WINDOWS) -> pl.DataFrame:
    """批量计算。families=None 算全部。返回原 df + 各因子列。"""
    d = _prep(df)
    exprs: list[pl.Expr] = []

    # kbar + price
    if families is None or "KBAR" in families:
        exprs += [_kbar_expr(n).alias(n) for n in _KBAR]
    if families is None or "PRICE" in families:
        exprs += [
            (pl.col("open") / pl.col("close")).over("symbol").alias("OPEN0"),
            (pl.col("high") / pl.col("close")).over("symbol").alias("HIGH0"),
            (pl.col("low") / pl.col("close")).over("symbol").alias("LOW0"),
            (pl.col("_vwap") / pl.col("close")).over("symbol").alias("VWAP0"),
        ]
    # rolling 族（numpy 族交给滑窗块）
    for fam in _ROLLING:
        if families is not None and fam.upper() not in families:
            continue
        if fam in _NUMPY_FAMILIES:
            continue
        exprs += [_expr(fam, w).alias(f"{fam}{w}") for w in windows]

    if exprs:
        d = d.with_columns(exprs)

    # numpy 滑窗族：逐 symbol 组算再拼回
    np_fams = ({f.upper() for f in (families or [])} & _NUMPY_FAMILIES) \
        if families is not None else _NUMPY_FAMILIES
    if np_fams:
        parts = []
        for gdf in d.partition_by("symbol"):
            cols = _numpy_block(gdf, windows)
            keep = [k for k in cols if any(k.startswith(f) for f in np_fams)]
            parts.append(gdf.with_columns([pl.Series(k, cols[k]) for k in keep]))
        d = pl.concat(parts)
        # numpy 滑窗的 warmup 期是 NaN；polars 里 NaN≠null（drop_nulls 过滤不掉），统一归 null
        d = d.with_columns([pl.col(c).fill_nan(None)
                            for c in d.columns if any(c.startswith(f) for f in _NUMPY_FAMILIES)])
    return d


def compute(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """算单个内置因子，返回带 `_factor` 列的 df（评价管线接口）。"""
    fam, win = resolve_name(name)
    if fam == "KBAR":
        return df.sort(["symbol", "trade_date"]).with_columns(
            _kbar_expr(name).alias("_factor"))
    if fam == "PRICE":
        col = {"OPEN0": "open", "HIGH0": "high", "LOW0": "low", "VWAP0": "_vwap"}[name]
        d = _prep(df)
        return d.with_columns((pl.col(col) / pl.col("close")).over("symbol").alias("_factor"))
    out = compute_all(df, families={fam}, windows=(win,))
    return out.with_columns(pl.col(f"{fam}{win}").alias("_factor")).drop(f"{fam}{win}")
