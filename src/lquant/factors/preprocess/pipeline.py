"""流水线执行器：按声明式 spec 串起预处理步骤。

spec 形如 default_pipeline() 返回的列表，可来自 YAML / API 请求 / 前端表单，
引擎本身不含任何 if-else 分支 —— 加新方法不用改这里。

顺序是有讲究的：去极值 → 标准化 → 中性化 → 正交化。
先去极值再标准化，否则极值会把 σ 撑大；先中性化再正交化，
否则正交化会把行业暴露重新混进因子里。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.preprocess.registry import METHODS, default_pipeline, get_method

STAGE_ORDER = {"winsorize": 0, "standardize": 1, "neutralize": 2, "orthogonalize": 3}


def normalize_steps(steps: list[dict]) -> list[dict]:
    """按标准顺序排序，并校验每一步的方法确实注册过。

    用户（或前端）可能把步骤写乱序，这里统一成执行序 ——
    否则「先中性化后去极值」这种写法会静默产出错误结果。
    """
    out = []
    for s in steps:
        op = s.get("op")
        if op not in STAGE_ORDER:
            raise FactorError(f"未知预处理步骤 {op!r}，可选: {list(STAGE_ORDER)}")
        if "method" not in s:
            raise FactorError(f"步骤 {op} 缺少 method")
        get_method(op, s["method"])      # 尽早失败，别跑到一半才炸
        out.append(s)
    return sorted(out, key=lambda s: STAGE_ORDER[s["op"]])


def run(df: pl.DataFrame, cols: str | list[str], steps: list[dict] | None = None,
        *, by: str = "trade_date", keep_original: bool = False) -> pl.DataFrame:
    """执行预处理流水线。

    Parameters
    ----------
    cols : 单因子列名，或多因子列名列表（正交化需要多列）
    steps : 默认用 default_pipeline()
    by : 截面分组列
    keep_original : 为 True 时把处理结果写到 `{col}_clean`，保留原值便于对比
    """
    steps = normalize_steps(steps if steps is not None else default_pipeline())
    multi = isinstance(cols, list)
    names = list(cols) if multi else [cols]

    missing = [c for c in names if c not in df.columns]
    if missing:
        raise FactorError(f"因子列不存在: {missing}")

    out = df
    if keep_original:
        out = out.with_columns([pl.col(c).cast(pl.Float64, strict=False).alias(f"{c}_raw")
                                for c in names])
        targets = [f"{c}_clean" for c in names]
        out = out.with_columns([pl.col(c).cast(pl.Float64, strict=False).alias(t)
                                for c, t in zip(names, targets, strict=False)])
    else:
        targets = names
        out = out.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in names])

    for s in steps:
        op = s["op"]
        fn = get_method(op, s["method"])
        kw = {k: v for k, v in s.items() if k not in ("op", "method")}
        if kw.get("factors") is not None and not isinstance(kw["factors"], list):
            kw["factors"] = [kw["factors"]]
        kw.setdefault("by", by)

        if op == "orthogonalize":
            # 正交化是多列联合操作 —— 单因子时无意义，跳过
            if len(targets) < 2:
                continue
            out = fn(out, targets, **kw)
        else:
            for t in targets:
                out = fn(out, t, **kw)

    return out


def describe() -> list[dict]:
    """给前端枚举 UI：每个方法的名字、阶段、默认参数。"""
    return METHODS.describe()


def drop_nonfinite(df: pl.DataFrame, col: str) -> pl.DataFrame:
    """null 与 NaN/Inf 一并剔除（中性化残差中回归剔除行写回 NaN，drop_nulls 必拦不住）。"""
    return df.filter(pl.col(col).is_not_null() & pl.col(col).is_finite())
